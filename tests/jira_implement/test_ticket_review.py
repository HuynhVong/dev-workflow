import json
import sqlite3
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from dev_workflows import doctor
from dev_workflows.jira_implement import ticket_review
from dev_workflows.jira_implement.graph import PreflightFailed
from dev_workflows.jira_implement.jira import JiraGateway
from dev_workflows.jira_implement.mcp_config import claude_code_mcp_servers, resolve_jira_server
from dev_workflows.jira_implement.models import (CommentDraft, Coverage, CriterionCoverage, TestCase, TestPlanDraft,
                                                 TicketUnderstanding)
from dev_workflows.jira_implement.workspace import DEFAULT_CONFLUENCE_READ_TOOLS, DEFAULT_JIRA_TOOLS, load_workspace
from dev_workflows.workflows.pr_review import Finding, LensReview, Triage, Verdict

from .harness import FakeCoder, FakeLLM, FakeMcp, make_deps, make_env, sh

KEY, RUN = "AQS-1", "AQS-1-qa-1"


def case(title, mode="auto"):
    return TestCase(title=title, criterion="CSV respects filters", preconditions=[], steps=["open /orders", "export"],
                    expected="CSV has only filtered rows", mode=mode, needs_you_reason="enter the OTP" if mode == "needs_you" else "",
                    proof=["the downloaded CSV"])


class FakeE2E:
    """Plays the Claude Code + Playwright agent: saves screenshots like browser_take_screenshot would."""

    def __init__(self, evidence, script):
        self.evidence, self.script, self.calls = Path(evidence), script, []

    def test_case(self, repos, instructions, step="ticket_review.e2e"):
        cid = instructions.split("Test case ")[1].split(" ")[0]
        self.calls.append((cid, instructions, step))
        status = self.script.get(cid, ["passed"])
        status = status.pop(0) if len(status) > 1 else status[0]
        if status == "needs_human":
            return {"status": status, "summary": "stopped at the OTP screen", "steps": [], "screenshots": [], "needs_human": "Give me the OTP"}
        (self.evidence / f"{cid}-1.png").write_bytes(b"\x89PNG fake")
        return {"status": status, "summary": f"{cid} {status}", "steps": [{"step": "export", "ok": status == "passed", "note": ""}],
                "screenshots": [f"{cid}-1.png"], "needs_human": ""}


def with_playwright(tmp_path):
    raw = json.loads((tmp_path / "workspace.yaml").read_text())
    raw["mcp_servers"] = {"playwright": {"command": "npx", "args": ["@playwright/mcp@latest"]}}
    raw["repos"]["web"]["app_url"] = "http://localhost:5173"
    (tmp_path / "workspace.yaml").write_text(json.dumps(raw))
    return load_workspace(tmp_path / "workspace.yaml")


def setup(tmp_path, script=None, mcp_tools=None, refuse=(), cases=None, checkout=True, findings=()):
    ws, env = make_env(tmp_path, repos=("web",))
    ws = with_playwright(tmp_path)
    web = ws.repos["web"].path
    sh("git", "-C", web, "checkout", "-q", "-b", KEY)
    Path(web, "export.ts").write_text("export const csv = () => rows.filter(f)\n")
    sh("git", "-C", web, "add", ".")
    sh("git", "-C", web, "commit", "-qm", f"{KEY}: CSV export")
    sha = sh("git", "-C", web, "rev-parse", "HEAD")
    sh("git", "-C", web, "push", "-q", "-u", "origin", KEY)
    if not checkout:
        sh("git", "-C", web, "checkout", "-q", "develop")
    llm = FakeLLM(["web"], [], overrides={
        TicketUnderstanding: [TicketUnderstanding(summary="Export orders as CSV", requirement=["filters apply"],
                                                  acceptance_criteria=["CSV respects filters"], out_of_scope=[], unclear=[])],
        Triage: [Triage(summary="adds export", touches_frontend=True, risk="medium", lenses=[])],
        LensReview: [LensReview(findings=list(findings))],
        Verdict: [Verdict(decision="request_changes" if findings else "approve", summary="looks fine")],
        Coverage: [Coverage(criteria=[CriterionCoverage(criterion="CSV respects filters", status="met", evidence="export.ts filters rows")],
                            unrelated_changes=[])],
        TestPlanDraft: [TestPlanDraft(cases=cases or [case("Filtered export", "needs_you"), case("Empty export")], not_testable=[])],
        CommentDraft: [CommentDraft(conclusion="ready", summary="The commits implement the export and the tests pass.")],
    })
    tools = mcp_tools if mcp_tools is not None else list(ws.jira_tools.values()) + list(ws.confluence_tools.values())
    mcp = FakeMcp(tools, refuse=refuse)
    deps, store, _ = make_deps(tmp_path, ws, env, llm, FakeCoder(), mcp=mcp)
    e2e = {}

    def e2e_agent(state, evidence, profile):
        e2e.setdefault("agent", FakeE2E(evidence, dict(script or {})))
        e2e["profile"] = profile
        return e2e["agent"]
    deps.e2e_agent = e2e_agent
    saver = SqliteSaver(sqlite3.connect(str(tmp_path / "runs.sqlite"), check_same_thread=False))
    g = ticket_review.build_graph(deps, checkpointer=saver)
    cfg = {"configurable": {"thread_id": RUN}}
    store.create_run(RUN, ticket_review.WORKFLOW, KEY)
    start = {"run_id": RUN, "ticket_key": KEY, **ticket_review.inputs({"commits": [f"web={sha[:9]}"]}, ws)}
    return ws, g, cfg, start, store, mcp, llm, e2e, sha


