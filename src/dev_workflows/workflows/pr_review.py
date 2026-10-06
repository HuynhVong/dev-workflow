"""PR review: triage, then parallel reviewers per lens, then one merged verdict.

triage -> [review(lens) x N in parallel via Send] -> verdict -> render
"""
import operator
from typing import Annotated, Literal

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel, Field
from typing_extensions import NotRequired, TypedDict

from ..llm import StructuredLLM, default_llm
from ..textutil import diff_digest

Lens = Literal["correctness", "security", "performance", "tests", "frontend"]
Severity = Literal["blocker", "major", "minor", "nit"]
SEVERITY_ORDER = {"blocker": 0, "major": 1, "minor": 2, "nit": 3}

LENS_GUIDE: dict[str, str] = {
    "correctness": "logic errors, edge cases, null/empty handling, error handling, race conditions, broken contracts",
    "security": "injection, authz/authn gaps, secrets in code, unsafe deserialization, XSS/CSRF, input validation",
    "performance": "N+1 queries, missing indexes, unbounded loops or payloads, needless re-renders, blocking I/O",
    "tests": "untested behaviour changes, brittle tests, missing regression test for the bug fixed",
    "frontend": "accessibility, loading/error/empty states, state management, responsive layout, i18n",
}


class Triage(BaseModel):
    summary: str = Field(description="What the PR does, in two or three sentences.")
    touches_frontend: bool
    risk: Literal["low", "medium", "high"]
    lenses: list[Lens] = Field(description="Which review lenses are worth running for this diff.")


class Finding(BaseModel):
    severity: Severity
    file: str
    line: int | None = None
    title: str
    detail: str
    suggestion: str


class LensReview(BaseModel):
    findings: list[Finding]


class Verdict(BaseModel):
    decision: Literal["approve", "comment", "request_changes"]
    summary: str = Field(description="Short review summary to post on the PR.")


DEVFLOW_UI = {
    "title": "PR review",
    "description": "Triages a diff, reviews it through several lenses in parallel and merges one verdict.",
    "icon": "search-code", "color": "#eeb747",
    "steps": ["triage", "review", "verdict", "render"],
    "nodes": {"triage": "Triage", "review": "Lens reviews", "verdict": "Verdict", "render": "Write review"},
    "form": [{"name": "title", "label": "Title", "type": "text", "required": True},
             {"name": "description", "label": "Description (optional)", "type": "textarea"},
             {"name": "diff", "label": "Diff", "type": "textarea", "required": True, "mono": True,
              "placeholder": "Paste the output of git diff develop...HEAD"}],
    "prepare": lambda v, ws=None: (v.get("title", "PR review")[:80], {"title": v.get("title", ""), "description": v.get("description", ""),
                                                                        "diff": v.get("diff", ""), "findings": []}),
}


class State(TypedDict):
    title: str
    diff: str
    description: NotRequired[str]
    triage: NotRequired[Triage]
    findings: Annotated[list[Finding], operator.add]
    verdict: NotRequired[Verdict]
    output: NotRequired[str]


class LensTask(TypedDict):
    lens: str
    title: str
    description: str
    diff: str


# Every lens reads the whole diff, so the diff is sent once per lens (2 to 5) plus once to triage. Over its budget a diff
# is digested (file list with +/- counts, whole files while they fit) rather than sent in full to each of them.
TRIAGE_DIFF = 8000   # triage only picks lenses and a risk level: the file list and the first files are enough
LENS_DIFF = 30000

SYSTEM = (
    "You are a careful senior reviewer on a full-stack team. Report only issues you can "
    "point to in the diff. Never pad the review; an empty list is a fine answer."
)


def _pr(title: str, description: str, diff: str) -> str:
    return f"<pr_title>{title}</pr_title>\n<pr_description>\n{description}\n</pr_description>\n<diff>\n{diff}\n</diff>"


def build_graph(llm: StructuredLLM | None = None, checkpointer=None):
    def get_llm() -> StructuredLLM:
        return llm or default_llm()

    def triage(state: State):
        t = get_llm().structured(
            SYSTEM, _pr(state["title"], state.get("description", ""), diff_digest(state["diff"], TRIAGE_DIFF)) + "\n\nTriage this PR.", Triage, step="pr_review.triage"
        )
        lenses = set(t.lenses) | {"correctness", "tests"}
        if t.touches_frontend:
            lenses.add("frontend")
        if t.risk == "high":
            lenses.add("security")
        return {"triage": t.model_copy(update={"lenses": sorted(lenses)})}

    def fan_out(state: State):
        return [
            Send("review", {"lens": lens, "title": state["title"],
                            "description": state.get("description", ""), "diff": diff_digest(state["diff"], LENS_DIFF)})
            for lens in state["triage"].lenses
        ]

    def review(task: LensTask):
        prompt = (
            _pr(task["title"], task["description"], task["diff"])
            + f"\n\nReview ONLY through the {task['lens']} lens: {LENS_GUIDE[task['lens']]}."
        )
        return {"findings": get_llm().structured(SYSTEM, prompt, LensReview, step="pr_review.lens").findings}

    def verdict(state: State):
        findings = sorted(state["findings"], key=lambda f: SEVERITY_ORDER[f.severity])
        listing = "\n".join(f"- [{f.severity}] {f.file}:{f.line or '?'} {f.title}" for f in findings) or "(none)"
        v = get_llm().structured(
            SYSTEM,
            f"<pr_summary>{state['triage'].summary}</pr_summary>\n<findings>\n{listing}\n</findings>\n\n"
            "Merge duplicate findings in your summary and decide: request_changes if any blocker or "
            "major remains, comment for minor-only, approve otherwise.",
            Verdict,
            step="pr_review.verdict",
        )
        return {"verdict": v}

    def render(state: State):
        return {"output": render_markdown(state)}

    g = StateGraph(State)
    g.add_node("triage", triage)
    g.add_node("review", review)
    g.add_node("verdict", verdict)
    g.add_node("render", render)
    g.add_edge(START, "triage")
    g.add_conditional_edges("triage", fan_out, ["review"])
    g.add_edge("review", "verdict")
    g.add_edge("verdict", "render")
    g.add_edge("render", END)
    return g.compile(checkpointer=checkpointer)


def render_markdown(state: State) -> str:
    v, t = state["verdict"], state["triage"]
    lines = [f"# Review: {state['title']}", "", f"**Decision:** {v.decision} · **Risk:** {t.risk}", "", v.summary, ""]
    findings = sorted(state["findings"], key=lambda f: SEVERITY_ORDER[f.severity])
    if findings:
        lines.append("## Findings")
        for f in findings:
            loc = f"{f.file}:{f.line}" if f.line else f.file
            lines += [f"- **[{f.severity}] {f.title}** (`{loc}`)", f"  {f.detail}", f"  *Suggestion:* {f.suggestion}"]
    return "\n".join(lines) + "\n"


graph = build_graph()
