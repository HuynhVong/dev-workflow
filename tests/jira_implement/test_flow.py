import sqlite3

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from dev_workflows.jira_implement.graph import build_graph

from .harness import FakeCoder, FakeLLM, glab_db, make_deps, make_env, sh


def start(tmp_path, ws, deps, run_id="AQS-1-run", repos=None):
    saver = SqliteSaver(sqlite3.connect(str(tmp_path / "runs.sqlite"), check_same_thread=False))
    g = build_graph(deps, checkpointer=saver)
    cfg = {"configurable": {"thread_id": run_id}}
    deps.store.create_run(run_id, "jira_ticket_implement", "AQS-1")
    out = g.invoke({"run_id": run_id, "ticket_key": "AQS-1", "requested_repos": repos or list(ws.repos)}, cfg)
    return g, cfg, out


def at(out):
    return out["__interrupt__"][0].value["name"] if "__interrupt__" in out else None


def answer(g, cfg, **ans):
    return g.invoke(Command(resume=ans), cfg)


def test_happy_path_two_repos_in_dependency_order(tmp_path):
    ws, env = make_env(tmp_path)
    llm, coder = FakeLLM(["api", "web"], [("api", "web")]), FakeCoder()
    deps, store, mcp = make_deps(tmp_path, ws, env, llm, coder)
    g, cfg, out = start(tmp_path, ws, deps)
    assert at(out) == "approve_plan"
    assert out["__interrupt__"][0].value["payload"]["dag"]["waves"] == [["api"], ["web"]]
    assert store.run("AQS-1-run")["status"] == "WAITING_HUMAN"
    out = answer(g, cfg, choice="approve")
    assert at(out) == "manual_test"  # integration not required -> straight to the mandatory manual test
    assert llm.steps[:4] == ["gather_context", "analyze_requirements", "change_impact", "plan_implementation"]
    impl = [c[1] for c in coder.calls if c[0] == "implement"]
    assert impl == ["api", "web"]  # api (upstream) strictly before web
    web_prompt = [c[2] for c in coder.calls if c[0] == "implement" and c[1] == "web"][0]
    assert "upstream_repo name='api'" in web_prompt
    for r in ("api", "web"):  # branch = bare ticket key from develop, nothing committed yet
        path = ws.repos[r].path
        assert sh("git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD") == "AQS-1"
        assert sh("git", "-C", path, "status", "--porcelain")
        assert sh("git", "-C", path, "config", "--get", "branch.AQS-1.devflow-run") == "AQS-1-run"
    assert not glab_db(tmp_path) and not mcp.writes()
    out = answer(g, cfg, choice="ok")
    assert at(out) == "approve_push"
    out = answer(g, cfg, choice="approve")
    assert "__interrupt__" not in out and "completed" in out["output"]
    db = glab_db(tmp_path)
    assert all(m["draft"] for r in db for m in db[r]) and set(db) == {"api", "web"}
    assert "Merge order:** api → web" in db["web"][0]["description"] and "merge_requests/1" in db["web"][0]["description"]
    assert mcp.status == "Code Review" and len(mcp.comments) == 1
    assert store.run("AQS-1-run")["status"] == "COMPLETED"
    origin_head = sh("git", "--git-dir", str(tmp_path / "origin/api.git"), "rev-parse", "AQS-1")
    assert origin_head == sh("git", "-C", ws.repos["api"].path, "rev-parse", "HEAD")
    msg = sh("git", "-C", ws.repos["api"].path, "log", "-1", "--format=%B")
    assert msg.startswith("AQS-1: Export orders as CSV") and "Devflow-Run: AQS-1-run" in msg


def test_abort_at_plan_has_no_side_effects(tmp_path):
    ws, env = make_env(tmp_path)
    deps, store, mcp = make_deps(tmp_path, ws, env, FakeLLM(["api", "web"], []), FakeCoder())
    g, cfg, out = start(tmp_path, ws, deps)
    out = answer(g, cfg, choice="abort", note="wrong ticket")
    assert "aborted" in out["output"]
    assert store.run("AQS-1-run")["status"] == "ABORTED"
    assert not mcp.writes() and not glab_db(tmp_path)
    assert sh("git", "-C", ws.repos["api"].path, "branch", "--list", "AQS-1") == ""


def test_fix_budget_is_run_wide_and_pauses_at_three(tmp_path):
    ws, env = make_env(tmp_path, repos=("api",), checks={"api": "false"})
    coder = FakeCoder()
    deps, store, _ = make_deps(tmp_path, ws, env, FakeLLM(["api"], []), coder)
    g, cfg, out = start(tmp_path, ws, deps)
    out = answer(g, cfg, choice="approve")
    assert at(out) == "budget_exhausted"
    assert len([c for c in coder.calls if c[0] == "implement"]) == 4  # first implementation + 3 fix attempts
    # Sonnet for the first implementation and fixes; the last automated attempt escalates to the stronger model
    assert [(st, esc) for st, _, esc in coder.steps] == [("implement", False), ("targeted_fix", False), ("targeted_fix", False), ("targeted_fix", True)]
    state = g.get_state(cfg).values
    assert state["repos"]["api"]["fix_attempts_used"] == 3
    # you fix it by hand: checks re-run with no automated edit; still failing -> back to you, budget unchanged
    out = answer(g, cfg, choice="fixed_by_hand")
    assert at(out) == "budget_exhausted"
    assert len([c for c in coder.calls if c[0] == "implement"]) == 4


