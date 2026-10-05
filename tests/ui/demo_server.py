"""`devflow ui` over the offline harness: real graphs, git repos and worktrees; fake Claude, Jira and GitLab.

Used by the Playwright end-to-end tests (frontend/e2e) and handy for trying the UI with no accounts:

    python tests/ui/demo_server.py --port 8799 --token demo      # then open http://127.0.0.1:8799/?token=demo

Every fake Claude call records token usage, and the fake coding agent logs tool calls, so the Tokens views and the
live agent log have something to show. Each call sleeps a little so running steps are visible.
"""
import argparse
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from langgraph.graph import END, START, StateGraph  # noqa: E402
from langgraph.types import interrupt  # noqa: E402
from typing_extensions import TypedDict  # noqa: E402

from dev_workflows import registry, telemetry  # noqa: E402
from dev_workflows.jira_implement.workspace import load_workspace  # noqa: E402
from dev_workflows.ui.server import App, create_app  # noqa: E402
from dev_workflows.jira_implement.models import (CommentDraft, Coverage, CriterionCoverage, TestCase, TestPlanDraft,  # noqa: E402
                                                 TicketUnderstanding)
from jira_implement.harness import FakeCoder, FakeLLM, make_env, sh  # noqa: E402
from jira_implement.test_runner import session as make_session  # noqa: E402

MODELS = {"gather_context": "claude-haiku-4-5", "plan_implementation": "claude-opus-5-5"}


class DemoLLM(FakeLLM):
    def __init__(self, *a, delay=0.3, **kw):
        super().__init__(*a, **kw)
        self.delay = delay

    def structured(self, system, prompt, schema, images=(), step=""):
        time.sleep(self.delay)
        out = super().structured(system, prompt, schema, images, step)
        telemetry.record_usage(step or "llm", MODELS.get(step, "claude-sonnet-5-5"),
                               {"input_tokens": 1800 + len(prompt) % 900, "output_tokens": 420, "cache_creation_input_tokens": 600,
                                "cache_read_input_tokens": 2400}, duration_s=self.delay)
        return out


class DemoCoder(FakeCoder):
    def __init__(self, *a, delay=0.6, **kw):
        super().__init__(*a, **kw)
        self.delay = delay

    def implement(self, repo, path, instructions, step="implement", escalate=False):
        for tool, detail, denied in (("Read", "src/main/OrderService.java", False), ("Edit", "feature.txt (+1 −0)", False),
                                     ("Bash", "rtk git commit -am wip", True)):
            time.sleep(self.delay / 3)
            telemetry.record_activity({"tool": tool, "detail": detail, **({"denied": True, "reason": "the workflow commits, not the agent"} if denied else {})},
                                      repo=repo)
        res = super().implement(repo, path, instructions, step, escalate)
        telemetry.record_usage(step, "claude-sonnet-5-5", {"input_tokens": 64000, "output_tokens": 9000, "cache_read_input_tokens": 41000},
                               source="agent_sdk", cost_usd=0.41, duration_s=self.delay, repo=repo)
        return res


REVIEW_TICKET = "AQS-900"  # the demo's ticket review: its commit is on this branch of the web repo, checked out

REVIEW_ANSWERS = {
    TicketUnderstanding: [TicketUnderstanding(summary="Export orders as CSV with the list's filters.", requirement=["The export uses the filters"],
                                              acceptance_criteria=["CSV respects the filters", "Export button is disabled while exporting"],
                                              out_of_scope=[], unclear=[])],
    Coverage: [Coverage(criteria=[CriterionCoverage(criterion="CSV respects the filters", status="met", evidence="export.ts filters rows"),
                                  CriterionCoverage(criterion="Export button is disabled while exporting", status="partial",
                                                    evidence="button disabled, but not on error")], unrelated_changes=[])],
    TestPlanDraft: [TestPlanDraft(cases=[
        TestCase(title="Filtered export", criterion="CSV respects the filters", preconditions=["orders exist"],
                 steps=["Open /orders", "Filter status = Paid", "Click Export"], expected="CSV has only paid orders", mode="needs_you",
                 needs_you_reason="log in with your SSO account", proof=["the filtered list", "the downloaded CSV"]),
        TestCase(title="Button disabled while exporting", criterion="Export button is disabled while exporting", preconditions=[],
                 steps=["Open /orders", "Click Export"], expected="The button is disabled until the file arrives", mode="auto",
                 needs_you_reason="", proof=["the disabled button"])], not_testable=[])],
    CommentDraft: [CommentDraft(conclusion="ready", summary="The commits implement the filtered CSV export and both E2E cases pass.")],
}


def demo_png(text_seed: int) -> bytes:
    """A small, valid PNG (a fake browser screenshot) with no image library."""
    import struct
    import zlib
    w, h = 480, 300
    rows = []
    for y in range(h):
        row = bytearray([0])
        for x in range(w):
            if y < 36:
                row += bytes((40, 44, 52))
            elif 60 < y < 76 + (text_seed % 3) * 20 and 24 < x < 300:
                row += bytes((52, 145, 255))
            elif 240 < y < 272 and 24 < x < 140:
                row += bytes((66, 203, 128))
            else:
                row += bytes((246, 247, 249))
        rows.append(bytes(row))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


