import json
import sqlite3
import threading
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from dev_workflows.jira_implement import worktrees
from dev_workflows.jira_implement.graph import PreflightFailed, build_graph
from dev_workflows.jira_implement.runner import RunConflict
from dev_workflows.jira_implement.vcs import VcsPolicyError, check_policy
from dev_workflows.jira_implement.workspace import load_workspace

from .harness import FakeCoder, FakeLLM, glab_db, make_deps, make_env, sh, wt
from .test_runner import session


def graph(tmp_path, deps):
    return build_graph(deps, checkpointer=SqliteSaver(sqlite3.connect(str(tmp_path / "runs.sqlite"), check_same_thread=False)))


def drive(g, deps, key, answers):
    run_id = f"{key}-run"
    cfg = {"configurable": {"thread_id": run_id}}
    deps.store.create_run(run_id, "jira_ticket_implement", key)
    out = g.invoke({"run_id": run_id, "ticket_key": key, "requested_repos": list(deps.workspace.repos)}, cfg)
    for a in answers:
        out = g.invoke(Command(resume={"choice": a}), cfg)
    return out


def test_three_tickets_on_the_same_repos_run_at_once_without_touching_each_other(tmp_path):
    ws, env = make_env(tmp_path)
    deps, store, _ = make_deps(tmp_path, ws, env, FakeLLM(["api", "web"], [("api", "web")]), FakeCoder())
    g = graph(tmp_path, deps)
    keys, outs, errors = ["AQS-1", "AQS-2", "AQS-3"], {}, []

    def go(key):
        try:
            outs[key] = drive(g, deps, key, ["approve", "ok", "approve"])
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=go, args=(k,)) for k in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    for key in keys:
        assert "completed" in outs[key]["output"]
        for repo in ("api", "web"):
            path = wt(ws, repo, key)
            assert sh("git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD") == key
            assert sh("git", "-C", path, "rev-parse", "HEAD") == sh("git", "--git-dir", str(tmp_path / f"origin/{repo}.git"), "rev-parse", key)
            assert f"Devflow-Run: {key}-run" in sh("git", "-C", path, "log", "-1", "--format=%B")
    for repo in ("api", "web"):  # the main clone was never switched or edited
        main = ws.repos[repo].path
        assert sh("git", "-C", main, "rev-parse", "--abbrev-ref", "HEAD") == "develop"
        assert not sh("git", "-C", main, "status", "--porcelain") and not Path(main, "feature.txt").exists()
    assert {m["source"] for m in glab_db(tmp_path)["api"]} == set(keys)


def test_main_clone_may_be_dirty_but_the_ticket_branch_must_not_be_checked_out_there(tmp_path):
    ws, env = make_env(tmp_path, repos=("api",))
    main = ws.repos["api"].path
    Path(main, "wip.txt").write_text("my own work in progress")
    deps, store, _ = make_deps(tmp_path, ws, env, FakeLLM(["api"], []), FakeCoder())
    g = graph(tmp_path, deps)
    out = drive(g, deps, "AQS-1", [])
    assert out["__interrupt__"][0].value["name"] == "approve_plan"
    assert Path(main, "wip.txt").exists() and not Path(wt(ws, "api"), "wip.txt").exists()

    sh("git", "-C", main, "checkout", "-q", "-b", "AQS-2")
    with pytest.raises(PreflightFailed) as e:
        drive(g, deps, "AQS-2", [])
    assert "branch AQS-2 is checked out in" in str(e.value)


def test_a_foreign_folder_at_the_worktree_path_is_a_preflight_gap(tmp_path):
    ws, env = make_env(tmp_path, repos=("api",))
    Path(wt(ws, "api")).mkdir(parents=True)
    Path(wt(ws, "api"), "notes.txt").write_text("not a worktree")
    deps, _, _ = make_deps(tmp_path, ws, env, FakeLLM(["api"], []), FakeCoder())
    with pytest.raises(PreflightFailed) as e:
        drive(graph(tmp_path, deps), deps, "AQS-1", [])
    assert "is not a worktree of api" in str(e.value)


def test_setup_commands_and_copied_files_run_once_and_checks_see_sibling_worktrees(tmp_path):
    ws, env = make_env(tmp_path)
    Path(ws.repos["web"].path, ".env.local").write_text("SECRET=1")  # untracked local file in the main clone
    raw = json.loads((tmp_path / "workspace.yaml").read_text())
    raw["repos"]["web"].update(copy_files=[".env.local"], setup_commands=["echo set >> setup.log"],
                               commands={"lint": 'test -f feature.txt && test -d "$DEVFLOW_WORKTREE_API"'})
    (tmp_path / "workspace.yaml").write_text(json.dumps(raw))
    ws = load_workspace(tmp_path / "workspace.yaml")
    deps, _, _ = make_deps(tmp_path, ws, env, FakeLLM(["api", "web"], [("api", "web")]), FakeCoder())
    g = graph(tmp_path, deps)
    out = drive(g, deps, "AQS-1", ["approve"])
    assert out["__interrupt__"][0].value["name"] == "manual_test"  # web's lint saw api's worktree
    web = wt(ws, "web")
    assert Path(web, ".env.local").read_text() == "SECRET=1"
    assert Path(web, "setup.log").read_text() == "set\n"
    assert out["__interrupt__"][0].value["payload"]["repos"]["web"]["path"] == web
    g.invoke(None, {"configurable": {"thread_id": "AQS-1-run"}})  # resuming never repeats the setup
    assert Path(web, "setup.log").read_text() == "set\n"


def test_one_unfinished_run_per_ticket(tmp_path):
    ws, env = make_env(tmp_path, repos=("api",))
    s, out, _ = session(tmp_path, ws, env, FakeLLM(["api"], []), FakeCoder())
    assert s.start("AQS-1", ["api"]) == "WAITING_HUMAN"
    with pytest.raises(RunConflict):
        s.start("AQS-1", ["api"])
    s.abort(s.store.runs()[0]["run_id"])
    import time
    time.sleep(1)  # run ids are per second
    assert s.start("AQS-1", ["api"]) == "WAITING_HUMAN"  # the worktree of the aborted run is reused


def test_worktree_policy_and_cleanup(tmp_path):
    check_policy("git", ["worktree", "add", "/x", "AQS-1"])
    check_policy("git", ["worktree", "list", "--porcelain"])
    for bad in (["worktree", "remove", "--force", "/x"], ["worktree", "prune"], ["worktree", "move", "/a", "/b"]):
        with pytest.raises(VcsPolicyError):
            check_policy("git", bad)

    ws, env = make_env(tmp_path, repos=("api",))
    deps, _, _ = make_deps(tmp_path, ws, env, FakeLLM(["api"], []), FakeCoder())
    drive(graph(tmp_path, deps), deps, "AQS-1", ["approve"])  # implemented, not committed: dirty
    runner = deps.vcs_runner
    [item] = worktrees.list_all(ws, runner)
    assert item["ticket"] == "AQS-1" and item["branch"] == "AQS-1" and item["dirty"] is True
    with pytest.raises(RuntimeError, match="uncommitted"):
        worktrees.remove(ws, "AQS-1", "api", runner)
    sh("git", "-C", wt(ws, "api"), "add", "-A")
    sh("git", "-C", wt(ws, "api"), "commit", "-qm", "wip")
    worktrees.remove(ws, "AQS-1", "api", runner)
    assert not Path(wt(ws, "api")).exists()
    assert sh("git", "-C", ws.repos["api"].path, "branch", "--list", "AQS-1")  # the branch is kept