def at(out):
    return out["__interrupt__"][0].value["name"] if "__interrupt__" in out else None


def payload(out):
    return out["__interrupt__"][0].value["payload"]


def answer(g, cfg, **ans):
    return g.invoke(Command(resume=ans), cfg)


def git_log(tmp_path):
    p = Path(str(tmp_path / "glab.json") + ".log")
    return p.read_text() if p.exists() else ""


def test_full_review_checks_out_tests_with_your_help_and_posts_after_approval(tmp_path):
    ws, g, cfg, start, store, mcp, llm, e2e, sha = setup(tmp_path, script={"TC1": ["needs_human", "passed"]}, checkout=False,
                                                         findings=[Finding(severity="major", file="export.ts", line=1, title="No row limit",
                                                                           detail="50k rows", suggestion="cap it")])
    web = ws.repos["web"].path
    out = g.invoke(start, cfg)
    assert at(out) == "checkout_gate"
    p = payload(out)["repos"]["web"]
    assert p["branches_with_commits"] == [f"origin/{KEY}"] and f"rtk git checkout {KEY}" in p["commands"]
    assert payload(out)["app_url"] == "http://localhost:5173"  # from workspace.yaml

    out = answer(g, cfg, choice="ready")  # still on develop: devflow says so and asks again, never checks out itself
    assert at(out) == "checkout_gate"
    assert any("not in the checked-out branch develop" in i["problem"] for i in payload(out)["issues"])
    assert sh("git", "-C", web, "rev-parse", "--abbrev-ref", "HEAD") == "develop"

    sh("git", "-C", web, "checkout", "-q", KEY)
    out = answer(g, cfg, choice="ready", app_url="http://localhost:3000")
    assert at(out) == "approve_test_plan"
    cases = payload(out)["cases"]
    assert [c["id"] for c in cases] == ["TC1", "TC2"] and cases[0]["mode"] == "needs_you"
    assert payload(out)["coverage"]["criteria"][0]["status"] == "met"
    assert sha in "".join(prompt for _, prompt in llm.calls if "Triage this PR" in prompt)  # the listed commit was reviewed
    assert not mcp.writes()

    out = answer(g, cfg, choice="approve", run=["TC1"], note="Test user: qa@acme.io")
    assert at(out) == "human_step" and payload(out)["ask"] == "Give me the OTP"
    out = answer(g, cfg, choice="continue", note="OTP 123456")
    assert at(out) == "review_results"
    agent = e2e["agent"]
    assert [c[0] for c in agent.calls] == ["TC1", "TC1"]  # TC2 was unticked
    assert "OTP 123456" in agent.calls[1][1] and "qa@acme.io" in agent.calls[1][1] and "http://localhost:3000" in agent.calls[1][1]
    res = payload(out)["results"]
    assert res[0]["status"] == "passed" and res[0]["screenshots"][0].endswith("evidence/TC1-1.png")
    assert e2e["profile"].endswith("playwright-profile")

    out = answer(g, cfg, choice="accept")
    assert at(out) == "approve_comment"
    pc = payload(out)
    assert pc["will_post"] and len(pc["attachments"]) == 1
    for text in ("Ticket review: AQS-1 Export orders as CSV", "CSV respects filters", "[major] export.ts:1 No row limit",
                 f"`{sha[:9]}` on `{KEY}`", f"{RUN}-TC1-1.png", "✅ passed"):
        assert text in pc["comment"], text
    assert not mcp.writes()  # nothing reaches Jira before you approve the exact comment

    out = answer(g, cfg, choice="edit", comment=pc["comment"] + "\n\nChecked on staging data too.")
    assert at(out) == "approve_comment" and payload(out)["comment"].endswith("Checked on staging data too.")
    out = answer(g, cfg, choice="approve")
    assert "Posted to Jira (added); 1 screenshot(s) attached." in out["output"]
    assert mcp.attachments == [f"{RUN}-TC1-1.png"]
    assert len(mcp.comments) == 1 and "Checked on staging data too." in mcp.comments[0]["body"]
    assert f"[devflow:{RUN}:ticket-review]" in mcp.comments[0]["body"]
    assert not any(c[0] == "jira_transition_issue" for c in mcp.calls)
    assert store.run(RUN)["status"] == "COMPLETED"
    assert Path(ws.state_dir, RUN, "jira-comment.md").read_text().startswith("## Ticket review")
    # read only on your clone and GitLab: same branch, same commit, nothing pushed, glab never called
    assert sh("git", "-C", web, "rev-parse", "HEAD") == sha
    assert sh("git", "--git-dir", str(tmp_path / "origin/web.git"), "rev-parse", KEY) == sha
    assert git_log(tmp_path) == ""


