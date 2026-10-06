from pathlib import Path
import sqlite3

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from dev_workflows.jira_implement.address_review import build_review_graph
from dev_workflows.jira_implement.models import ReviewFixPlan, ReviewTriage, ThreadPlan

from .harness import FakeCoder, FakeLLM, glab_db, make_deps, make_env, seed_review, sh, wt

T_WEB, T_API = "aa11", "bb22"


def plan(thread, repo, action, behaviour=True, reply="Done, thanks!"):
    return ThreadPlan(thread_id=thread, category="must_fix" if action == "fix" else "question", repo=repo, summary=f"{action} {thread}",
                      proposed_action=action, fix_instructions="rename the label" if action == "fix" else "", reply=reply,
                      behaviour_change=behaviour, contract_change=False)


def setup(tmp_path, items, repos=("api", "web"), checks=None, delivery=True):
    ws, env = make_env(tmp_path, repos=repos, checks=checks)
    seed_review(tmp_path, ws, threads=[("web", T_WEB, "Label says Exprot"), ("api", T_API, "Why 50k rows?")][:len(repos)])
    llm = FakeLLM(list(repos), [], overrides={ReviewTriage: [ReviewTriage(items=items)],
                                              ReviewFixPlan: [ReviewFixPlan(edges=[], integration_required=False)]})
    coder = FakeCoder()
    deps, store, mcp = make_deps(tmp_path, ws, env, llm, coder)
    if delivery:
        mcp.comments.append({"id": "1", "body": "Draft MRs for AQS-1 ...\n\n[devflow:AQS-1-impl:delivery]"})
    saver = SqliteSaver(sqlite3.connect(str(tmp_path / "runs.sqlite"), check_same_thread=False))
    g = build_review_graph(deps, checkpointer=saver)
    cfg = {"configurable": {"thread_id": "AQS-1-review"}}
    store.create_run("AQS-1-review", "address_review", "AQS-1")
    out = g.invoke({"run_id": "AQS-1-review", "ticket_key": "AQS-1", "requested_repos": list(repos)}, cfg)
    return ws, g, cfg, out, store, mcp, coder


def at(out):
    return out["__interrupt__"][0].value["name"] if "__interrupt__" in out else None


def answer(g, cfg, **ans):
    return g.invoke(Command(resume=ans), cfg)


def notes(tmp_path, repo, thread):
    return next(d for d in glab_db(tmp_path)["discussions"][repo]["1"] if d["id"] == thread)["notes"]


def test_fix_one_thread_answer_another_then_push_reply_and_refresh_jira(tmp_path):
    ws, g, cfg, out, store, mcp, coder = setup(tmp_path, [plan(T_WEB, "web", "fix"), plan(T_API, "api", "answer", reply="Product asked for it.")])
    assert at(out) == "triage"
    assert sorted(t["proposed_action"] for t in out["__interrupt__"][0].value["payload"]["threads"]) == ["answer", "fix"]
    api_head = sh("git", "-C", ws.repos["api"].path, "rev-parse", "AQS-1")
    out = answer(g, cfg, choice="approve")
    assert at(out) == "manual_retest"  # the fix changes behaviour, so you test again
    assert [c[1] for c in coder.calls if c[0] == "implement"] == ["web"]  # only the repo that must change
    assert not mcp.writes() and len(notes(tmp_path, "web", T_WEB)) == 1  # nothing outward before approve_push
    out = answer(g, cfg, choice="ok")
    assert at(out) == "approve_push"
    out = answer(g, cfg, choice="approve")
    assert "completed" in out["output"]
    web = ws.repos["web"].path  # the finished run's clean worktree is gone; the branch stays in the main clone
    assert not Path(wt(ws, "web")).exists()
    assert sh("git", "--git-dir", str(tmp_path / "origin/web.git"), "rev-parse", "AQS-1") == sh("git", "-C", web, "rev-parse", "AQS-1")
    assert "AQS-1: address review comments" in sh("git", "-C", web, "log", "-1", "--format=%B", "AQS-1")
    assert sh("git", "--git-dir", str(tmp_path / "origin/api.git"), "rev-parse", "AQS-1") == api_head  # api untouched
    web_reply, api_reply = notes(tmp_path, "web", T_WEB)[-1], notes(tmp_path, "api", T_API)[-1]
    assert "Fixed in web commit" in web_reply["body"] and "<!-- devflow:AQS-1-review:aa11 -->" in web_reply["body"]
    assert api_reply["body"].startswith("Product asked for it.")
    assert not any(n["resolved"] for r, t in (("web", T_WEB), ("api", T_API)) for n in notes(tmp_path, r, t))  # never resolved
    assert len(mcp.comments) == 1 and "Review update: 1 review thread(s) fixed, 1 answered" in mcp.comments[0]["body"]
    assert "[devflow:AQS-1-impl:delivery]" in mcp.comments[0]["body"]
    assert not any(c[0] == "jira_transition_issue" for c in mcp.calls)  # status untouched
    assert store.run("AQS-1-review")["status"] == "COMPLETED"


