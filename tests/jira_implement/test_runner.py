import io
import subprocess

from dev_workflows.jira_implement.confluence import ConfluenceReader
from dev_workflows.jira_implement.graph import Deps
from dev_workflows.jira_implement.jira import JiraGateway
from dev_workflows.jira_implement.runner import Session

from .harness import FakeCoder, FakeLLM, FakeMcp, glab_db, make_env, no_skills


def session(tmp_path, ws, env, llm, coder, which=None):
    mcp = FakeMcp(list(ws.jira_tools.values()) + list(ws.confluence_tools.values()))

    def factory(ws_, store):
        return Deps(workspace=ws_, store=store, llm=llm, jira=JiraGateway(mcp, ws_.jira_tools, ws_.status_order),
                    confluence=ConfluenceReader(mcp, ws_.confluence_tools), coder_factory=lambda s: coder,
                    vcs_runner=lambda cmd, cwd: subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, env=env),
                    cmd_runner=lambda cmd, cwd, extra=None: subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, env={**env, **(extra or {})}),
                    which=which or (lambda n: f"/usr/bin/{n}"), routing=no_skills())
    out = io.StringIO()
    return Session(ws, deps_factory=factory, ask=None, out=out), out, mcp


def test_preflight_failure_is_resumable(tmp_path):
    ws, env = make_env(tmp_path, repos=("api",))
    installed = {"rtk": None}
    s, out, _ = session(tmp_path, ws, env, FakeLLM(["api"], []), FakeCoder(), which=lambda n: installed.get(n, f"/usr/bin/{n}"))
    assert s.start("AQS-1", ["api"]) == "FAILED"
    run_id = s.store.runs()[0]["run_id"]
    assert "'rtk' is not installed" in out.getvalue()
    installed["rtk"] = "/usr/bin/rtk"
    assert s.resume(run_id) == "WAITING_HUMAN"
    assert s.status(run_id)["pending_checkpoint"] == "approve_plan"


def test_answer_abort_and_reopen(tmp_path):
    ws, env = make_env(tmp_path, repos=("api",))
    s, out, mcp = session(tmp_path, ws, env, FakeLLM(["api"], []), FakeCoder())
    assert s.start("AQS-1", ["api"]) == "WAITING_HUMAN"
    run_id = s.store.runs()[0]["run_id"]
    assert s.answer(run_id, {"choice": "approve"}) == "WAITING_HUMAN"  # now at manual_test
    assert s.abort(run_id, "pausing for a meeting") == "ABORTED"
    assert s.resume(run_id) == "ABORTED"  # terminal unless reopened explicitly
    assert s.resume(run_id, reopen=True) == "WAITING_HUMAN"
    assert s.status(run_id)["pending_checkpoint"] == "manual_test"
    assert s.answer(run_id, {"choice": "ok"}) == "WAITING_HUMAN"
    assert s.answer(run_id, {"choice": "approve"}) == "COMPLETED"
    kinds = [a["kind"] for a in s.show(run_id)["audit"]]
    assert "reopened" in kinds and glab_db(tmp_path)["api"][0]["draft"]


def test_invalid_choice_is_rejected_and_run_stays_resumable(tmp_path):
    ws, env = make_env(tmp_path, repos=("api",))
    s, out, _ = session(tmp_path, ws, env, FakeLLM(["api"], []), FakeCoder())
    s.start("AQS-1", ["api"])
    run_id = s.store.runs()[0]["run_id"]
    assert s.answer(run_id, {"choice": "merge-it"}) == "WAITING_HUMAN"  # rejected before reaching the graph
    assert "choose one of ['approve', 'revise', 'abort']" in out.getvalue()
    # an invalid answer that reaches the graph directly is asked again, never treated as a choice
    from langgraph.types import Command
    res = s.graph(run_id).invoke(Command(resume={"choice": "merge-it"}), s.cfg(run_id))
    assert res["__interrupt__"][0].value["error"].startswith("'merge-it' is not one of")
    assert s.status(run_id)["pending_checkpoint"] == "approve_plan"


def test_address_review_runs_through_the_same_session_commands(tmp_path):
    from dev_workflows.jira_implement.address_review import WORKFLOW as REVIEW
    from dev_workflows.jira_implement.models import ReviewTriage, ThreadPlan

    from .harness import seed_review
    ws, env = make_env(tmp_path, repos=("web",))
    seed_review(tmp_path, ws, threads=[("web", "aa11", "Why?")])
    item = ThreadPlan(thread_id="aa11", category="question", repo="web", summary="why", proposed_action="answer", fix_instructions="",
                      reply="Because.", behaviour_change=False, contract_change=False)
    s, out, _ = session(tmp_path, ws, env, FakeLLM(["web"], [], overrides={ReviewTriage: [ReviewTriage(items=[item])]}), FakeCoder())
    assert s.start("AQS-1", ["web"], workflow=REVIEW) == "WAITING_HUMAN"
    run_id = s.store.runs()[0]["run_id"]
    assert "-review-" in run_id and s.status(run_id)["pending_checkpoint"] == "triage"
    assert s.answer(run_id, {"choice": "approve"}) == "COMPLETED"
