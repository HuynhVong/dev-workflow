import json
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel
from typing_extensions import TypedDict

from dev_workflows import llm as llm_mod
from dev_workflows import registry
from dev_workflows.jira_implement.workspace import MASK, load_workspace
from dev_workflows.llm import ClaudeLLM
from dev_workflows.ui.server import App, create_app

from jira_implement.harness import FakeCoder, FakeLLM, make_env
from jira_implement.test_runner import session as make_session


class Echo(BaseModel):
    text: str


class FakeAnthropic:
    """Just enough of anthropic.Anthropic for ClaudeLLM.structured, with usage numbers."""

    def __init__(self):
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self.parse))

    def parse(self, model, **kw):
        usage = SimpleNamespace(input_tokens=1200, output_tokens=300, cache_creation_input_tokens=100, cache_read_input_tokens=800)
        return SimpleNamespace(parsed_output=Echo(text="hi"), usage=usage, stop_reason="end_turn", model=model, stop_details=None)


class TState(TypedDict, total=False):
    topic: str
    reply: str
    answer: str
    output: str


def build_test_graph(checkpointer=None):
    """A graph that knows nothing about devflow: one LLM call, one plain interrupt()."""
    llm = ClaudeLLM(client=FakeAnthropic())

    def think(s):
        return {"reply": llm.structured("sys", s["topic"], Echo, step="standup").text}

    def ask(s):
        return {"answer": interrupt({"question": f"Ship {s['topic']}?"})}

    g = StateGraph(TState)
    g.add_node("think", think)
    g.add_node("ask", ask)
    g.add_node("finish", lambda s: {"output": f"{s['reply']} / {s['answer']}"})
    g.add_edge(START, "think")
    g.add_edge("think", "ask")
    g.add_edge("ask", "finish")
    g.add_edge("finish", END)
    return g.compile(checkpointer=checkpointer)


@pytest.fixture
def env(tmp_path):
    registry.register_workflow("test_only_graph", lambda cp: build_test_graph(cp), title="Test-only graph")
    ws, genv = make_env(tmp_path)
    raw = json.loads((tmp_path / "workspace.yaml").read_text())
    raw["mcp_servers"] = {"atlassian": {"command": "uvx", "args": ["mcp-atlassian"],
                                        "env": {"JIRA_URL": "https://acme.atlassian.net", "JIRA_API_TOKEN": "s3cret", "OTHER": "${HOME}"}}}
    raw["prices"] = {"claude-sonnet": {"input": 3, "output": 15}}
    (tmp_path / "workspace.yaml").write_text(json.dumps(raw))
    ws = load_workspace(tmp_path / "workspace.yaml")
    llm = FakeLLM(["api", "web"], [("api", "web")])

    def factory():
        s, _, _ = make_session(tmp_path, load_workspace(tmp_path / "workspace.yaml"), genv, llm, FakeCoder())
        return s

    app = App(factory, str(tmp_path / "workspace.yaml"), token="tok")
    client = TestClient(create_app(app))
    client.get("/?token=tok", follow_redirects=False)
    yield SimpleNamespace(app=app, client=client, ws=ws, tmp=tmp_path)
    app.manager.shutdown()
    registry._registered.pop("test_only_graph", None)


