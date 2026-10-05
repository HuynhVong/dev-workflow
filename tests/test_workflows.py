import subprocess

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from dev_workflows.workflows import pr_review, standup, ticket_to_plan as tp
from fake_llm import FakeLLM


def _analysis(questions=()):
    return tp.TicketAnalysis(
        summary="Add CSV export to the orders page", kind="feature", areas=["frontend", "backend"],
        acceptance_criteria=["User can download filtered orders as CSV"], open_questions=list(questions),
    )


def _plan(size="M"):
    return tp.ImplementationPlan(
        steps=[tp.PlanStep(title="Add /orders/export endpoint", area="backend", details="Stream CSV", likely_files=["api/orders.py"])],
        api_changes=["GET /orders/export"], db_migrations=[], test_plan=["Export respects filters"],
        risks=["Large exports"], assumptions=[], size=size,
    )


def test_plan_pauses_for_questions_then_resumes():
    llm = FakeLLM({tp.TicketAnalysis: _analysis(["Max rows?"]), tp.ImplementationPlan: _plan(),
                   tp.PlanCritique: tp.PlanCritique(approved=True, issues=[])})
    g = tp.build_graph(llm, checkpointer=InMemorySaver())
    cfg = {"configurable": {"thread_id": "t1"}}
    first = g.invoke({"ticket": "Export orders"}, cfg)
    assert first["__interrupt__"][0].value == {"questions": ["Max rows?"]}
    done = g.invoke(Command(resume="Q: Max rows?\nA: 50k"), cfg)
    assert "# Plan: Add CSV export" in done["output"]
    assert "50k" in [p for s, p in llm.calls if s is tp.ImplementationPlan][0]


def test_plan_revises_until_approved_and_caps_revisions():
    llm = FakeLLM({tp.TicketAnalysis: _analysis(), tp.ImplementationPlan: _plan(),
                   tp.PlanCritique: tp.PlanCritique(approved=False, issues=["No pagination test"])})
    out = tp.build_graph(llm).invoke({"ticket": "Export orders", "interactive": False})
    assert sum(s is tp.ImplementationPlan for s, _ in llm.calls) == 1 + tp.MAX_REVISIONS
    assert "No pagination test" in out["output"]


def test_pr_review_fans_out_and_sorts_findings():
    triage = pr_review.Triage(summary="Adds search", touches_frontend=True, risk="high", lenses=["performance"])
    finding = lambda sev: pr_review.LensReview(findings=[pr_review.Finding(
        severity=sev, file="api/search.py", line=10, title=f"{sev} issue", detail="d", suggestion="s")])
    llm = FakeLLM({pr_review.Triage: triage,
                   pr_review.LensReview: [finding("nit"), finding("blocker"), finding("minor"), finding("major"), finding("nit")],
                   pr_review.Verdict: pr_review.Verdict(decision="request_changes", summary="Fix the blocker.")})
    out = pr_review.build_graph(llm).invoke({"title": "Search", "diff": "+code", "findings": []})
    lenses = {p.split("through the ")[1].split(" lens")[0] for s, p in llm.calls if s is pr_review.LensReview}
    assert lenses == {"correctness", "tests", "frontend", "security", "performance"}
    assert len(out["findings"]) == 5
    assert out["output"].index("[blocker]") < out["output"].index("[nit]")


def test_standup_reads_git(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVFLOW_VCS_PREFIX", "")
    repo = tmp_path / "r"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    run("init", "-q")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-qm", "Add order export endpoint")
    llm = FakeLLM({standup.Standup: standup.Standup(yesterday=["Shipped order export API"], today=["Frontend button"], blockers=[])})
    out = standup.build_graph(llm).invoke({"repo_paths": [str(repo)], "since": "1 week ago"})
    assert "Add order export endpoint" in llm.calls[0][1]
    assert "- Shipped order export API" in out["output"] and "**Blockers**\n- None" in out["output"]