def test_retest_runs_failed_cases_again(tmp_path):
    ws, g, cfg, start, store, mcp, llm, e2e, sha = setup(tmp_path, script={"TC1": ["needs_human"], "TC2": ["failed", "passed"]})
    g.invoke(start, cfg)
    answer(g, cfg, choice="ready")
    out = answer(g, cfg, choice="approve")
    assert at(out) == "human_step"  # TC1 needs you
    out = answer(g, cfg, choice="skip", note="no OTP device today")
    assert at(out) == "review_results"
    assert payload(out)["counts"] == {"passed": 0, "failed": 1, "skipped": 1}
    out = answer(g, cfg, choice="retest", retest=["TC2"])
    assert at(out) == "review_results"
    assert {r["id"]: r["status"] for r in payload(out)["results"]} == {"TC1": "skipped", "TC2": "passed"}


def test_read_only_jira_saves_the_comment_for_you_to_post(tmp_path):
    read_only = [t for k, t in DEFAULT_JIRA_TOOLS.items() if k not in ("add_comment", "edit_comment", "attach", "transition_issue")]
    ws, g, cfg, start, store, mcp, llm, e2e, sha = setup(tmp_path, mcp_tools=read_only + list(DEFAULT_CONFLUENCE_READ_TOOLS.values()),
                                                         cases=[case("Filtered export")])
    out = g.invoke(start, cfg)
    assert any("Jira posting is off" in w for w in payload(out)["warnings"])
    answer(g, cfg, choice="ready")
    answer(g, cfg, choice="approve")
    out = answer(g, cfg, choice="accept")
    assert not payload(out)["will_post"] and "post by hand" in payload(out)["hint"]
    out = answer(g, cfg, choice="approve")
    assert "Not posted to Jira" in out["output"] and not mcp.writes()
    assert Path(ws.state_dir, RUN, "jira-comment.md").exists()
    assert list(Path(ws.state_dir, RUN, "upload").iterdir())
    run = store.run(RUN)
    assert run["status"] == "COMPLETED" and "not posted to Jira" in run["detail"]


def test_jira_refusing_the_comment_falls_back_to_a_local_copy(tmp_path):
    ws, g, cfg, start, store, mcp, llm, e2e, sha = setup(tmp_path, refuse={"jira_add_comment"}, cases=[case("Filtered export")])
    g.invoke(start, cfg)
    answer(g, cfg, choice="ready")
    answer(g, cfg, choice="approve")
    answer(g, cfg, choice="accept")
    out = answer(g, cfg, choice="approve")
    assert "Not posted to Jira: Jira refused the comment" in out["output"] and "403" in out["output"]
    assert not mcp.comments and Path(ws.state_dir, RUN, "jira-comment.md").exists()
    assert store.run(RUN)["status"] == "COMPLETED"


