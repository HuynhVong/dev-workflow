"""Jira ticket implement: the LangGraph orchestration (design: docs/jira-ticket-implement-design.md).

Lifecycle lives in the run registry (PENDING/RUNNING/WAITING_HUMAN/FAILED/ABORTED/COMPLETED) and is
mirrored in the audit log. Every human checkpoint is two nodes: `<name>` records WAITING_HUMAN and the
payload, `<name>_wait` calls interrupt(). Abort is offered at every checkpoint and routes to `abort`,
which ends the graph before any later side effect.
"""
import contextlib
import fcntl
import json
import operator
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Callable

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy, Send, interrupt
from typing_extensions import NotRequired, TypedDict

from ..llm import AsStep, StructuredLLM
from ..routing import Routing
from . import dag as dagmod
from . import worktrees
from .coding_agent import ClaudeCodeAgent, CodingAgent
from .confluence import ConfluenceReader, page_ids_from_urls
from .jira import JiraGateway, marker
from .ledger import Effect, Store, perform
from .mcp_client import TransientToolError
from .mcp_config import playwright_servers, sql_servers
from .models import (Analysis, ContractCheck, ContractReview, FeedbackAnalysis, Impact, Outdated, Plan,
                     RequirementContext)
from .scope import ScopeGuard, ScopeViolation
from .vcs import Vcs, VcsError
from .workspace import Workspace

WORKFLOW = "jira_ticket_implement"
FIX_CATEGORIES = {"integration_failure", "e2e_failure", "manual_test_feedback", "review_blocker", "contract_issue", "test_issue"}
TRANSIENT = RetryPolicy(max_attempts=3, initial_interval=2.0, backoff_factor=2.0, retry_on=TransientToolError)

SYSTEM = (
    "You are a senior full-stack engineer on a product team working across several GitLab repositories. "
    "Be precise and grounded in the ticket, the Confluence requirement and the code evidence you are given. "
    "Never invent repositories, files or requirements."
)


TICKET_FORM = [
    {"name": "ticket", "label": "Jira ticket", "type": "ticket", "required": True, "placeholder": "AQS-5512"},
    {"name": "repos", "label": "Repos", "type": "repos", "help": "Hard allow-list. Empty means every repo in workspace.yaml."},
]
CHECKPOINT_TITLES = {
    "clarify": "Answer open questions", "approve_plan": "Approve the plan", "branch_ownership": "Reuse existing branches",
    "scope_request": "Approve a repo outside scope", "budget_exhausted": "Fix attempts used up", "manual_test": "Manual test",
    "route_ask": "Decide on feedback", "approve_push": "Approve push", "push_blocked": "Push blocked",
    "triage": "Triage review threads", "manual_retest": "Manual re-test", "sync_blocked": "Branch sync blocked",
}
HIDDEN_NODES = ["schedule", "apply_clarify", "apply_scope", "apply_route_ask", "apply_triage", "manual_ok", "manual_feedback",
                "mark_hand_fixed", "approve_branch_reuse", "plan_revise"]
DEVFLOW_UI = {
    "title": "Jira ticket implement",
    "description": "One Jira key to draft GitLab MRs across several repos, with your plan approval, manual test and push approval.",
    "icon": "git-pull-request", "color": "#3491ff", "form": TICKET_FORM, "checkpoints": CHECKPOINT_TITLES, "hidden_nodes": HIDDEN_NODES,
    "steps": ["preflight", "prepare_worktrees", "fetch_ticket", "gather_context", "analyze_requirements", "change_impact",
              "discover_repos", "plan_implementation", "approve_plan", "prepare_branches", "implement_repo", "integration_check",
              "manual_test", "repo_review", "contract_review", "approve_push", "commit_and_push", "open_draft_mrs", "jira_update",
              "summary"],
    "nodes": {
        "preflight": "Preflight", "prepare_worktrees": "Prepare worktrees", "fetch_ticket": "Fetch ticket",
        "gather_context": "Gather context", "analyze_requirements": "Analyze requirements", "change_impact": "Change impact",
        "clarify": "Clarify", "discover_repos": "Discover repos", "plan_implementation": "Plan implementation",
        "approve_plan": "Approve plan", "prepare_branches": "Prepare branches", "branch_ownership": "Branch ownership",
        "implement_repo": "Implement", "budget_exhausted": "Fix budget used", "scope_request": "Scope request",
        "integration_check": "Integration check", "manual_test": "Manual test", "repo_review": "Repo review",
        "contract_review": "Contract review", "analyze_feedback_and_route": "Route feedback", "route_ask": "Feedback decision",
        "approve_push": "Approve push", "commit_and_push": "Commit and push", "push_blocked": "Push blocked",
        "open_draft_mrs": "Open draft MRs", "jira_update": "Update Jira", "summary": "Summary", "abort": "Aborted",
    },
    "node_details": {
        "preflight": "Tools, MCP servers, repos and worktrees checked", "prepare_worktrees": "One worktree per repo for this ticket",
        "fetch_ticket": "Ticket, attachments and links from Jira", "gather_context": "Jira, Confluence (read only) and open MRs",
        "analyze_requirements": "Acceptance criteria and open questions", "change_impact": "Layers, repos, contracts and risk",
        "discover_repos": "Relevant code in each candidate repo", "plan_implementation": "Tasks per repo and the dependency DAG",
        "approve_plan": "Your decision", "prepare_branches": "Branch from a freshly fetched develop",
        "implement_repo": "Claude Code edits each repo, wave by wave", "integration_check": "Integration and E2E checks",
        "manual_test": "You test it locally", "repo_review": "Review of each repo's diff", "contract_review": "Cross-repo review",
        "approve_push": "Your decision", "commit_and_push": "Commit and push (never forced)", "open_draft_mrs": "Draft MRs to develop",
        "jira_update": "Code Review status and one delivery comment", "summary": "Final summary",
    },
}


class PreflightFailed(RuntimeError):
    def __init__(self, gaps: list[str]):
        self.gaps = gaps
        super().__init__("Preflight failed:\n- " + "\n- ".join(gaps))


def merge_repos(a: dict | None, b: dict | None) -> dict:
    out = dict(a or {})
    for k, v in (b or {}).items():
        out[k] = {**out.get(k, {}), **v}
    return out


def last(a, b):
    return b


class State(TypedDict, total=False):
    run_id: str
    ticket_key: str
    requested_repos: list[str]
    scope: dict[str, str]
    scope_gaps: Annotated[list[dict], operator.add]
    env_warnings: list[str]
    ticket: dict
    context: dict
    existing_mrs: dict
    analysis: dict
    impact: dict
    answers: Annotated[list[str], operator.add]
    clarify_proceed: bool
    scope_requests: list[dict]
    repo_findings: dict
    plan: dict
    plan_errors: list[str]
    plan_notes: Annotated[list[str], operator.add]
    dag: dict
    branch_issues: list[dict]
    branch_approvals: dict
    repos: Annotated[dict, merge_repos]
    code_version: Annotated[int, operator.add]
    integration_version: int
    manual_test_version: int
    review_version: int
    integration_report: dict
    reviews: dict
    contract_review: dict
    pending_feedback: list[dict]
    feedback_items: Annotated[list[dict], operator.add]
    route_ask_items: list[dict]
    pending_checkpoint: dict | None
    last_answer: dict
    decisions: Annotated[list[dict], operator.add]
    push_issues: list[dict]
    mrs: dict
    jira_result: dict
    output: str
    aborted_at: str


