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
from jira_implement.harness import FakeCoder, FakeLLM, make_env  # noqa: E402
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
                                                                                         "JIRA_API_TOKEN": "s3cret"}}}
    (tmp / "workspace.yaml").write_text(__import__("json").dumps(raw))
    llm = DemoLLM(["api", "web"], [("api", "web")], delay=delay)
    coder = DemoCoder(delay=delay * 2)

    def factory():
        s, _, _ = make_session(tmp, load_workspace(tmp / "workspace.yaml"), genv, llm, coder)
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
    print(f"devflow demo: http://127.0.0.1:{a.port}/?token={a.token}  (data in {tmp})", flush=True)
    uvicorn.run(create_app(app), host="127.0.0.1", port=a.port, log_level="warning")


if __name__ == "__main__":
    main()