def test_manual_feedback_routes_only_to_affected_repo_and_retests(tmp_path):
    from dev_workflows.jira_implement.models import FeedbackAnalysis, FeedbackItem
    ws, env = make_env(tmp_path, repos=("api", "web", "batch"))
    fb = FeedbackAnalysis(items=[FeedbackItem(source="manual_test", category="manual_test_feedback", repos=["web"], cause="button label",
                                              confidence="high", fix_instructions="rename the button", contract_changed=False)])
    llm = FakeLLM(["api", "web", "batch"], [("api", "web")], overrides={FeedbackAnalysis: [fb]})
    coder = FakeCoder()
    deps, _, _ = make_deps(tmp_path, ws, env, llm, coder)
    g, cfg, out = start(tmp_path, ws, deps)
    out = answer(g, cfg, choice="approve")
    assert at(out) == "manual_test"
    before = len(coder.calls)
    out = answer(g, cfg, choice="feedback", note="the export button says 'Exprot'")
    assert at(out) == "manual_test"  # you must test again after the fix
    assert [c[1] for c in coder.calls[before:] if c[0] == "implement"] == ["web"]
    st = g.get_state(cfg).values
    assert st["repos"]["web"]["fix_attempts_used"] == 1 and st["repos"]["api"]["fix_attempts_used"] == 0


def test_resume_after_crash_does_not_duplicate_mrs_or_comments(tmp_path):
    ws, env = make_env(tmp_path, repos=("api",))
    llm, coder = FakeLLM(["api"], []), FakeCoder()
    deps, store, mcp = make_deps(tmp_path, ws, env, llm, coder)
    g, cfg, out = start(tmp_path, ws, deps)
    answer(g, cfg, choice="approve")
    answer(g, cfg, choice="ok")
    real = deps.jira.upsert_comment
    calls = {"n": 0}

    def crash_once(*a, **k):
        res = real(*a, **k)
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("process killed after the comment was posted")
        return res

    deps.jira.upsert_comment = crash_once
    try:
        answer(g, cfg, choice="approve")
        raise AssertionError("expected crash")
    except RuntimeError:
        pass
    out = g.invoke(None, cfg)  # devflow resume
    assert "completed" in out["output"]
    assert len(mcp.comments) == 1 and len(glab_db(tmp_path)["api"]) == 1
    assert sum(1 for c in mcp.calls if c[0] == "jira_transition_issue") == 1


def test_branch_from_another_run_needs_your_approval(tmp_path):
    ws, env = make_env(tmp_path, repos=("api",))
    path = ws.repos["api"].path
    sh("git", "-C", path, "checkout", "-q", "-b", "AQS-1")
    sh("git", "-C", path, "config", "branch.AQS-1.devflow-run", "AQS-1-oldrun")
    sh("git", "-C", path, "checkout", "-q", "develop")
    deps, store, _ = make_deps(tmp_path, ws, env, FakeLLM(["api"], []), FakeCoder())
    g, cfg, out = start(tmp_path, ws, deps)
    out = answer(g, cfg, choice="approve")
    assert at(out) == "branch_ownership"
    assert out["__interrupt__"][0].value["payload"]["issues"][0]["owner"] == "AQS-1-oldrun"
    out = answer(g, cfg, choice="reuse")
    assert at(out) == "manual_test"
    assert sh("git", "-C", path, "config", "--get", "branch.AQS-1.devflow-run") == "AQS-1-run"


def test_out_of_scope_repo_pauses_for_approval(tmp_path):
    from dev_workflows.jira_implement.models import ScopeNeed
    ws, env = make_env(tmp_path, repos=("api",), extra_repos=("shared",))
    llm = FakeLLM(["api"], [], out_of_scope=[ScopeNeed(repo="shared", reason="DTO lives there")])
    deps, store, _ = make_deps(tmp_path, ws, env, llm, FakeCoder())
    g, cfg, out = start(tmp_path, ws, deps, repos=["api"])
    assert at(out) == "clarify"
    assert out["__interrupt__"][0].value["payload"]["scope_requests"][0]["repo"] == "shared"
    out = answer(g, cfg, choice="proceed")  # not approved -> denied, logged, never touched
    assert at(out) == "approve_plan"
    st = g.get_state(cfg).values
    assert "shared" not in st["scope"] and st["scope_gaps"][0]["repo"] == "shared"


def test_integration_runs_repo_commands_before_any_ai(tmp_path):
    from dev_workflows.jira_implement.models import ContractCheck
    ws, env = make_env(tmp_path, repos=("api",))
    object.__setattr__(ws.repos["api"], "commands", {**ws.repos["api"].commands, "integration": "echo contract-tests-ok"})
    llm = FakeLLM(["api"], [], integration=True)
    deps, _, _ = make_deps(tmp_path, ws, env, llm, FakeCoder())
    g, cfg, out = start(tmp_path, ws, deps)
    out = answer(g, cfg, choice="approve")
    assert at(out) == "manual_test"
    report = g.get_state(cfg).values["integration_report"]
    assert report["commands"][0]["ok"] and "contracts" not in report
    assert ContractCheck not in [c[0] for c in llm.calls]  # the command covered it: no AI contract check


def test_missing_global_skills_are_a_warning_not_a_blocker(tmp_path):
    from dev_workflows.routing import Routing, SkillRegistry
    ws, env = make_env(tmp_path, repos=("api",))
    skills = tmp_path / "skills" / "planning"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("---\nname: planning\ndescription: plan\n---\nPlan.")
    deps, _, _ = make_deps(tmp_path, ws, env, FakeLLM(["api"], []), FakeCoder(), routing=Routing(registry=SkillRegistry([tmp_path / "skills"])))
    g, cfg, out = start(tmp_path, ws, deps)
    warnings = out["__interrupt__"][0].value["payload"]["warnings"]
    skill_warning = next(w for w in warnings if "skills not installed" in w)
    assert at(out) == "approve_plan"  # the run goes on; missing skills only warn
    assert "analyze_requirements: requirements-analysis" in skill_warning and "plan_implementation" not in skill_warning