@dataclass
class Deps:
    workspace: Workspace
    store: Store
    llm: StructuredLLM
    jira: JiraGateway
    confluence: ConfluenceReader
    coder_factory: Callable[[ScopeGuard], CodingAgent] = field(default=None)  # type: ignore[assignment]
    vcs_runner: Callable | None = None
    cmd_runner: Callable[..., subprocess.CompletedProcess] | None = None  # (cmd, cwd, env=None)
    which: Callable[[str], str | None] = shutil.which
    routing: Routing = field(default_factory=Routing)

    def vcs(self, state: dict) -> Vcs:
        kw = {"runner": self.vcs_runner} if self.vcs_runner else {}
        return Vcs(ScopeGuard.from_state(state["scope"]), prefix=self.workspace.vcs_prefix, **kw)

    def coder(self, state: dict) -> CodingAgent:
        scope = ScopeGuard.from_state(state["scope"])
        if self.coder_factory:
            return self.coder_factory(scope)
        sql, upstream = sql_servers(self.workspace)
        return ClaudeCodeAgent(scope, routing=self.routing, mcp_servers={**playwright_servers(self.workspace), **sql},
                               sql_upstream=upstream)

    def e2e_agent(self, state: dict, evidence_dir: str, profile_dir: str) -> CodingAgent:
        """A Claude Code agent whose Playwright MCP saves screenshots in `evidence_dir` and keeps its browser profile
        (logins) in `profile_dir` between test cases."""
        scope = ScopeGuard.from_state(state["scope"])
        if self.coder_factory:
            return self.coder_factory(scope)
        playwright = {k: playwright_config(v, evidence_dir, profile_dir) for k, v in playwright_servers(self.workspace).items()}
        sql, upstream = sql_servers(self.workspace)
        return ClaudeCodeAgent(scope, routing=self.routing, mcp_servers={**playwright, **sql}, sql_upstream=upstream)

    def run_cmd(self, cmd: str, cwd: str, env: dict | None = None) -> subprocess.CompletedProcess:
        """`env` adds variables (e.g. DEVFLOW_WORKTREE_<REPO>) on top of the current environment."""
        if self.cmd_runner:
            return self.cmd_runner(cmd, cwd, env)
        return subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=1800,
                              env={**os.environ, **env} if env else None)

    def source_vcs(self, repos) -> Vcs:
        """The developer's main clones: only fetched from and used to add worktrees, never edited or switched."""
        return worktrees.source_vcs(self.workspace, list(repos), self.vcs_runner)

    def worktree(self, state: dict, repo: str) -> str:
        """Create or reuse this run's worktree for `repo` (used when scope widens mid-run too)."""
        return worktrees.ensure(self.source_vcs([repo]), self.workspace, self.store, state["run_id"], state["ticket_key"],
                                repo, self.run_cmd)

    @contextlib.contextmanager
    def repo_lock(self, repo: str):
        """Repos with `parallel_checks: false` run their checks one run at a time, across processes."""
        if self.workspace.repo(repo).parallel_checks:
            yield
            return
        lock_dir = Path(self.workspace.state_dir, "locks")
        lock_dir.mkdir(parents=True, exist_ok=True)
        with open(lock_dir / f"{repo}.lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)


def playwright_config(cfg: dict, output_dir: str, profile_dir: str) -> dict:
    """A stdio Playwright MCP config with --output-dir and --user-data-dir added (unless already set)."""
    if "command" not in cfg:
        return cfg
    args = list(cfg.get("args") or [])
    for flag, value in (("--output-dir", output_dir), ("--user-data-dir", profile_dir)):
        if not any(a == flag or a.startswith(flag + "=") for a in args):
            args += [flag, value]
    return {**cfg, "args": args}


def _j(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str)


def _tail(text: str, n: int = 4000) -> str:
    return text if len(text) <= n else "…" + text[-n:]


class GraphKit:
    """Wiring shared by every devflow graph: heartbeat nodes, two-node human checkpoints and the terminal abort.

    Every human checkpoint is two nodes: `<name>` records the payload, `<name>_wait` sets WAITING_HUMAN and calls
    interrupt(). An answer outside the options is asked again, never treated as a choice."""

    def __init__(self, g: StateGraph, store: Store):
        self.g, self.store = g, store

    def node(self, name: str, fn: Callable, **kw):
        store = self.store

        def wrapped(state):
            run_id = state.get("run_id") or state.get("payload_run_id")
            if run_id:
                store.heartbeat(run_id, name)
            return fn(state)
        wrapped.__name__ = name
        self.g.add_node(name, wrapped, **kw)

    def checkpoint(self, name: str, build: Callable[[dict], dict], options: list[str]):
        store = self.store

        def prepare(state):
            return {"pending_checkpoint": {"name": name, "payload": build(state), "options": options}}

        def wait(state):
            # Rebuilt if missing (e.g. a run reopened after Abort), so the question is always shown in full.
            pc = state.get("pending_checkpoint") or {"name": name, "payload": build(state), "options": options}
            store.set_status(state["run_id"], "WAITING_HUMAN", node=name, checkpoint=name)
            answer = interrupt(pc)
            while True:
                if isinstance(answer, str):
                    answer = {"choice": answer}
                if isinstance(answer, dict) and answer.get("choice") in options:
                    break
                answer = interrupt({**pc, "error": f"'{(answer or {}).get('choice')}' is not one of {options}"})
            store.set_status(state["run_id"], "RUNNING", node=name)
            store.audit(state["run_id"], "decision", {"checkpoint": name, **answer})
            return {"last_answer": answer, "pending_checkpoint": None,
                    "decisions": [{"checkpoint": name, **answer}]}

        self.g.add_node(name, prepare)
        self.g.add_node(f"{name}_wait", wait)
        self.g.add_edge(name, f"{name}_wait")

    @staticmethod
    def choice(state) -> str:
        return state["last_answer"]["choice"]

    def add_abort(self):
        store = self.store

        def abort(state):
            at = (state.get("last_answer") and state["decisions"][-1]["checkpoint"]) if state.get("decisions") else ""
            store.set_status(state["run_id"], "ABORTED", node="abort", checkpoint=at, detail=(state.get("last_answer") or {}).get("note", ""))
            return {"aborted_at": at, "output": f"Run {state['run_id']} aborted at {at}. Local branches and edits were left untouched."}

        self.g.add_node("abort", abort)
        self.g.add_edge("abort", END)


IMPLEMENT_STEPS = ("gather_context", "analyze_requirements", "change_impact", "discover_repos", "plan_implementation", "implement",
                   "targeted_fix", "analyze_feedback_and_route", "integration_check", "repo_review", "contract_review", "summary")


def check_environment(deps: "Deps", state: dict, need_confluence: bool, steps: tuple[str, ...] = IMPLEMENT_STEPS) -> tuple[ScopeGuard, list[str]]:
    """Preflight shared by the workflows: tools installed, MCPs reachable, repos reachable, worktrees free, ticket
    readable. Raises PreflightFailed with every gap at once; returns the frozen scope (repo -> this run's worktree
    path) and non-blocking warnings. The main clones are only read, so they may have uncommitted work."""
    ws = deps.workspace
    gaps, warnings = [], []
    requested = state.get("requested_repos") or list(ws.repos)
    unknown = [r for r in requested if r not in ws.repos]
    if unknown:
        gaps.append(f"--repos names not in workspace.yaml: {unknown}")
    repos = {r: ws.repos[r].path for r in requested if r in ws.repos}
    scope = ScopeGuard.create({r: ws.worktree(state["ticket_key"], r) for r in repos})
    from .. import doctor
    blocking = lambda checks: [c.detail for c in checks if c.status == "fail" and c.blocking]  # noqa: E731
    gaps += blocking(doctor.tool_checks(ws, deps.which, list(repos)))
    gaps += blocking(doctor.mcp_checks(ws, deps.jira, deps.confluence, need_confluence))
    if repos and not gaps:
        vcs = deps.source_vcs(repos)
        checks, ok_repos = doctor.vcs_checks(ws, vcs, list(repos))
        gaps += blocking(checks)
        gaps += worktrees.gaps(vcs, ws, state["ticket_key"], ok_repos)
    if not gaps:
        try:
            status, assignee = deps.jira.status_and_assignee(state["ticket_key"])
            if status.lower() != "in progress" and need_confluence:
                warnings.append(f"{state['ticket_key']} is '{status}', not In Progress (the workflow will not change that)")
            if ws.jira_user and assignee and ws.jira_user.lower() not in assignee.lower():
                warnings.append(f"{state['ticket_key']} is assigned to {assignee}, not you")
        except Exception as e:  # noqa: BLE001
            gaps.append(f"cannot read {state['ticket_key']} from Jira: {e}")
    if gaps:
        raise PreflightFailed(gaps)
    missing = {s: m for s, m in deps.routing.missing().items() if s in steps}
    if missing:  # the step still runs, just without that skill; `devflow setup` lists what to install
        warnings.append("global Claude Code skills not installed (steps run without them): "
                        + "; ".join(f"{s}: {', '.join(m)}" for s, m in missing.items()))
    return scope, warnings


def run_repo_checks(deps: "Deps", repo: str, scope: dict) -> tuple[bool, list[dict]]:
    """The repo's own lint/typecheck/test/build in the run's worktree; the first failure stops the list."""
    results = []
    with deps.repo_lock(repo):
        for name, cmd in deps.workspace.repo(repo).check_commands:
            r = deps.run_cmd(cmd, scope[repo], worktrees.env_for(scope))
            results.append({"check": name, "ok": r.returncode == 0, "output": _tail((r.stdout or "") + (r.stderr or ""), 3000)})
            if r.returncode != 0:
                return False, results
    return True, results


def route_feedback(fa: FeedbackAnalysis, scope: dict, repos: dict, edges: list[tuple], cap: int):
    """Route analyzed feedback: high-confidence fixable items go to the affected repos (plus downstream repos when a
    contract changes) while every target still has fix attempts left; everything else goes to the developer.
    Returns (fixes {repo: [instructions]}, ask [items], scope_requests [{repo, reason}])."""
    fixes: dict[str, list[str]] = {}
    ask, scope_reqs = [], []
    for it in fa.items:
        d = it.model_dump()
        outside = [r for r in it.repos if r not in scope]
        if it.category == "scope_issue" or outside:
            scope_reqs += [{"repo": r, "reason": it.cause} for r in outside] or [{"repo": "?", "reason": it.cause}]
            continue
        targets = list(it.repos)
        if it.contract_changed:
            for r in it.repos:
                targets += [x for x in dagmod.downstream_closure(r, edges) if x not in targets]
        fixable = (it.category in FIX_CATEGORIES and it.confidence == "high" and targets
                   and all(t in repos for t in targets)
                   and all(repos[t].get("fix_attempts_used", 0) < cap for t in targets))
        if not fixable:
            reason = ("budget_used_up" if targets and any(repos.get(t, {}).get("fix_attempts_used", 0) >= cap for t in targets)
                      else "requirement_gap" if it.category == "requirement_gap" else "unclear")
            ask.append({**d, "why_asking": reason})
            continue
        for t in targets:
            fixes.setdefault(t, []).append(f"[{it.category}] {it.cause}\n{it.fix_instructions}")
    return fixes, ask, scope_reqs


def build_graph(deps: Deps, checkpointer=None):
    ws, store, llm = deps.workspace, deps.store, deps.llm
    g = StateGraph(State)

    kit = GraphKit(g, store)
    node, checkpoint, choice = kit.node, kit.checkpoint, kit.choice

    # ------------------------------------------------------------------ Phase 0
    def preflight(state):
        scope, warnings = check_environment(deps, state, need_confluence=True)
        store.audit(state["run_id"], "preflight", {"scope": scope.to_state(), "warnings": warnings})
        return {"scope": scope.to_state(), "env_warnings": warnings, "code_version": 0,
                "integration_version": -1, "manual_test_version": -1, "review_version": -1}

    node("preflight", preflight)

    def prepare_worktrees(state):
        return {"scope": ScopeGuard.create({r: deps.worktree(state, r) for r in state["scope"]}).to_state()}

    node("prepare_worktrees", prepare_worktrees)

    # ------------------------------------------------------------------ Phase 1
    def fetch_ticket(state):
        out_dir = Path(ws.state_dir, state["run_id"], "attachments")
        return {"ticket": deps.jira.fetch_ticket(state["ticket_key"], str(out_dir))}

    node("fetch_ticket", fetch_ticket, retry_policy=TRANSIENT)

    def gather_context(state):
        t = state["ticket"]
        pages, confirmed = [], True
        for pid in page_ids_from_urls(t.get("confluence_urls", [])):
            pages.append({"id": pid, "page": deps.confluence.get_page(pid), "children": deps.confluence.get_children(pid)})
        if not pages:
            confirmed = False
            found = deps.confluence.search(t["title"])
            pages.append({"id": "search", "page": found, "children": []})
        mrs = {}
        vcs = deps.vcs(state)
        for repo in state["scope"]:
            out = vcs.glab(repo, "mr", "list", "--search", state["ticket_key"], check=False)
            if out:
                mrs[repo] = out[:2000]
        prompt = (
            f"<ticket key='{t['key']}'>\n<title>{t['title']}</title>\n<description>\n{t['description']}\n</description>\n</ticket>\n"
            f"<confluence linked_from_ticket='{confirmed}'>\n{_tail(_j(pages), 120000)}\n</confluence>\n\n"
            "Extract (a) the detailed requirement and (b) the current related business. Cite the page title and "
            "section for every point. List conflicts between Confluence and the Jira text. "
            + ("" if confirmed else "These pages were found by search, not linked: mark every page confirmed=false.")
        )
        ctx = llm.structured(SYSTEM, prompt, RequirementContext, images=t.get("images", []), step="gather_context")
        return {"context": ctx.model_dump(), "existing_mrs": mrs}

    node("gather_context", gather_context, retry_policy=TRANSIENT)

    def _ticket_block(state) -> str:
        t = state["ticket"]
        parts = [f"<ticket key='{t['key']}'><title>{t['title']}</title>\n<description>\n{t['description']}\n</description></ticket>",
                 f"<requirement_context>\n{_j(state.get('context', {}))}\n</requirement_context>"]
        if state.get("answers"):
            parts.append("<developer_answers>\n" + "\n---\n".join(state["answers"]) + "\n</developer_answers>")
        return "\n".join(parts)

    def analyze_requirements(state):
        a = llm.structured(SYSTEM, _ticket_block(state) + "\n\nAnalyze the requirement. Only list questions that genuinely block planning.",
                           Analysis, images=state["ticket"].get("images", []), step="analyze_requirements")
        return {"analysis": a.model_dump()}

    node("analyze_requirements", analyze_requirements)

    def change_impact(state):
        repos_desc = "\n".join(f"- {n} (ui={ws.repos[n].has_ui})" for n in state["scope"]) if state["scope"] else "(none)"
        other = [n for n in ws.repos if n not in state["scope"]]
        prompt = (
            _ticket_block(state)
            + f"\n<analysis>\n{_j(state['analysis'])}\n</analysis>\n<allowed_repos>\n{repos_desc}\n</allowed_repos>\n"
            + f"<other_workspace_repos_not_allowed>{other}</other_workspace_repos_not_allowed>\n\n"
            "Determine the change impact. candidate_repos must come only from allowed_repos. If another repo seems "
            "required, list it in out_of_scope_repos instead."
        )
        imp = llm.structured(SYSTEM, prompt, Impact, step="change_impact")
        allowed = set(state["scope"])
        requests = [n.model_dump() for n in imp.out_of_scope_repos]
        requests += [{"repo": r, "reason": "named as a candidate but not in scope"} for r in imp.candidate_repos if r not in allowed]
        decided = {d["repo"] for d in state.get("scope_gaps", [])}
        requests = [r for r in requests if r["repo"] not in allowed and r["repo"] not in decided]
        impact = imp.model_dump()
        impact["candidate_repos"] = [r for r in imp.candidate_repos if r in allowed]
        return {"impact": impact, "scope_requests": requests}

    node("change_impact", change_impact)

    def after_impact(state) -> str:
        questions = state["analysis"]["questions"] + state["impact"]["questions"]
        if state.get("scope_requests") or (questions and not state.get("clarify_proceed")):
            return "clarify"
        return "discover_repos"

    g.add_conditional_edges("change_impact", after_impact, ["clarify", "discover_repos"])

    checkpoint("clarify", lambda s: {
        "questions": s["analysis"]["questions"] + s["impact"]["questions"],
        "scope_requests": s.get("scope_requests", []),
        "conflicts": s.get("context", {}).get("conflicts", []),
        "hint": "choice=answer with your answers in note; approve_repos lists scope requests you approve. "
                "choice=proceed continues with assumptions.",
    }, ["answer", "proceed", "abort"])

    def apply_clarify(state):
        ans = state["last_answer"]
        update: dict = {}
        if ans.get("note"):
            update["answers"] = [ans["note"]]
        if ans["choice"] == "proceed":
            update["clarify_proceed"] = True
        approved = set(ans.get("approve_repos") or [])
        scope = dict(state["scope"])
        gaps = []
        for req in state.get("scope_requests", []):
            if req["repo"] in approved and req["repo"] in ws.repos:
                scope[req["repo"]] = deps.worktree(state, req["repo"])
                store.audit(state["run_id"], "scope_widened", req)
            else:
                gaps.append({**req, "decision": "denied"})
        update.update({"scope": ScopeGuard.create(scope).to_state(), "scope_gaps": gaps, "scope_requests": []})
        return update

    node("apply_clarify", apply_clarify)
    g.add_conditional_edges("clarify_wait", lambda s: "abort" if choice(s) == "abort" else "apply_clarify", ["abort", "apply_clarify"])
    g.add_edge("apply_clarify", "change_impact")

    # ------------------------------------------------------------------ Phase 2
    def discover_repos(state):
        vcs, coder = deps.vcs(state), deps.coder(state)
        findings, needs = {}, []
        question = (f"{_ticket_block(state)}\n<analysis>{_j(state['analysis'])}</analysis>\n<impact>{_j(state['impact'])}</impact>\n\n"
                    "Find the modules, routes, components and tables in THIS repository that the change touches. "
                    "Set confirmed=false if this repository does not need to change.")
        for repo in state["impact"]["candidate_repos"]:
            vcs.git(repo, "fetch", "origin")
            res = coder.explore(repo, state["scope"][repo], question, step="discover_repos")
            findings[repo] = {"confirmed": res.ok, "relevant_files": res.files_changed, "notes": res.summary}
            needs += [n for n in res.out_of_scope_needs if n.get("repo") not in state["scope"]]
        return {"repo_findings": findings, "scope_requests": needs}

    node("discover_repos", discover_repos)
    g.add_conditional_edges("discover_repos", lambda s: "scope_request" if s.get("scope_requests") else "plan_implementation",
                            ["scope_request", "plan_implementation"])

    def plan_implementation(state):
        findings = state.get("repo_findings", {})
        confirmed = [r for r, f in findings.items() if f["confirmed"]] or list(findings) or state["impact"]["candidate_repos"]
        base = (_ticket_block(state)
                + f"\n<analysis>{_j(state['analysis'])}</analysis>\n<impact>{_j(state['impact'])}</impact>\n"
                + f"<repo_findings>{_j(findings)}</repo_findings>\n<repos_to_plan>{confirmed}</repos_to_plan>\n")
        if state.get("plan_notes"):
            base += "<developer_revision_notes>\n" + "\n".join(state["plan_notes"]) + "\n</developer_revision_notes>\n"
        instr = ("Write the implementation plan. Use only repos_to_plan. Define an explicit dependency DAG between repos "
                 "(e.g. shared library -> backend -> frontend); repos with no dependency get no edge. Fix the shared "
                 "contracts up front so repos can be coded against them. Map every acceptance criterion to tests.")
        plan = llm.structured(SYSTEM, base + instr, Plan, step="plan_implementation")
        errors = _plan_errors(plan, state)
        if errors:
            plan = llm.structured(SYSTEM, base + instr + "\n\nYour previous plan was invalid:\n- " + "\n- ".join(errors), Plan, step="plan_implementation")
            errors = _plan_errors(plan, state)
        nodes = [r.repo for r in plan.repos]
        edges = [(e.upstream, e.downstream) for e in plan.edges]
        waves = dagmod.waves(nodes, edges) if not errors else []
        return {"plan": plan.model_dump(), "plan_errors": errors,
                "dag": {"nodes": nodes, "edges": edges, "waves": waves, "merge_order": [r for w in waves for r in w]}}

    def _plan_errors(plan: Plan, state) -> list[str]:
        nodes = [r.repo for r in plan.repos]
        errors = dagmod.validate(nodes, [(e.upstream, e.downstream) for e in plan.edges], set(state["scope"]))
        if not nodes:
            errors.append("the plan has no repos")
        if state["impact"]["contract_changes"] and not plan.contracts:
            errors.append("the impact lists contract changes but the plan fixes no contracts")
        return errors

    node("plan_implementation", plan_implementation)
    g.add_edge("plan_implementation", "approve_plan")

    checkpoint("approve_plan", lambda s: {"plan": s["plan"], "dag": s["dag"], "validation_errors": s.get("plan_errors", []),
                                          "risk": s["impact"]["risk"], "warnings": s.get("env_warnings", [])},
               ["approve", "revise", "abort"])

    def after_plan(state) -> str:
        c = choice(state)
        if c == "approve" and state.get("plan_errors"):
            return "plan_implementation"  # an invalid DAG can never be approved
        return {"approve": "prepare_branches", "revise": "plan_revise", "abort": "abort"}[c]

    node("plan_revise", lambda s: {"plan_notes": [s["last_answer"].get("note") or "Revise the plan."]})
    g.add_edge("plan_revise", "plan_implementation")
    g.add_conditional_edges("approve_plan_wait", after_plan, ["prepare_branches", "plan_revise", "plan_implementation", "abort"])

    # ------------------------------------------------------------------ Phase 3
    def prepare_branches(state):
        vcs, key, run_id = deps.vcs(state), state["ticket_key"], state["run_id"]
        approvals = state.get("branch_approvals", {})
        repos_state, issues = {}, []
        plan_repos = {r["repo"]: r for r in state["plan"]["repos"]}
        for repo in state["dag"]["nodes"]:
            cfg = ws.repo(repo)
            prior = (state.get("repos") or {}).get(repo, {})
            res = _ensure_branch(vcs, repo, cfg.base_branch, key, run_id, prior.get("base_sha"), repo in approvals)
            if res.get("issue"):
                issues.append(res["issue"])
                continue
            repos_state[repo] = {
                "status": prior.get("status", "pending"), "branch": key, "base_sha": res["base_sha"],
                "branch_owner": run_id, "fix_attempts_used": prior.get("fix_attempts_used", 0),
                "implemented": prior.get("implemented", False), "tasks": plan_repos[repo]["tasks"],
                "likely_files": plan_repos[repo]["likely_files"], "fix_instructions": prior.get("fix_instructions", []),
            }
        return {"repos": repos_state, "branch_issues": issues}

    def _ensure_branch(vcs: Vcs, repo, base, key, run_id, recorded_base, approved) -> dict:
        def create():
            # In the run's worktree: cut <KEY> from a freshly fetched develop (the main clone is never switched).
            vcs.git(repo, "fetch", "origin")
            vcs.git(repo, "checkout", "--no-track", "-b", key, f"origin/{base}")
            sha = vcs.head(repo)
            vcs.git(repo, "config", f"branch.{key}.devflow-run", run_id)
            vcs.git(repo, "config", f"branch.{key}.devflow-base", sha)
            return sha

        local, remote = vcs.local_branch_exists(repo, key), vcs.remote_branch_exists(repo, key)
        if not local and not remote:
            out = perform(store, Effect(run_id, "prepare_branches", repo, f"create_branch:{key}"),
                          detect=lambda: vcs.config_get(repo, f"branch.{key}.devflow-base") if vcs.local_branch_exists(repo, key) else None,
                          act=create)
            return {"base_sha": out["result"]}
        owner = vcs.config_get(repo, f"branch.{key}.devflow-run") if local else ""
        cfg_base = vcs.config_get(repo, f"branch.{key}.devflow-base") if local else ""
        expected_base = recorded_base or cfg_base
        ahead = behind = 0
        if local and remote:
            vcs.git(repo, "fetch", "origin", key)
            ahead, behind = vcs.ahead_behind(repo, key, f"origin/{key}")
        dirty = vcs.is_dirty(repo) if local else False
        safe = (owner == run_id and bool(cfg_base) and cfg_base == expected_base and behind == 0)
        if safe or approved:
            if approved and not safe:
                vcs.git(repo, "fetch", "origin")
                vcs.git(repo, "checkout", key)  # local, or creates a tracking branch from origin/<key>
                sha = vcs.git(repo, "merge-base", f"origin/{base}", key)
                vcs.git(repo, "config", f"branch.{key}.devflow-run", run_id)
                vcs.git(repo, "config", f"branch.{key}.devflow-base", sha)
                store.audit(run_id, "branch_reused_with_approval", {"repo": repo, "previous_owner": owner or "unknown"})
                return {"base_sha": sha}
            current = vcs.git(repo, "rev-parse", "--abbrev-ref", "HEAD")
            if current != key:
                vcs.git(repo, "checkout", key)
            return {"base_sha": cfg_base}
        return {"issue": {"repo": repo, "branch": key, "owner": owner or "unknown", "local": local, "remote": remote,
                          "recorded_base": expected_base, "branch_base": cfg_base, "ahead": ahead, "behind": behind,
                          "uncommitted_changes": dirty}}

    node("prepare_branches", prepare_branches)
    g.add_conditional_edges("prepare_branches", lambda s: "branch_ownership" if s.get("branch_issues") else "schedule",
                            ["branch_ownership", "schedule"])
    checkpoint("branch_ownership", lambda s: {"issues": s["branch_issues"],
                                              "hint": "reuse = adopt these existing branches for this run (logged). The workflow never deletes or resets a branch."},
               ["reuse", "abort"])
    node("approve_branch_reuse", lambda s: {"branch_approvals": {**s.get("branch_approvals", {}), **{i["repo"]: True for i in s["branch_issues"]}}})
    g.add_conditional_edges("branch_ownership_wait", lambda s: "abort" if choice(s) == "abort" else "approve_branch_reuse",
                            ["abort", "approve_branch_reuse"])
    g.add_edge("approve_branch_reuse", "prepare_branches")

    # --- wave scheduler ----------------------------------------------------
    node("schedule", lambda s: {})

    def gate(state) -> str:
        cv = state.get("code_version", 0)
        imp = state["impact"]
        if (imp["integration_required"] or imp["e2e_required"]) and state.get("integration_version") != cv:
            return "integration_check"
        if state.get("manual_test_version") != cv:
            return "manual_test"
        if state.get("review_version") != cv:
            return "repo_review"
        return "approve_push"

    def route_schedule(state):
        repos, edges = state["repos"], [tuple(e) for e in state["dag"]["edges"]]
        if any(r.get("scope_needs") for r in repos.values()):
            return "scope_request"
        if any(r["status"] == "blocked_budget" for r in repos.values()):
            return "budget_exhausted"
        todo = [n for n, r in repos.items() if r["status"] in ("pending", "needs_fix", "needs_checks")]
        if not todo:
            return gate(state)
        ready = [n for n in todo if all(repos[u]["status"] == "ready" for u in dagmod.upstream_of(n, edges))]
        if not ready:
            return "budget_exhausted"
        return [Send("implement_repo", _repo_payload(state, n)) for n in ready]

    def _repo_payload(state, repo) -> dict:
        edges = [tuple(e) for e in state["dag"]["edges"]]
        return {"payload_run_id": state["run_id"], "repo": repo, "repo_state": state["repos"][repo], "scope": state["scope"],
                "ticket_key": state["ticket_key"], "plan": state["plan"], "analysis": state["analysis"],
                "upstream": dagmod.upstream_of(repo, edges)}

    g.add_conditional_edges("schedule", route_schedule,
                            ["implement_repo", "scope_request", "budget_exhausted", "integration_check", "manual_test", "repo_review", "approve_push"])

    def run_checks(repo: str, scope: dict) -> tuple[bool, list[dict]]:
        return run_repo_checks(deps, repo, scope)

    def implement_repo(payload):
        repo, rs = payload["repo"], dict(payload["repo_state"])
        st = {"scope": payload["scope"]}
        coder, vcs, cap = deps.coder(st), deps.vcs(st), ws.max_fix_attempts
        path = payload["scope"][repo]
        changed = 0
        mode = rs["status"]
        feedback: list[str] = list(rs.get("fix_instructions", []))
        while True:
            if mode in ("pending", "needs_fix"):
                if rs.get("implemented"):
                    if rs.get("fix_attempts_used", 0) >= cap:  # run-wide budget: never reset, never exceeded
                        rs["status"] = "blocked_budget"
                        break
                    rs["fix_attempts_used"] = rs.get("fix_attempts_used", 0) + 1
                upstream = {u: _tail(vcs.diff_against(u, f"origin/{ws.repo(u).base_branch}"), 15000) for u in payload["upstream"]}
                instructions = _implement_prompt(payload, repo, rs, feedback, upstream)
                res = coder.implement(repo, path, instructions, step="targeted_fix" if rs.get("fix_attempts_used") else "implement",
                                      escalate=rs.get("fix_attempts_used", 0) >= cap)  # the last attempt gets the stronger model
                rs["implemented"], changed = True, changed + 1
                rs["summary"] = res.summary
                store.audit(payload["payload_run_id"], "implement", {"repo": repo, "attempt": rs.get("fix_attempts_used", 0), "ok": res.ok})
                needs = [n for n in res.out_of_scope_needs if n.get("repo") not in payload["scope"]]
                if needs:
                    rs["scope_needs"] = needs
                    rs["status"] = "needs_scope"
                    break
            ok, results = run_checks(repo, payload["scope"])
            rs["checks"] = results
            if ok:
                rs["status"], rs["fix_instructions"] = "ready", []
                break
            if mode == "needs_checks":  # you fixed it by hand; a failure goes back to you, not to the agent
                rs["status"] = "blocked_budget"
                break
            mode = "needs_fix"
            feedback = [f"Checks failed:\n{_j(results)}"]
        rs["diff_files"] = vcs.changed_files(repo)
        return {"repos": {repo: rs}, "code_version": changed}

    def _implement_prompt(payload, repo, rs, feedback, upstream) -> str:
        plan = payload["plan"]
        parts = [f"Ticket {payload['ticket_key']}: {payload['analysis']['summary']}",
                 f"<acceptance_criteria>{_j(payload['analysis']['acceptance_criteria'])}</acceptance_criteria>",
                 f"<tasks_for_{repo}>{_j(rs['tasks'])}</tasks_for_{repo}>",
                 f"<likely_files>{_j(rs.get('likely_files', []))}</likely_files>",
                 f"<contracts_fixed_up_front>{_j(plan['contracts'])}</contracts_fixed_up_front>",
                 f"<migrations>{_j(plan['migrations'])}</migrations>",
                 f"<test_plan>{_j(plan['test_plan'])}</test_plan>"]
        for u, diff in upstream.items():
            parts.append(f"<upstream_repo name='{u}' already_implemented='true'>\n{diff}\n</upstream_repo>")
        if feedback:
            parts.append("<fix_this>\n" + "\n---\n".join(feedback) + "\n</fix_this>\nFix exactly this; keep the rest of the work.")
        else:
            parts.append("Implement the tasks for this repository, including tests. Match the repo's conventions.")
        return "\n".join(parts)

    node("implement_repo", implement_repo)
    g.add_edge("implement_repo", "schedule")

    checkpoint("budget_exhausted", lambda s: {
        "repos": {n: {"fix_attempts_used": r.get("fix_attempts_used"), "checks": r.get("checks"), "summary": r.get("summary")}
                  for n, r in s["repos"].items() if r["status"] == "blocked_budget"},
        "hint": "These repos used their 3 automated fix attempts for this run. Fix them by hand, then choose fixed_by_hand "
                "(checks re-run with no automated edit), or abort."}, ["fixed_by_hand", "abort"])
    node("mark_hand_fixed", lambda s: {"repos": {n: {"status": "needs_checks"} for n, r in s["repos"].items() if r["status"] == "blocked_budget"}})
    g.add_conditional_edges("budget_exhausted_wait", lambda s: "abort" if choice(s) == "abort" else "mark_hand_fixed",
                            ["abort", "mark_hand_fixed"])
    g.add_edge("mark_hand_fixed", "schedule")

    # --- scope requests from discovery, implementation or routing ----------------
    checkpoint("scope_request", lambda s: {
        "requests": (s.get("scope_requests") or []) + [{**n, "from_repo": r} for r, rs in (s.get("repos") or {}).items() for n in rs.get("scope_needs", [])],
        "hint": "approve = add the repo(s) to scope (logged) and re-plan; deny = continue without them."},
        ["approve", "deny", "abort"])

    def apply_scope(state):
        reqs = (state.get("scope_requests") or []) + [n for rs in (state.get("repos") or {}).values() for n in rs.get("scope_needs", [])]
        cleared = {r: {"scope_needs": [], "status": "needs_checks"} for r, rs in (state.get("repos") or {}).items() if rs.get("scope_needs")}
        if choice(state) == "approve":
            scope = dict(state["scope"])
            for req in reqs:
                if req["repo"] in ws.repos:
                    scope[req["repo"]] = deps.worktree(state, req["repo"])
                    store.audit(state["run_id"], "scope_widened", req)
            return {"scope": ScopeGuard.create(scope).to_state(), "scope_requests": [], "repos": cleared,
                    "plan_notes": [f"Scope widened by the developer to include {sorted({r['repo'] for r in reqs})}: {_j(reqs)}"]}
        return {"scope_requests": [], "repos": cleared, "scope_gaps": [{**r, "decision": "denied"} for r in reqs]}

    node("apply_scope", apply_scope)
    g.add_conditional_edges("scope_request_wait", lambda s: "abort" if choice(s) == "abort" else "apply_scope", ["abort", "apply_scope"])

    def after_scope(state) -> str:
        approved = state["last_answer"]["choice"] == "approve"
        if not state.get("plan"):  # raised during discovery: recompute impact with the new scope, or plan without it
            return "change_impact" if approved else "plan_implementation"
        return "plan_implementation" if approved else "schedule"  # raised during implementation or routing

    g.add_conditional_edges("apply_scope", after_scope, ["change_impact", "plan_implementation", "schedule"])

    # --- integration / E2E -------------------------------------------------------
    def integration_check(state):
        """No AI first: the repos' own `integration` / `e2e` commands run first. The AI contract check and the
        Claude Code + Playwright walk-through run only for what those commands do not cover."""
        imp, nodes = state["impact"], state["dag"]["nodes"]
        feedback, report = [], {"commands": []}
        for kind in ("integration", "e2e"):
            for r in nodes:
                cmd = ws.repo(r).commands.get(kind)
                if cmd:
                    res = deps.run_cmd(cmd, state["scope"][r], worktrees.env_for(state["scope"]))
                    out = _tail((res.stdout or "") + (res.stderr or ""), 3000)
                    report["commands"].append({"repo": r, "check": kind, "ok": res.returncode == 0, "output": out})
                    if res.returncode != 0:
                        feedback.append({"source": kind, "repos": [r], "text": f"`{cmd}` failed:\n{out}"})
        has = lambda kind: any(ws.repo(r).commands.get(kind) for r in nodes)  # noqa: E731
        if not feedback and imp["integration_required"] and not has("integration"):
            vcs = deps.vcs(state)
            diffs = {r: _tail(vcs.diff_against(r, f"origin/{ws.repo(r).base_branch}"), 30000) for r in nodes}
            cc = llm.structured(SYSTEM, f"<contracts>{_j(state['plan']['contracts'])}</contracts>\n<diffs>{_j(diffs)}</diffs>\n\n"
                                "Check that every repo implements the fixed contracts consistently (producer and consumer agree on "
                                "routes, fields, types, events, migrations).", ContractCheck, step="integration_check")
            report["contracts"] = cc.model_dump()
            feedback += [{"source": "integration", "repos": i.repos, "text": i.issue} for i in cc.issues]
        if not feedback and imp["e2e_required"] and not has("e2e"):
            repos = {r: state["scope"][r] for r in nodes}
            run_cmds = {r: ws.repo(r).commands.get("run", "") for r in repos}
            res = deps.coder(state).verify(repos, step="integration_check", instructions=(
                f"Start the services with these commands: {_j(run_cmds)}. Using the Playwright MCP tools, walk each acceptance "
                f"criterion and record pass/fail with evidence (save screenshots under each repo's .devflow-evidence/ folder).\n"
                f"<acceptance_criteria>{_j(state['analysis']['acceptance_criteria'])}</acceptance_criteria>\n"
                f"<manual_test_focus>{_j(imp['manual_test_focus'])}</manual_test_focus>\nStop the services when done."))
            report["e2e"] = res.findings
            feedback += [{"source": "e2e", "repos": r.get("suspected_repos", []), "text": f"{r['criterion']}: {r['evidence']}"}
                         for r in res.findings.get("results", []) if not r.get("passed")]
        update = {"integration_report": report, "pending_feedback": feedback}
        if not feedback:
            update["integration_version"] = state.get("code_version", 0)
        return update

    node("integration_check", integration_check)
    g.add_conditional_edges("integration_check", lambda s: "analyze_feedback_and_route" if s.get("pending_feedback") else gate(s),
                            ["analyze_feedback_and_route", "manual_test", "repo_review", "approve_push"])

    # --- manual test (mandatory) -------------------------------------------------
    def manual_payload(state):
        criteria = state["analysis"]["acceptance_criteria"] + state["impact"]["manual_test_focus"]
        return {"repos": {r: {"path": state["scope"][r], "branch": state["ticket_key"], "changed_files": rs.get("diff_files", []),
                              "run": ws.repo(r).commands.get("run", "")} for r, rs in state["repos"].items()},
                "checklist": criteria, "integration": state.get("integration_report", {}),
                "hint": "Nothing is committed yet. ok = it works; feedback = describe what's wrong (name repos if you know them)."}

    checkpoint("manual_test", manual_payload, ["ok", "feedback", "abort"])

    def after_manual(state) -> str:
        c = choice(state)
        return {"ok": "manual_ok", "feedback": "manual_feedback", "abort": "abort"}[c]

    node("manual_ok", lambda s: {"manual_test_version": s.get("code_version", 0)})
    node("manual_feedback", lambda s: {"pending_feedback": [{"source": "manual_test", "repos": s["last_answer"].get("repos", []),
                                                              "text": s["last_answer"].get("note", "")}]})
    g.add_conditional_edges("manual_test_wait", after_manual, ["manual_ok", "manual_feedback", "abort"])
    g.add_conditional_edges("manual_ok", gate, ["integration_check", "manual_test", "repo_review", "approve_push"])
    g.add_edge("manual_feedback", "analyze_feedback_and_route")

    # --- reviews -------------------------------------------------------------
    def repo_review(state):
        from ..workflows import pr_review
        vcs = deps.vcs(state)
        reviewer = pr_review.build_graph(AsStep(llm, "repo_review"))
        reviews, feedback = {}, []
        for repo in state["dag"]["nodes"]:
            diff = vcs.diff_against(repo, f"origin/{ws.repo(repo).base_branch}")
            out = reviewer.invoke({"title": f"{state['ticket_key']} ({repo})", "description": _j(state["repos"][repo].get("tasks", [])),
                                   "diff": _tail(diff, 60000), "findings": []})
            reviews[repo] = {"decision": out["verdict"].decision, "summary": out["verdict"].summary,
                             "findings": [f.model_dump() for f in out["findings"]]}
            feedback += [{"source": "review", "repos": [repo], "text": f"[{f.severity}] {f.file}:{f.line or '?'} {f.title}: {f.detail} -> {f.suggestion}"}
                         for f in out["findings"] if f.severity in ("blocker", "major")]
        return {"reviews": reviews, "pending_feedback": feedback}

    node("repo_review", repo_review)

    def contract_review(state):
        vcs = deps.vcs(state)
        diffs = {r: _tail(vcs.diff_against(r, f"origin/{ws.repo(r).base_branch}"), 30000) for r in state["dag"]["nodes"]}
        cr = llm.structured(SYSTEM, (
            f"<plan>{_j(state['plan'])}</plan>\n<merge_order>{state['dag']['merge_order']}</merge_order>\n"
            f"<acceptance_criteria>{_j(state['analysis']['acceptance_criteria'])}</acceptance_criteria>\n<diffs>{_j(diffs)}</diffs>\n\n"
            "Cross-repo review: producer/consumer contract agreement, migrations matching the code that uses them, a merge "
            "order that is safe to deploy, and every acceptance criterion covered somewhere. Only real blockers."), ContractReview, step="contract_review")
        feedback = list(state.get("pending_feedback") or []) + [{"source": "contract_review", "repos": b.repos, "text": b.issue} for b in cr.blockers]
        update = {"contract_review": cr.model_dump(), "pending_feedback": feedback}
        if not feedback:
            update["review_version"] = state.get("code_version", 0)
        return update

    node("contract_review", contract_review)
    g.add_edge("repo_review", "contract_review")
    g.add_conditional_edges("contract_review", lambda s: "analyze_feedback_and_route" if s.get("pending_feedback") else gate(s),
                            ["analyze_feedback_and_route", "integration_check", "manual_test", "repo_review", "approve_push"])

    # --- analyze_feedback_and_route --------------------------------------------
    def analyze_feedback_and_route(state):
        items_in = state.get("pending_feedback") or []
        repos = state["repos"]
        status = {r: {"fix_attempts_used": rs.get("fix_attempts_used", 0), "status": rs["status"]} for r, rs in repos.items()}
        fa = llm.structured(SYSTEM, (
            _ticket_block(state)
            + f"\n<plan>{_j(state['plan'])}</plan>\n<dag>{_j(state['dag'])}</dag>\n<repos_in_scope>{list(state['scope'])}</repos_in_scope>\n"
            f"<repo_status>{_j(status)}</repo_status>\n<feedback>{_j(items_in)}</feedback>\n\n"
            "Classify each feedback item. Decide which repos must change, the cause and your confidence. Use requirement_gap "
            "when the requirement itself is unclear or missing, scope_issue when a repo outside repos_in_scope must change, "
            "and unclear when the evidence does not point to a cause."), FeedbackAnalysis, step="analyze_feedback_and_route")
        fixes, ask, scope_reqs = route_feedback(fa, state["scope"], repos, [tuple(e) for e in state["dag"]["edges"]], ws.max_fix_attempts)
        if not fa.items:
            ask = [{"source": "analyzer", "category": "unclear", "cause": "no actionable item found", "repos": [], "feedback": items_in, "why_asking": "unclear"}]
        store.audit(state["run_id"], "feedback_routed", {"fixes": list(fixes), "ask": len(ask), "scope": len(scope_reqs)})
        return {"feedback_items": [i.model_dump() for i in fa.items], "pending_feedback": [], "route_ask_items": ask,
                "scope_requests": scope_reqs,
                "repos": {r: {"status": "needs_fix", "fix_instructions": repos[r].get("fix_instructions", []) + msgs} for r, msgs in fixes.items()}}

    node("analyze_feedback_and_route", analyze_feedback_and_route)
    g.add_conditional_edges("analyze_feedback_and_route",
                            lambda s: "scope_request" if s.get("scope_requests") else "route_ask" if s.get("route_ask_items") else "schedule",
                            ["scope_request", "route_ask", "schedule"])

    checkpoint("route_ask", lambda s: {"items": s["route_ask_items"],
                                       "hint": "answer = give the missing requirement/decision in note (re-analyzed); "
                                               "fix = name repos + instructions in note (only repos with attempts left); "
                                               "skip = ignore these items."},
               ["answer", "fix", "skip", "abort"])

    def apply_route_ask(state):
        ans, items = state["last_answer"], state["route_ask_items"]
        if ans["choice"] == "answer":
            return {"answers": [ans.get("note", "")], "route_ask_items": [],
                    "pending_feedback": [{"source": i.get("source"), "repos": i.get("repos", []), "text": i.get("cause", "")} for i in items]
                    + [{"source": "developer", "repos": [], "text": ans.get("note", "")}]}
        if ans["choice"] == "fix":
            update: dict = {}
            for r in ans.get("repos") or []:
                rs = state["repos"].get(r)
                if rs is None:
                    continue
                if rs.get("fix_attempts_used", 0) >= ws.max_fix_attempts:
                    update[r] = {"status": "blocked_budget"}  # the cap holds even when you ask for a fix
                else:
                    update[r] = {"status": "needs_fix", "fix_instructions": rs.get("fix_instructions", []) + [ans.get("note", "")]}
            return {"repos": update, "route_ask_items": []}
        # skip: accept these items as they are. Integration/review findings you skip are not raised again for this code;
        # manual-test feedback is never "accepted": the manual test runs again.
        cv = state.get("code_version", 0)
        sources = " ".join(str(i.get("source", "")) for i in items).lower()
        update = {"route_ask_items": []}
        if "integration" in sources or "e2e" in sources:
            update["integration_version"] = cv
        if "review" in sources:
            update["review_version"] = cv
        return update

    node("apply_route_ask", apply_route_ask)
    g.add_conditional_edges("route_ask_wait", lambda s: "abort" if choice(s) == "abort" else "apply_route_ask", ["abort", "apply_route_ask"])
    g.add_conditional_edges("apply_route_ask", lambda s: "analyze_feedback_and_route" if s.get("pending_feedback") else "schedule",
                            ["analyze_feedback_and_route", "schedule"])

    # ------------------------------------------------------------------ Phase 4
    checkpoint("approve_push", lambda s: {
        "repos": {r: {"changed_files": rs.get("diff_files", []), "checks": rs.get("checks"), "fix_attempts_used": rs.get("fix_attempts_used")}
                  for r, rs in s["repos"].items()},
        "integration": s.get("integration_report", {}), "reviews": s.get("reviews", {}), "contract_review": s.get("contract_review", {}),
        "merge_order": s["dag"]["merge_order"], "scope_gaps": s.get("scope_gaps", []),
        "manual_test": "passed on the current code"}, ["approve", "abort"])

    def after_push_approval(state) -> str:
        if choice(state) == "abort":
            return "abort"
        return "commit_and_push" if gate(state) == "approve_push" else gate(state)  # never push untested code

    g.add_conditional_edges("approve_push_wait", after_push_approval,
                            ["abort", "commit_and_push", "integration_check", "manual_test", "repo_review"])

    def commit_and_push(state):
        vcs, key, run_id = deps.vcs(state), state["ticket_key"], state["run_id"]
        trailer = f"Devflow-Run: {run_id}"
        title = state["ticket"]["title"]
        issues, out = [], {}
        for repo in state["dag"]["merge_order"]:
            def committed():
                if vcs.is_dirty(repo):
                    return None
                return vcs.head(repo) if trailer in vcs.git(repo, "log", "-1", "--format=%B") else None

            def commit():
                vcs.git(repo, "add", "-A")
                vcs.git(repo, "commit", "-m", f"{key}: {title}", "-m", trailer)
                return vcs.head(repo)

            if vcs.is_dirty(repo) or committed():
                head = perform(store, Effect(run_id, "commit", repo, "commit"), committed, commit)["result"]
            else:
                head = vcs.head(repo)
                if head == state["repos"][repo].get("base_sha"):
                    out[repo] = {"no_changes": True}
                    continue
            remote = vcs.remote_branch_exists(repo, key)
            if remote:
                vcs.git(repo, "fetch", "origin", key)
                ahead, behind = vcs.ahead_behind(repo, key, f"origin/{key}")
                if behind:
                    issues.append({"repo": repo, "problem": f"origin/{key} has {behind} commit(s) this run does not have", "ahead": ahead})
                    continue
            perform(store, Effect(run_id, "push", repo, f"push:{head}"),
                    detect=lambda: head if vcs.remote_branch_exists(repo, key) and vcs.git(repo, "rev-parse", f"origin/{key}") == head else None,
                    act=lambda: vcs.git(repo, "push", "-u", "origin", key) or head)
            out[repo] = {"pushed_sha": head}
        return {"repos": out, "push_issues": issues}

    node("commit_and_push", commit_and_push)
    g.add_conditional_edges("commit_and_push", lambda s: "push_blocked" if s.get("push_issues") else "open_draft_mrs",
                            ["push_blocked", "open_draft_mrs"])
    checkpoint("push_blocked", lambda s: {"issues": s["push_issues"], "hint": "The workflow never force-pushes. Resolve it by hand, then retry, or abort."},
               ["retry", "abort"])
    g.add_conditional_edges("push_blocked_wait", lambda s: "abort" if choice(s) == "abort" else "commit_and_push", ["abort", "commit_and_push"])

    def open_draft_mrs(state):
        vcs, key, run_id = deps.vcs(state), state["ticket_key"], state["run_id"]
        mrs = dict(state.get("mrs") or {})
        order = [r for r in state["dag"]["merge_order"] if state["repos"][r].get("pushed_sha")]

        def find(repo):
            raw = vcs.glab(repo, "mr", "list", "--source-branch", key, "--output", "json", check=False)
            try:
                items = json.loads(raw) if raw else []
            except json.JSONDecodeError:
                return None
            return {"iid": items[0]["iid"], "url": items[0].get("web_url", "")} if items else None

        def description(repo) -> str:
            lines = [f"Jira: {state['ticket_key']} - {state['ticket']['title']}", "", "**Merge order:** " + " → ".join(order), ""]
            for r in order:
                if r != repo and r in mrs:
                    lines.append(f"- Related MR in {r}: {mrs[r]['url']}")
            lines += ["", "Tasks:"] + [f"- {t}" for t in state["repos"][repo].get("tasks", [])]
            lines += ["", f"<!-- devflow:{run_id} -->"]
            return "\n".join(lines)

        for repo in order:
            def create(repo=repo):
                vcs.glab(repo, "mr", "create", "--source-branch", key, "--target-branch", ws.repo(repo).base_branch, "--draft",
                         "--title", f"Draft: {key}: {state['ticket']['title']}", "--description", description(repo), "--yes")
                found = find(repo)
                if not found:
                    raise RuntimeError(f"MR for {repo} was not found after creation")
                return found
            mrs[repo] = perform(store, Effect(run_id, "open_draft_mrs", repo, "create_mr"), lambda repo=repo: find(repo), create)["result"]
        for repo in order:  # refresh descriptions so every MR links its siblings (idempotent overwrite)
            vcs.glab(repo, "mr", "update", str(mrs[repo]["iid"]), "--description", description(repo))
        return {"mrs": mrs}

    node("open_draft_mrs", open_draft_mrs, retry_policy=TRANSIENT)
    g.add_edge("open_draft_mrs", "jira_update")

    def jira_update(state):
        key, run_id = state["ticket_key"], state["run_id"]
        mark = marker(run_id, "delivery")
        body = "\n".join(
            [f"Draft MRs for {key} (merge in this order):"]
            + [f"- {r}: {state['mrs'][r]['url']}" for r in state["dag"]["merge_order"] if r in state["mrs"]]
            + ["", "Local verification: checks passed in every repo"
               + (", integration/E2E passed" if state.get("integration_version") == state.get("code_version") else "")
               + ", manual test passed by the developer."])
        transition = perform(store, Effect(run_id, "jira_update", key, f"transition:{ws.review_status}"),
                             detect=lambda: None, act=lambda: deps.jira.transition_if_behind(key, ws.review_status))
        comment = perform(store, Effect(run_id, "jira_update", key, "delivery_comment"),
                          detect=lambda: None, act=lambda: deps.jira.upsert_comment(key, mark, body))
        return {"jira_result": {"transition": transition["result"], "comment": comment["result"]}}

    node("jira_update", jira_update, retry_policy=TRANSIENT)
    g.add_edge("jira_update", "summary")

    def summary(state):
        pages = state.get("context", {}).get("pages", [])
        outdated = []
        if pages:
            vcs = deps.vcs(state)
            stat = {r: vcs.git(r, "diff", "--stat", f"{state['repos'][r].get('base_sha', '')}..HEAD", check=False) for r in state["dag"]["nodes"]}
            outdated = llm.structured(SYSTEM, (
                f"<confluence_pages>{_j(pages)}</confluence_pages>\n<requirement_context>{_j(state.get('context'))}</requirement_context>\n"
                f"<changes>{_j(stat)}</changes>\n\nList Confluence pages that look out of date after this change (the developer cannot "
                "edit Confluence; the page owner will). Empty if none."), Outdated, step="summary").model_dump()["pages"]
        md = _render_summary(state, outdated)
        store.set_status(state["run_id"], "COMPLETED", node="summary")
        return {"output": md}

    node("summary", summary)
    g.add_edge("summary", END)

    kit.add_abort()

    g.add_edge(START, "preflight")
    g.add_edge("preflight", "prepare_worktrees")
    g.add_edge("prepare_worktrees", "fetch_ticket")
    g.add_edge("fetch_ticket", "gather_context")
    g.add_edge("gather_context", "analyze_requirements")
    g.add_edge("analyze_requirements", "change_impact")
    return g.compile(checkpointer=checkpointer)


def _render_summary(state, outdated: list[dict]) -> str:
    key, t = state["ticket_key"], state["ticket"]
    lines = [f"# {key}: {t['title']}", "", f"Run `{state['run_id']}` completed.", "", "## Repos (merge in this order)"]
    for r in state["dag"]["merge_order"]:
        rs, mr = state["repos"][r], (state.get("mrs") or {}).get(r, {})
        lines.append(f"- **{r}**: branch `{rs.get('branch')}`, {len(rs.get('diff_files', []))} files, fix attempts {rs.get('fix_attempts_used', 0)}, MR {mr.get('url', '-')}")
    lines += ["", "## Verification (local only, no CI)", "- Checks passed in every repo",
              f"- Integration/E2E: {'passed' if state.get('integration_version') == state.get('code_version') else 'not required'}",
              "- Manual test: passed by you"]
    if state.get("scope_gaps"):
        lines += ["", "## Scope requests you denied"] + [f"- {g['repo']}: {g['reason']}" for g in state["scope_gaps"]]
    if outdated:
        lines += ["", "## Confluence pages that look out of date (for the page owner; the workflow never edits Confluence)"]
        lines += [f"- {p['page']}: {p['reason']}" for p in outdated]
    jr = state.get("jira_result") or {}
    lines += ["", f"Jira: {jr.get('transition', '')}; delivery comment {jr.get('comment', {}) if isinstance(jr.get('comment'), str) else 'updated'}"]
    return "\n".join(lines) + "\n"