def test_refused_upload_still_posts_the_comment_and_names_the_screenshots(tmp_path):
    ws, g, cfg, start, store, mcp, llm, e2e, sha = setup(tmp_path, refuse={"jira_update_issue"}, cases=[case("Filtered export")])
    g.invoke(start, cfg)
    answer(g, cfg, choice="ready")
    answer(g, cfg, choice="approve")
    answer(g, cfg, choice="accept")
    out = answer(g, cfg, choice="approve")
    assert "Jira refused the upload" in out["output"]
    assert len(mcp.comments) == 1 and f"{RUN}-TC1-1.png" in mcp.comments[0]["body"] and not mcp.attachments


def test_abort_at_the_comment_posts_nothing(tmp_path):
    ws, g, cfg, start, store, mcp, llm, e2e, sha = setup(tmp_path, cases=[case("Filtered export")])
    g.invoke(start, cfg)
    answer(g, cfg, choice="ready")
    answer(g, cfg, choice="approve")
    answer(g, cfg, choice="accept")
    out = answer(g, cfg, choice="abort", note="not today")
    assert "aborted at approve_comment" in out["output"] and not mcp.writes()
    assert store.run(RUN)["status"] == "ABORTED"


def test_preflight_lists_every_gap(tmp_path):
    ws, g, cfg, start, store, mcp, llm, e2e, sha = setup(tmp_path)
    bad = {**start, "commits": {"web": ["not-a-sha"], "mobile": ["abc1234"]}}
    with pytest.raises(PreflightFailed) as e:
        g.invoke(bad, cfg)
    gaps = "\n".join(e.value.gaps)
    assert "'not-a-sha' is not a commit SHA" in gaps and "'mobile' is not a repo in workspace.yaml" in gaps


def test_commit_parsing_accepts_cli_and_form_shapes():
    assert ticket_review.parse_commits(["web=abc1234", "api=88be0d4,a17c3e9", "web=fff0000"]) == {
        "web": ["abc1234", "fff0000"], "api": ["88be0d4", "a17c3e9"]}
    assert ticket_review.parse_commits({"web": "abc1234 def5678"}) == {"web": ["abc1234", "def5678"]}


def test_setup_requires_a_jira_mcp_and_reports_what_it_may_do(tmp_path):
    ws, _ = make_env(tmp_path, repos=("web",))
    fail = doctor.jira_access_checks(ws, None)
    assert fail[0].status == "fail" and fail[0].blocking and "claude mcp add" in fail[0].fix
    full = doctor.jira_access_checks(ws, JiraGateway(FakeMcp(ws.jira_tools.values()), ws.jira_tools, ws.status_order), ticket=KEY)
    assert [c.status for c in full] == ["ok", "ok", "ok", "ok", "ok"]
    ro = FakeMcp([ws.jira_tools["get_issue"], ws.jira_tools["download_attachments"]])
    checks = {c.id: c for c in doctor.jira_access_checks(ws, JiraGateway(ro, ws.jira_tools, ws.status_order))}
    assert checks["mcp.jira.read"].status == "ok"
    assert checks["mcp.jira.comment"].status == "warn" and "post by hand" in checks["mcp.jira.comment"].detail
    assert checks["mcp.jira.attach"].status == "warn"
    assert checks["mcp.jira.search"].status == "warn" and "standup" in checks["mcp.jira.search"].detail


def test_jira_server_comes_from_claude_code_when_workspace_has_none(tmp_path):
    ws, _ = make_env(tmp_path, repos=("web",))
    cc = tmp_path / "claude.json"
    cc.write_text(json.dumps({"mcpServers": {"github": {"command": "gh-mcp"}},
                              "projects": {"/x": {"mcpServers": {"my-jira": {"type": "http", "url": "https://mcp.acme.io/jira",
                                                                             "headers": {"Authorization": "Bearer t"}}}}}}))
    assert set(claude_code_mcp_servers([cc])) == {"github", "my-jira"}
    name, config, source = resolve_jira_server(ws, files=[cc])
    assert name == "my-jira" and config == {"url": "https://mcp.acme.io/jira", "headers": {"Authorization": "Bearer t"}}
    assert source == str(cc)
    assert resolve_jira_server(ws, files=[tmp_path / "missing.json"]) is None