def test_abort_at_triage_changes_nothing(tmp_path):
    ws, g, cfg, out, store, mcp, coder = setup(tmp_path, [plan(T_WEB, "web", "fix")], repos=("web",))
    before = glab_db(tmp_path)
    out = answer(g, cfg, choice="abort")
    assert "aborted" in out["output"] and store.run("AQS-1-review")["status"] == "ABORTED"
    assert glab_db(tmp_path) == before and not mcp.writes() and not coder.calls
    assert not sh("git", "-C", ws.repos["web"].path, "status", "--porcelain")


def test_answer_only_needs_no_code_push_or_retest(tmp_path):
    ws, g, cfg, out, store, mcp, coder = setup(tmp_path, [plan(T_WEB, "web", "fix")], repos=("web",))
    out = answer(g, cfg, choice="edit", answer=["1"])
    assert "completed" in out["output"] and not coder.calls
    assert notes(tmp_path, "web", T_WEB)[-1]["author"]["username"] == "dev"
    assert "Fixed in" not in notes(tmp_path, "web", T_WEB)[-1]["body"]


def test_replies_are_not_duplicated_after_a_crash(tmp_path):
    ws, g, cfg, out, store, mcp, coder = setup(tmp_path, [plan(T_WEB, "web", "fix", behaviour=False)], repos=("web",))
    out = answer(g, cfg, choice="approve")
    assert at(out) == "approve_push"  # rename-only fix: no manual re-test
    import dev_workflows.jira_implement.jira as jira_mod
    orig = jira_mod.JiraGateway.refresh_delivery_comment
    calls = {"n": 0}

    def crash_once(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("killed")
        return orig(self, *a, **k)

    jira_mod.JiraGateway.refresh_delivery_comment = crash_once
    try:
        with pytest.raises(RuntimeError):
            answer(g, cfg, choice="approve")
        out = g.invoke(None, cfg)
    finally:
        jira_mod.JiraGateway.refresh_delivery_comment = orig
    assert "completed" in out["output"]
    assert len(notes(tmp_path, "web", T_WEB)) == 2  # the reviewer's note + exactly one reply
    assert len(mcp.comments) == 1


def test_fix_budget_pauses_at_three_for_this_run(tmp_path):
    ws, g, cfg, out, store, mcp, coder = setup(tmp_path, [plan(T_WEB, "web", "fix")], repos=("web",), checks={"web": "false"})
    out = answer(g, cfg, choice="approve")
    assert at(out) == "budget_exhausted"
    assert len([c for c in coder.calls if c[0] == "implement"]) == 4  # first fix + 3 attempts
    assert g.get_state(cfg).values["repos"]["web"]["fix_attempts_used"] == 3


def test_no_open_mr_completes_without_changes(tmp_path):
    ws, env = make_env(tmp_path, repos=("web",))
    llm = FakeLLM(["web"], [], overrides={ReviewTriage: [ReviewTriage(items=[])]})
    deps, store, mcp = make_deps(tmp_path, ws, env, llm, FakeCoder())
    g = build_review_graph(deps, checkpointer=SqliteSaver(sqlite3.connect(str(tmp_path / "runs.sqlite"), check_same_thread=False)))
    store.create_run("r", "address_review", "AQS-1")
    out = g.invoke({"run_id": "r", "ticket_key": "AQS-1", "requested_repos": ["web"]}, {"configurable": {"thread_id": "r"}})
    assert "no open MR" in out["output"] and store.run("r")["status"] == "COMPLETED"


def test_threads_we_already_answered_are_not_picked_up_again(tmp_path):
    ws, g, cfg, out, store, mcp, coder = setup(tmp_path, [plan(T_WEB, "web", "answer", reply="Because.")], repos=("web",))
    answer(g, cfg, choice="approve")
    assert notes(tmp_path, "web", T_WEB)[-1]["body"].startswith("Because.")
    store.create_run("AQS-1-review2", "address_review", "AQS-1")
    out = g.invoke({"run_id": "AQS-1-review2", "ticket_key": "AQS-1", "requested_repos": ["web"]}, {"configurable": {"thread_id": "AQS-1-review2"}})
    assert "no unresolved review threads" in out["output"]
