"""Ticket -> implementation plan.

analyze -> (clarify, human-in-the-loop) -> draft_plan <-> critique -> render

`clarify` pauses the graph with `interrupt()` when the ticket has open questions,
so you can answer them before any plan is written. Set `interactive=False` to skip
the pause and have the plan list its assumptions instead.
"""
from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, Field
from typing_extensions import NotRequired, TypedDict

from ..llm import StructuredLLM, default_llm

Area = Literal["frontend", "backend", "database", "infra", "tests", "docs"]
MAX_REVISIONS = 2


class TicketAnalysis(BaseModel):
    summary: str = Field(description="One or two sentences on what the ticket asks for.")
    kind: Literal["feature", "bug", "chore", "spike"]
    areas: list[Area]
    acceptance_criteria: list[str]
    open_questions: list[str] = Field(description="Questions that block a confident plan. Empty if none.")


class PlanStep(BaseModel):
    title: str
    area: Area
    details: str
    likely_files: list[str] = Field(description="Paths or modules likely touched, if inferable from context.")


class ImplementationPlan(BaseModel):
    steps: list[PlanStep]
    api_changes: list[str]
    db_migrations: list[str]
    test_plan: list[str]
    risks: list[str]
    assumptions: list[str]
    size: Literal["S", "M", "L", "XL"]


class PlanCritique(BaseModel):
    approved: bool
    issues: list[str] = Field(description="Concrete gaps: missing steps, untested criteria, risky ordering.")


class State(TypedDict):
    ticket: str
    repo_context: NotRequired[str]
    interactive: NotRequired[bool]
    analysis: NotRequired[TicketAnalysis]
    answers: NotRequired[str]
    plan: NotRequired[ImplementationPlan]
    critique: NotRequired[PlanCritique]
    revisions: NotRequired[int]
    output: NotRequired[str]


SYSTEM = (
    "You are a senior full-stack engineer on a product team. You turn tickets into "
    "concrete, reviewable implementation plans. Be specific to the codebase context "
    "you are given and do not invent files you have no evidence for."
)


def _context(state: State) -> str:
    parts = [f"<ticket>\n{state['ticket']}\n</ticket>"]
    if state.get("repo_context"):
        parts.append(f"<repo_context>\n{state['repo_context']}\n</repo_context>")
    if state.get("answers"):
        parts.append(f"<answers_to_open_questions>\n{state['answers']}\n</answers_to_open_questions>")
    return "\n\n".join(parts)


def build_graph(llm: StructuredLLM | None = None, checkpointer=None):
    def get_llm() -> StructuredLLM:
        return llm or default_llm()

    def analyze(state: State):
        analysis = get_llm().structured(
            SYSTEM,
            _context(state) + "\n\nAnalyze this ticket. List only questions that genuinely block planning.",
            TicketAnalysis,
        )
        return {"analysis": analysis}

    def route_after_analyze(state: State) -> str:
        if state["analysis"].open_questions and state.get("interactive", True):
            return "clarify"
        return "draft_plan"

    def clarify(state: State):
        answers = interrupt({"questions": state["analysis"].open_questions})
        return {"answers": answers if isinstance(answers, str) else "\n".join(map(str, answers))}

    def draft_plan(state: State):
        prompt = _context(state) + f"\n\n<analysis>\n{state['analysis'].model_dump_json(indent=2)}\n</analysis>"
        if state.get("critique") and not state["critique"].approved:
            prompt += (
                f"\n\n<previous_plan>\n{state['plan'].model_dump_json(indent=2)}\n</previous_plan>"
                "\n<review_issues>\n- " + "\n- ".join(state["critique"].issues) + "\n</review_issues>"
                "\n\nRevise the plan to address every review issue."
            )
        else:
            prompt += (
                "\n\nWrite an ordered implementation plan. Steps should be small enough for one "
                "commit each. Every acceptance criterion must be covered by the test plan. "
                "Record anything you assumed under assumptions."
            )
        return {"plan": get_llm().structured(SYSTEM, prompt, ImplementationPlan),
                "revisions": state.get("revisions", 0) + (1 if state.get("critique") else 0)}

    def critique(state: State):
        prompt = (
            _context(state)
            + f"\n\n<analysis>\n{state['analysis'].model_dump_json(indent=2)}\n</analysis>"
            + f"\n\n<plan>\n{state['plan'].model_dump_json(indent=2)}\n</plan>"
            + "\n\nReview this plan as the tech lead. Approve unless there is a concrete gap."
        )
        return {"critique": get_llm().structured(SYSTEM, prompt, PlanCritique)}

    def route_after_critique(state: State) -> str:
        if state["critique"].approved or state.get("revisions", 0) >= MAX_REVISIONS:
            return "render"
        return "draft_plan"

    def render(state: State):
        return {"output": render_markdown(state)}

    g = StateGraph(State)
    g.add_node("analyze", analyze)
    g.add_node("clarify", clarify)
    g.add_node("draft_plan", draft_plan)
    g.add_node("critique", critique)
    g.add_node("render", render)
    g.add_edge(START, "analyze")
    g.add_conditional_edges("analyze", route_after_analyze, ["clarify", "draft_plan"])
    g.add_edge("clarify", "draft_plan")
    g.add_edge("draft_plan", "critique")
    g.add_conditional_edges("critique", route_after_critique, ["draft_plan", "render"])
    g.add_edge("render", END)
    return g.compile(checkpointer=checkpointer)


def render_markdown(state: State) -> str:
    a, p = state["analysis"], state["plan"]
    lines = [f"# Plan: {a.summary}", "", f"**Type:** {a.kind} · **Size:** {p.size} · **Areas:** {', '.join(a.areas)}", ""]
    lines += ["## Acceptance criteria", *[f"- [ ] {c}" for c in a.acceptance_criteria], ""]
    lines += ["## Steps"]
    for i, s in enumerate(p.steps, 1):
        files = f" ({', '.join(f'`{f}`' for f in s.likely_files)})" if s.likely_files else ""
        lines += [f"{i}. **[{s.area}] {s.title}**{files}", f"   {s.details}"]
    for title, items in [("API changes", p.api_changes), ("DB migrations", p.db_migrations),
                         ("Test plan", p.test_plan), ("Risks", p.risks), ("Assumptions", p.assumptions)]:
        if items:
            lines += ["", f"## {title}", *[f"- {x}" for x in items]]
    c = state.get("critique")
    if c and not c.approved and c.issues:
        lines += ["", "## Unresolved review notes", *[f"- {x}" for x in c.issues]]
    return "\n".join(lines) + "\n"


graph = build_graph()
