import sqlite3
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from dev_workflows.jira_implement import handoff, task_inputs
from dev_workflows.jira_implement.graph import build_graph

from .harness import FakeCoder, FakeLLM, make_deps, make_env, wt
from .test_flow import answer, at


def start_manual(tmp_path, ws, deps):
    g = build_graph(deps, checkpointer=SqliteSaver(sqlite3.connect(str(tmp_path / "runs.sqlite"), check_same_thread=False)))
    cfg = {"configurable": {"thread_id": "AQS-1-run"}}
    deps.store.create_run("AQS-1-run", "jira_ticket_implement", "AQS-1")
    out = g.invoke({"run_id": "AQS-1-run", "ticket_key": "AQS-1", "requested_repos": list(ws.repos), "manual_code": True}, cfg)
    return g, cfg, out


def test_you_code_in_the_worktrees_and_the_agent_never_runs(tmp_path):
    ws, env = make_env(tmp_path, repos=("api",), checks={"api": "test -f mine.txt"})
    coder = FakeCoder()
    deps, store, _ = make_deps(tmp_path, ws, env, FakeLLM(["api"], []), coder)
    g, cfg, out = start_manual(tmp_path, ws, deps)
    out = answer(g, cfg, choice="approve")
    assert at(out) == "code_by_hand"
    payload = out["__interrupt__"][0].value["payload"]
    plan = Path(payload["plan_file"]).read_text()
    assert "## Repos" in plan and "### api" in plan and wt(ws, "api") in plan and "Do not commit" in plan
    assert payload["repos"]["api"]["worktree"] == wt(ws, "api")
    Path(wt(ws, "api"), "mine.txt").write_text("by hand\n")
    out = answer(g, cfg, choice="done")
    assert at(out) == "manual_test"  # checks passed, then your manual test
    assert not [c for c in coder.calls if c[0] == "implement"]
    state = g.get_state(cfg).values
    assert state["repos"]["api"]["status"] == "ready" and state["code_version"] == 1


def test_checks_that_fail_on_your_code_come_back_to_you_not_to_the_agent(tmp_path):
    ws, env = make_env(tmp_path, repos=("api",), checks={"api": "false"})
    coder = FakeCoder()
    deps, _, _ = make_deps(tmp_path, ws, env, FakeLLM(["api"], []), coder)
    g, cfg, out = start_manual(tmp_path, ws, deps)
    out = answer(g, cfg, choice="approve")
    out = answer(g, cfg, choice="done")
    assert at(out) == "code_by_hand"
    assert "Checks failed" in out["__interrupt__"][0].value["payload"]["repos"]["api"]["fix_this_first"][0]
    assert not coder.calls or all(c[0] != "implement" for c in coder.calls)


def test_the_start_form_choice_reaches_the_run_inputs(tmp_path):
    ws, _ = make_env(tmp_path, repos=("api",))
    assert task_inputs.collect(ws, "r", {"manual_code": True}) == {"manual_code": True}
    assert task_inputs.collect(ws, "r", {}) == {}


def test_plan_markdown_lists_only_repos_that_need_code():
    state = {"ticket_key": "AQS-1", "ticket": {"title": "Export", "description": "d"}, "scope": {"a": "/w/a", "b": "/w/b"},
             "analysis": {"summary": "s", "acceptance_criteria": ["c1"]},
             "plan": {"contracts": ["GET /x"], "migrations": [], "test_plan": [{"criterion": "c1", "tests": ["t1"]}]},
             "dag": {"edges": [["a", "b"]]},
             "repos": {"a": {"status": "ready"}, "b": {"status": "needs_fix", "tasks": ["do b"], "likely_files": ["b.py"],
                                                       "fix_instructions": ["fix it"]}}}
    md = handoff.plan_markdown(state, {"b": [("test", "pytest")]})
    assert "a before b" in md and "(done, nothing to do)" in md and "do b" in md and "fix it" in md and "`pytest`" in md


def test_run_each_runs_side_by_side_in_order_and_keeps_the_context():
    import contextvars
    import threading
    import time

    from dev_workflows.jira_implement.parallel import run_each

    var = contextvars.ContextVar("v", default="none")
    var.set("run-1")
    seen, gate = [], threading.Barrier(3, timeout=5)

    def work(n):
        gate.wait()  # only passes when three calls are in flight at once
        seen.append(var.get())
        time.sleep(0.01 * (3 - n))
        return n * 2

    assert run_each([0, 1, 2], work) == [0, 2, 4] and seen == ["run-1"] * 3


def test_run_each_raises_the_first_failure_after_all_finished():
    import pytest

    from dev_workflows.jira_implement.parallel import run_each

    done = []

    def work(n):
        done.append(n)
        if n == 1:
            raise ValueError("boom")
        return n

    with pytest.raises(ValueError):
        run_each([0, 1, 2], work)
    assert sorted(done) == [0, 1, 2]