class DemoE2E:
    """Plays Claude Code + Playwright MCP for the ticket review: the first run of a needs-you case asks for help."""

    def __init__(self, evidence, delay):
        self.evidence, self.delay = Path(evidence), delay

    def test_case(self, repos, instructions, step="ticket_review.e2e"):
        cid = instructions.split("Test case ")[1].split(" ")[0]
        time.sleep(self.delay)
        for tool, detail in (("mcp__playwright__browser_navigate", "http://localhost:5173/orders"),
                             ("mcp__playwright__browser_click", "Export"), ("mcp__playwright__browser_take_screenshot", f"{cid}-1.png")):
            telemetry.record_activity({"tool": tool, "detail": detail})
        if '"needs_you"' in instructions and "<developer_input>" not in instructions:
            return {"status": "needs_human", "summary": "The app redirected to the SSO login page.", "steps": [], "screenshots": [],
                    "needs_human": "Log in with your SSO account in the test browser, then continue."}
        n = int(cid[2:])
        for i in (1, 2):
            (self.evidence / f"{cid}-{i}.png").write_bytes(demo_png(n + i))
        return {"status": "passed", "summary": f"{cid}: every expected result was seen.", "steps": [{"step": "Click Export", "ok": True, "note": ""}],
                "screenshots": [f"{cid}-1.png", f"{cid}-2.png"], "needs_human": ""}


class QState(TypedDict, total=False):
    topic: str
    answer: str
    output: str


def build_question_graph(checkpointer=None):
    """A graph that knows nothing about devflow: one plain interrupt()."""
    g = StateGraph(QState)
    g.add_node("draft", lambda s: (time.sleep(0.2), {"answer": ""})[1])
    g.add_node("ask", lambda s: {"answer": interrupt({"question": f"Ship {s.get('topic', 'it')}?"})})
    g.add_node("finish", lambda s: {"output": f"Shipped {s.get('topic')}: {s['answer']}"})
    g.add_edge(START, "draft")
    g.add_edge("draft", "ask")
    g.add_edge("ask", "finish")
    g.add_edge("finish", END)
    return g.compile(checkpointer=checkpointer)


def build_app(tmp: Path, token: str, delay: float = 0.3) -> App:
    registry.register_workflow("question_graph", lambda cp: build_question_graph(cp), title="Question graph",
                               description="A plain LangGraph graph with one interrupt(), monitored with no UI code.")
    ws, genv = make_env(tmp)
    raw = __import__("json").loads((tmp / "workspace.yaml").read_text())
    raw["prices"] = {"claude-sonnet-5-5": {"input": 2, "output": 10, "cache_write": 2.5, "cache_read": 0.2},
                     "claude-opus-5-5": {"input": 4, "output": 20, "cache_write": 5, "cache_read": 0.2},
                     "claude-haiku-4-5": {"input": 1, "output": 5, "cache_write": 1.25, "cache_read": 0.1}}
    raw["mcp_servers"] = {"atlassian": {"command": "uvx", "args": ["mcp-atlassian"], "env": {"JIRA_URL": "https://acme.atlassian.net",
                                                                                         "JIRA_API_TOKEN": "s3cret"}},
                          "playwright": {"command": "npx", "args": ["@playwright/mcp@latest"]}}
    raw["repos"]["web"]["app_url"] = "http://localhost:5173"
    (tmp / "workspace.yaml").write_text(__import__("json").dumps(raw))
    web = raw["repos"]["web"]["path"]  # the ticket review's commit, on a branch the developer has checked out
    sh("git", "-C", web, "checkout", "-q", "-b", REVIEW_TICKET)
    Path(web, "export.ts").write_text("export const toCsv = (rows, f) => rows.filter(f).map(String).join('\\n')\n")
    sh("git", "-C", web, "add", ".")
    sh("git", "-C", web, "commit", "-qm", f"{REVIEW_TICKET}: CSV export")
    sh("git", "-C", web, "push", "-q", "-u", "origin", REVIEW_TICKET)
    (tmp / "review-commit.txt").write_text(sh("git", "-C", web, "rev-parse", "--short=9", "HEAD"))
    llm = DemoLLM(["api", "web"], [("api", "web")], delay=delay, overrides=REVIEW_ANSWERS)
    coder = DemoCoder(delay=delay * 2)

    def factory():
        s, _, _ = make_session(tmp, load_workspace(tmp / "workspace.yaml"), genv, llm, coder)
        make_deps = s._deps_factory

        def with_e2e(ws_, store):
            deps = make_deps(ws_, store)
            deps.e2e_agent = lambda state, evidence, profile: DemoE2E(evidence, delay * 2)
            return deps
        s._deps_factory = with_e2e
        return s

    return App(factory, str(tmp / "workspace.yaml"), token=token)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--token", default="demo")
    ap.add_argument("--dir", default="")
    ap.add_argument("--delay", type=float, default=0.3)
    a = ap.parse_args()
    tmp = Path(a.dir) if a.dir else Path(tempfile.mkdtemp(prefix="devflow-demo-"))
    tmp.mkdir(parents=True, exist_ok=True)
    import uvicorn
    app = build_app(tmp, a.token, a.delay)
    print(f"devflow demo: http://127.0.0.1:{a.port}/?token={a.token}  (data in {tmp})\n"
          f"ticket review: {REVIEW_TICKET} with commit web={(tmp / 'review-commit.txt').read_text()}", flush=True)
    uvicorn.run(create_app(app), host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