def wait_status(client, run_id, status, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        r = client.get(f"/api/runs/{run_id}").json()
        if r["status"] == status and not r["active"]:
            return r
        time.sleep(0.05)
    raise AssertionError(f"{run_id} never reached {status}: {client.get(f'/api/runs/{run_id}').json()['status']}")


def test_local_security(env):
    fresh = TestClient(create_app(env.app))
    assert fresh.get("/api/meta").status_code == 401
    r = fresh.get("/?token=tok", follow_redirects=False)
    assert r.status_code == 303 and "devflow_token" in r.headers["set-cookie"]
    assert fresh.get("/api/meta").status_code == 200
    assert fresh.get("/api/meta", headers={"host": "evil.example:8765"}).status_code == 403
    assert TestClient(create_app(env.app)).get("/?token=wrong", follow_redirects=False).status_code == 401


def test_workflows_are_discovered_with_graphs_and_forms(env):
    data = env.client.get("/api/workflows").json()
    by_id = {w["id"]: w for w in data["workflows"]}
    assert {"jira_ticket_implement", "address_review", "ticket_to_plan", "pr_review", "standup", "test_only_graph"} <= set(by_id)
    t = by_id["test_only_graph"]
    assert t["generated_form"] and [f["name"] for f in t["form"]] == ["topic", "reply", "answer", "output"]
    assert {"source": "think", "target": "ask", "conditional": False} in t["graph"]["edges"]
    assert by_id["jira_ticket_implement"]["form"][0]["type"] == "ticket"


def test_any_graph_gets_full_history_usage_and_a_generic_checkpoint(env):
    c = env.client
    run_id = c.post("/api/runs", json={"workflow": "test_only_graph", "values": {"topic": "the export", "_label": "Export"}}).json()["run_id"]
    r = wait_status(c, run_id, "WAITING_HUMAN")
    assert r["label"] == "Export" and r["pending"]["generic"] and r["pending"]["name"] == "ask"
    assert r["pending"]["payload"] == {"question": "Ship the export?"}
    assert c.post(f"/api/runs/{run_id}/answer", json={"value": "yes"}).status_code == 200
    r = wait_status(c, run_id, "COMPLETED")
    assert r["output"] == "hi / yes"
    kinds = [(e["kind"], e["node"]) for e in c.get(f"/api/runs/{run_id}/events").json()["events"]]
    for k in [("node_started", "think"), ("usage", "think"), ("node_finished", "think"), ("checkpoint_waiting", "ask"),
              ("decision", "ask"), ("node_finished", "finish"), ("output", "")]:
        assert k in kinds, k
    u = c.get(f"/api/runs/{run_id}/usage").json()
    assert u["totals"]["input_tokens"] == 1200 and u["totals"]["cache_read_tokens"] == 800
    assert u["by_node"][0]["node"] == "think" and u["rows"][0]["model"] == "claude-haiku-4-5-20251001"
    assert c.get("/api/runs").json()["runs"][0]["tokens"]["output"] == 300
    fin = next(e for e in c.get(f"/api/runs/{run_id}/events").json()["events"] if e["kind"] == "node_finished" and e["node"] == "think")
    node = c.get(f"/api/runs/{run_id}/nodes/{fin['seq']}").json()
    assert node["finished"]["data"]["result"] == {"reply": "hi"} and node["input"]["topic"] == "the export"
    assert node["usage"][0]["data"]["input"] == 1200


def test_ticket_run_end_to_end_through_the_api(env):
    c = env.client
    bad = c.post("/api/runs", json={"workflow": "jira_ticket_implement", "values": {"ticket": "not a key"}})
    assert bad.status_code == 400
    run_id = c.post("/api/runs", json={"workflow": "jira_ticket_implement", "values": {"ticket": "aqs-1", "repos": ["api", "web"]}}).json()["run_id"]
    assert run_id.startswith("AQS-1-")
    assert c.post("/api/runs", json={"workflow": "jira_ticket_implement", "values": {"ticket": "AQS-1"}}).status_code == 409
    r = wait_status(c, run_id, "WAITING_HUMAN")
    assert r["pending"]["name"] == "approve_plan" and r["label"] == "AQS-1: Export orders as CSV"
    assert c.post(f"/api/runs/{run_id}/answer", json={"choice": "merge"}).status_code == 400
    c.post(f"/api/runs/{run_id}/answer", json={"choice": "approve"})
    r = wait_status(c, run_id, "WAITING_HUMAN")
    assert r["pending"]["name"] == "manual_test"
    assert r["repos"]["api"]["wave"] == 0 and r["repos"]["web"]["wave"] == 1 and r["repos"]["web"]["status"] == "ready"
    d = c.get(f"/api/runs/{run_id}/diff", params={"repo": "web"}).json()
    assert d["files"] == ["feature.txt"] and d["path"] == env.ws.worktree("AQS-1", "web")
    wts = c.get("/api/worktrees").json()["worktrees"]
    assert {w["repo"] for w in wts} == {"api", "web"} and not any(w["can_clean"] for w in wts)
    c.post(f"/api/runs/{run_id}/answer", json={"choice": "ok"})
    assert wait_status(c, run_id, "WAITING_HUMAN")["pending"]["name"] == "approve_push"
    c.post(f"/api/runs/{run_id}/answer", json={"choice": "approve", "note": "ship it"})
    r = wait_status(c, run_id, "COMPLETED")
    assert set(r["mrs"]) == {"api", "web"} and [d["checkpoint"] for d in r["decisions"]] == ["approve_plan", "manual_test", "approve_push"]
    audit = c.get(f"/api/runs/{run_id}/audit").json()
    assert any(e["action"] == "create_mr" for e in audit["side_effects"])


def test_abort_a_waiting_run(env):
    c = env.client
    run_id = c.post("/api/runs", json={"workflow": "jira_ticket_implement", "values": {"ticket": "AQS-2", "repos": ["api", "web"]}}).json()["run_id"]
    wait_status(c, run_id, "WAITING_HUMAN")
    assert c.post(f"/api/runs/{run_id}/abort", json={"note": "wrong ticket"}).json()["status"] == "ABORTING"
    r = wait_status(c, run_id, "ABORTED")
    assert r["detail"] == "wrong ticket"
    assert c.post(f"/api/runs/{run_id}/resume", json={}).status_code == 409
    c.post(f"/api/runs/{run_id}/resume", json={"reopen": True})
    assert wait_status(c, run_id, "WAITING_HUMAN")["pending"]["name"] == "approve_plan"


def test_runs_beyond_max_parallel_wait_in_the_queue(env):
    c = env.client
    env.app.manager.max_parallel = 1
    gate = __import__("threading").Event()
    real_drive = env.app.session.drive

    def slow_drive(run_id, inp):
        if run_id.startswith("AQS-3"):
            gate.wait(10)
        return real_drive(run_id, inp)

    env.app.session.drive = slow_drive
    a = c.post("/api/runs", json={"workflow": "jira_ticket_implement", "values": {"ticket": "AQS-3", "repos": ["api", "web"]}}).json()
    b = c.post("/api/runs", json={"workflow": "jira_ticket_implement", "values": {"ticket": "AQS-4", "repos": ["api", "web"]}}).json()
    assert not a["queued"] and b["queued"]
    assert c.get(f"/api/runs/{b['run_id']}").json()["status"] == "PENDING"
    assert c.get("/api/meta").json()["slots"] == {"max": 1, "running": 1, "queued": 1}
    gate.set()
    wait_status(c, a["run_id"], "WAITING_HUMAN")
    assert wait_status(c, b["run_id"], "WAITING_HUMAN")["pending"]["name"] == "approve_plan"  # started once a slot freed


def test_workspace_secrets_never_reach_the_browser(env):
    c = env.client
    w = c.get("/api/workspace").json()
    srv = w["data"]["mcp_servers"]["atlassian"]["env"]
    assert srv["JIRA_API_TOKEN"] == MASK and srv["JIRA_URL"] == "https://acme.atlassian.net" and srv["OTHER"] == "${HOME}"
    assert "s3cret" not in json.dumps(w) and w["env_refs"]["HOME"] == "set"
    data = w["data"]
    data["max_parallel_runs"] = 2
    assert c.put("/api/workspace", json={"data": data}).status_code == 200
    raw = __import__("yaml").safe_load((env.tmp / "workspace.yaml").read_text())
    assert raw["mcp_servers"]["atlassian"]["env"]["JIRA_API_TOKEN"] == "s3cret" and raw["max_parallel_runs"] == 2
    bad = dict(data, repos={"x": {"no_path": True}})
    assert c.put("/api/workspace", json={"data": bad}).status_code == 400


def test_doctor_checks_and_usage_aggregates(env):
    c = env.client
    d = c.post("/api/doctor/run", json={}).json()
    by_id = {x["id"]: x for x in d["checks"]}
    assert by_id["mcp.jira"]["status"] == "ok" and by_id["repo.api"]["status"] == "ok"
    assert by_id["cli.git"]["status"] == "ok" and "summary" in d
    run_id = c.post("/api/runs", json={"workflow": "test_only_graph", "values": {"topic": "x"}}).json()["run_id"]
    wait_status(c, run_id, "WAITING_HUMAN")
    u = c.get("/api/usage").json()
    assert u["totals"]["input_tokens"] == 1200 and u["by_workflow"][0]["workflow"] == "test_only_graph"
