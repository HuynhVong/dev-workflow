"""address-review: the companion workflow that handles reviewer comments on the draft MRs of one ticket.

`devflow address-review AQS-5512 [--repos ...]`. Same guardrails, lifecycle, checkpointing, idempotency and scope guard
as `jira ticket implement`. It works only on the existing `<KEY>` branches (never creates one), never resolves a
thread (the reviewer does), never changes the Jira status, and never touches CI/CD.
"""
import json
import operator
from typing import Annotated

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from typing_extensions import TypedDict

from ..llm import AsStep
from . import dag as dagmod
from . import worktrees
from .graph import (CHECKPOINT_TITLES, release_run_worktrees, HIDDEN_NODES, SYSTEM, TICKET_FORM, TRANSIENT, Deps, GraphKit, _j, _tail, diff_digest,
                    check_environment, merge_repos, route_feedback, run_repo_checks)
from .jira import marker
from .ledger import Effect, perform
from .scope import ScopeGuard
from .models import ContractCheck, ContractReview, FeedbackAnalysis, ReviewFixPlan, ReviewTriage
from .vcs import Vcs

WORKFLOW = "address_review"
REVIEW_STEPS = ("classify_comments", "map_to_repos", "targeted_fix", "integration_check", "repo_review", "contract_review",
                "analyze_feedback_and_route")
REPLY_MARK = "<!-- devflow:{run_id}:{thread} -->"


DEVFLOW_UI = {
    "title": "Address review",
    "description": "Reads unresolved threads on the ticket's draft MRs, lets you triage them, fixes and re-tests, then replies.",
    "icon": "message-square", "color": "#b78cff", "form": TICKET_FORM, "checkpoints": CHECKPOINT_TITLES, "hidden_nodes": HIDDEN_NODES,
    "steps": ["preflight", "prepare_worktrees", "load_mrs", "read_discussions", "classify_comments", "triage", "map_to_repos",
              "sync_branches", "fix_repo", "integration_check", "manual_retest", "repo_review", "contract_review", "approve_push",
              "push_updates", "reply_to_discussions", "jira_refresh", "summary"],
    "nodes": {"preflight": "Preflight", "prepare_worktrees": "Prepare worktrees", "load_mrs": "Load draft MRs",
              "read_discussions": "Read review threads", "classify_comments": "Classify comments", "triage": "Triage",
              "map_to_repos": "Map fixes to repos", "sync_branches": "Sync branches", "sync_blocked": "Sync blocked",
              "fix_repo": "Fix", "budget_exhausted": "Fix budget used", "integration_check": "Integration check",
              "manual_retest": "Manual re-test", "repo_review": "Repo review", "contract_review": "Contract review",
              "analyze_feedback_and_route": "Route feedback", "route_ask": "Feedback decision", "approve_push": "Approve push",
              "push_updates": "Push updates", "push_blocked": "Push blocked", "reply_to_discussions": "Reply in threads",
              "jira_refresh": "Update Jira comment", "summary": "Summary", "nothing_to_do": "Nothing to do", "abort": "Aborted"},
}


class ReviewState(TypedDict, total=False):
    run_id: str
    ticket_key: str
    requested_repos: list[str]
    scope: dict[str, str]
    env_warnings: list[str]
    mrs: dict
    threads: list[dict]
    triage: list[dict]
    dag: dict
    integration_required: bool
    behaviour_changed: bool
    notes: list[str]
    sync_issues: list[dict]
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
    answers: Annotated[list[str], operator.add]
    pending_checkpoint: dict | None
    last_answer: dict
    decisions: Annotated[list[dict], operator.add]
    push_issues: list[dict]
    replies: dict
    jira_result: str
    output: str
    aborted_at: str


def _round_diff(vcs: Vcs, repo: str, base_sha: str) -> str:
    """Everything this round changed in a repo: commits since the synced head plus the working tree."""
    vcs.git(repo, "add", "-N", ".")
    return vcs.git(repo, "diff", base_sha)


def build_review_graph(deps: Deps, checkpointer=None):
    ws, store, llm = deps.workspace, deps.store, deps.llm
    g = StateGraph(ReviewState)
    kit = GraphKit(g, store)
    node, checkpoint, choice = kit.node, kit.checkpoint, kit.choice

    # 0 -------------------------------------------------------------------------------------------
    def preflight(state):
        scope, warnings = check_environment(deps, state, need_confluence=False, steps=REVIEW_STEPS)
        store.audit(state["run_id"], "preflight", {"scope": scope.to_state(), "warnings": warnings})
        return {"scope": scope.to_state(), "env_warnings": warnings, "code_version": 0,
                "integration_version": -1, "manual_test_version": -1, "review_version": -1}

    node("preflight", preflight)
    node("prepare_worktrees", lambda s: {"scope": ScopeGuard.create({r: deps.worktree(s, r) for r in s["scope"]}).to_state()})

    # 1 -------------------------------------------------------------------------------------------
    def load_mrs(state):
        vcs, key, mrs = deps.vcs(state), state["ticket_key"], {}
        for repo in state["scope"]:
            raw = vcs.glab(repo, "mr", "list", "--source-branch", key, "--output", "json", check=False)
            try:
                items = json.loads(raw) if raw else []
            except json.JSONDecodeError:
                items = []
            if items:
                mrs[repo] = {"iid": items[0]["iid"], "url": items[0].get("web_url", ""), "title": items[0].get("title", "")}
        return {"mrs": mrs}

    node("load_mrs", load_mrs, retry_policy=TRANSIENT)
    g.add_conditional_edges("load_mrs", lambda s: "read_discussions" if s["mrs"] else "nothing_to_do", ["read_discussions", "nothing_to_do"])

    # 2 -------------------------------------------------------------------------------------------
    def read_discussions(state):
        vcs, threads = deps.vcs(state), []
        for repo, mr in state["mrs"].items():
            raw = vcs.glab(repo, "api", f"projects/:id/merge_requests/{mr['iid']}/discussions?per_page=100")
            for d in json.loads(raw or "[]"):
                notes = [n for n in d.get("notes", []) if not n.get("system")]
                if not notes or not any(n.get("resolvable") and not n.get("resolved") for n in notes):
                    continue
                if "<!-- devflow:" in notes[-1].get("body", ""):
                    continue  # we already replied; waiting on the reviewer
                pos = notes[0].get("position") or {}
                threads.append({"id": d["id"], "repo": repo, "mr_iid": mr["iid"],
                                "file": pos.get("new_path") or pos.get("old_path") or "", "line": pos.get("new_line") or pos.get("old_line"),
                                "notes": [{"author": (n.get("author") or {}).get("username", ""), "body": n.get("body", "")} for n in notes]})
        return {"threads": threads}

    node("read_discussions", read_discussions, retry_policy=TRANSIENT)
    g.add_conditional_edges("read_discussions", lambda s: "classify_comments" if s["threads"] else "nothing_to_do",
                            ["classify_comments", "nothing_to_do"])

    # 3 -------------------------------------------------------------------------------------------
    def classify_comments(state):
        vcs = deps.vcs(state)
        diffs = {r: diff_digest(vcs.diff_against(r, f"origin/{ws.repo(r).base_branch}")) for r in state["mrs"]}
        tr = llm.structured(SYSTEM, (
            f"Ticket {state['ticket_key']}. Unresolved review threads on its draft MRs:\n<threads>{_j(state['threads'])}</threads>\n"
            f"<mr_diffs>{_j(diffs)}</mr_diffs>\n<repos_in_scope>{list(state['scope'])}</repos_in_scope>\n\n"
            "Classify every thread (one item per thread_id): must_fix, suggestion, question, out_of_scope, or disagree "
            "(say why in reply). Propose fix, answer or skip. repo is the repo that must change; it may differ from the "
            "thread's repo (e.g. a frontend comment that needs a backend change), but only from repos_in_scope. Draft a short, "
            "polite reply for each thread."), ReviewTriage, step="classify_comments")
        by_id = {t["id"]: t for t in state["threads"]}
        items = []
        for n, it in enumerate([i for i in tr.items if i.thread_id in by_id], start=1):
            d = it.model_dump()
            t = by_id[it.thread_id]
            if d["proposed_action"] == "fix" and d["repo"] not in state["scope"]:
                d.update(category="out_of_scope", proposed_action="answer",
                         reply=d["reply"] or f"This needs a change in {d['repo']}, which is outside this ticket's repos.")
            d.update(n=n, thread_repo=t["repo"], mr_iid=t["mr_iid"], file=t["file"], line=t["line"],
                     comment=t["notes"][0]["body"][:500], repo=d["repo"] if d["proposed_action"] == "fix" else t["repo"])
            items.append(d)
        missing = [t for t in state["threads"] if t["id"] not in {i["thread_id"] for i in items}]
        for t in missing:  # never drop a thread silently: it is shown to you as skip
            items.append({"n": len(items) + 1, "thread_id": t["id"], "category": "question", "repo": t["repo"], "thread_repo": t["repo"],
                          "mr_iid": t["mr_iid"], "file": t["file"], "line": t["line"], "comment": t["notes"][0]["body"][:500],
                          "summary": "not classified", "proposed_action": "skip", "fix_instructions": "", "reply": "",
                          "behaviour_change": False, "contract_change": False})
        return {"triage": items}

    node("classify_comments", classify_comments)
    g.add_edge("classify_comments", "triage")

    # 4 -------------------------------------------------------------------------------------------
    checkpoint("triage", lambda s: {
        "threads": [{k: i[k] for k in ("n", "category", "repo", "file", "line", "comment", "summary", "proposed_action", "reply")}
                    for i in s["triage"]],
        "warnings": s.get("env_warnings", []),
        "hint": "approve = take the proposed actions; edit = set fix / answer / skip lists (thread numbers), note = "
                "extra instructions for every fix. Replies are posted only after approve_push (or right away when nothing "
                "needs code). Threads are never resolved by the workflow."}, ["approve", "edit", "abort"])

    def apply_triage(state):
        ans = state["last_answer"]
        items = [dict(i) for i in state["triage"]]
        if ans["choice"] == "edit":
            pick = {str(x): a for a in ("fix", "answer", "skip") for x in ans.get(a) or []}
            for i in items:
                a = pick.get(str(i["n"])) or pick.get(i["thread_id"])
                if a:
                    i["proposed_action"] = a
                if i["proposed_action"] == "fix" and i["repo"] not in state["scope"]:
                    i["repo"] = i["thread_repo"]
        notes = [ans["note"]] if ans.get("note") else []
        store.audit(state["run_id"], "triage", {a: [i["n"] for i in items if i["proposed_action"] == a] for a in ("fix", "answer", "skip")})
        return {"triage": items, "notes": notes}

    node("apply_triage", apply_triage)
    g.add_conditional_edges("triage_wait", lambda s: "abort" if choice(s) == "abort" else "apply_triage", ["abort", "apply_triage"])

    def after_triage(state) -> str:
        acts = {i["proposed_action"] for i in state["triage"]}
        return "map_to_repos" if "fix" in acts else "reply_to_discussions" if "answer" in acts else "summary"

    g.add_conditional_edges("apply_triage", after_triage, ["map_to_repos", "reply_to_discussions", "summary"])

    # 5 -------------------------------------------------------------------------------------------
    def map_to_repos(state):
        fixes = [i for i in state["triage"] if i["proposed_action"] == "fix"]
        repos = list(dict.fromkeys(i["repo"] for i in fixes))
        edges, integration, note = [], any(i["contract_change"] for i in fixes), ""
        if len(repos) > 1:
            fp = llm.structured(SYSTEM, (f"<fixes>{_j(fixes)}</fixes>\n<repos>{repos}</repos>\n\nWhich of these repos must be fixed "
                                         "before another (e.g. a backend contract the frontend follows)? Only edges between these repos."),
                                ReviewFixPlan, step="map_to_repos")
            edges = [(e.upstream, e.downstream) for e in fp.edges]
            integration = integration or fp.integration_required
            if dagmod.validate(repos, edges, set(state["scope"])):
                edges = list(zip(repos, repos[1:]))  # unusable graph: fall back to one repo at a time, in workspace order
                note = "The proposed fix order was not a valid DAG, so repos are fixed one at a time."
        waves = dagmod.waves(repos, edges)
        work = {r: {"status": "pending", "fix_attempts_used": 0, "implemented": False, "branch": state["ticket_key"],
                    "fix_instructions": [f"Thread #{i['n']} ({i['file']}:{i['line']}): {i['comment']}\n-> {i['fix_instructions']}" for i in fixes if i["repo"] == r]
                    + [f"Developer note: {n}" for n in state.get("notes", [])]} for r in repos}
        return {"dag": {"nodes": repos, "edges": edges, "waves": waves, "merge_order": [r for w in waves for r in w], "note": note},
                "repos": work, "integration_required": integration,
                "behaviour_changed": any(i["behaviour_change"] for i in fixes)}

    node("map_to_repos", map_to_repos)
    g.add_edge("map_to_repos", "sync_branches")

    # 6 -------------------------------------------------------------------------------------------
    def sync_branches(state):
        vcs, key, issues, out = deps.vcs(state), state["ticket_key"], [], {}
        for repo in state["dag"]["nodes"]:
            if state["repos"][repo].get("base_sha"):
                continue  # already synced in this run
            vcs.git(repo, "fetch", "origin")
            if not vcs.remote_branch_exists(repo, key):
                issues.append({"repo": repo, "problem": f"origin has no branch {key}; address-review never creates branches"})
                continue
            if vcs.is_dirty(repo):
                issues.append({"repo": repo, "problem": "uncommitted changes in the working tree"})
                continue
            vcs.git(repo, "checkout", key)
            r = vcs.run(repo, "git", "pull", "--ff-only", "origin", key, check=False)
            if r.returncode != 0:
                issues.append({"repo": repo, "problem": f"`pull --ff-only origin {key}` failed (a reviewer push conflicts with local commits?): "
                                                        f"{(r.stderr or r.stdout).strip()[:300]}"})
                continue
            out[repo] = {"base_sha": vcs.head(repo)}
        return {"repos": out, "sync_issues": issues}

    node("sync_branches", sync_branches)
    g.add_conditional_edges("sync_branches", lambda s: "sync_blocked" if s.get("sync_issues") else "schedule", ["sync_blocked", "schedule"])
    checkpoint("sync_blocked", lambda s: {"issues": s["sync_issues"],
                                          "hint": "Nothing was changed. Resolve it by hand (the workflow never rebases, resets or forces), then retry, or abort."},
               ["retry", "abort"])
    g.add_conditional_edges("sync_blocked_wait", lambda s: "abort" if choice(s) == "abort" else "sync_branches", ["abort", "sync_branches"])

    # 7 -------------------------------------------------------------------------------------------
    node("schedule", lambda s: {})

    def gate(state) -> str:
        cv = state.get("code_version", 0)
        if state.get("integration_required") and state.get("integration_version") != cv:
            return "integration_check"
        if state.get("behaviour_changed") and state.get("manual_test_version") != cv:
            return "manual_retest"
        if state.get("review_version") != cv:
            return "repo_review"
        return "approve_push"

    def route_schedule(state):
        repos, edges = state["repos"], [tuple(e) for e in state["dag"]["edges"]]
        if any(r["status"] == "blocked_budget" for r in repos.values()):
            return "budget_exhausted"
        todo = [n for n, r in repos.items() if r["status"] in ("pending", "needs_fix", "needs_checks")]
        if not todo:
            return gate(state)
        ready = [n for n in todo if all(repos[u]["status"] == "ready" for u in dagmod.upstream_of(n, edges))]
        if not ready:
            return "budget_exhausted"
        return [Send("fix_repo", {"payload_run_id": state["run_id"], "repo": n, "repo_state": repos[n], "scope": state["scope"],
                                  "ticket_key": state["ticket_key"], "upstream": dagmod.upstream_of(n, edges),
                                  "upstream_base": {u: repos[u]["base_sha"] for u in dagmod.upstream_of(n, edges)}}) for n in ready]

    g.add_conditional_edges("schedule", route_schedule,
                            ["fix_repo", "budget_exhausted", "integration_check", "manual_retest", "repo_review", "approve_push"])

    def fix_repo(payload):
        repo, rs = payload["repo"], dict(payload["repo_state"])
        st = {"scope": payload["scope"]}
        coder, vcs, cap, path = deps.coder(st), deps.vcs(st), ws.max_fix_attempts, payload["scope"][payload["repo"]]
        changed, mode = 0, rs["status"]
        feedback = list(rs.get("fix_instructions", []))
        while True:
            if mode in ("pending", "needs_fix"):
                if rs.get("implemented"):
                    if rs.get("fix_attempts_used", 0) >= cap:  # this run's budget: never reset, never exceeded
                        rs["status"] = "blocked_budget"
                        break
                    rs["fix_attempts_used"] = rs.get("fix_attempts_used", 0) + 1
                upstream = "".join(f"<upstream_repo name='{u}' already_fixed='true'>\n{_tail(_round_diff(vcs, u, b), 15000)}\n</upstream_repo>\n"
                                   for u, b in payload["upstream_base"].items())
                res = coder.implement(repo, path, step="targeted_fix", escalate=rs.get("fix_attempts_used", 0) >= cap, instructions=(
                    f"Ticket {payload['ticket_key']}: address code review comments on branch {payload['ticket_key']}.\n{upstream}"
                    "<fix_this>\n" + "\n---\n".join(feedback) + "\n</fix_this>\n"
                    "Change only what these comments need. Keep the rest of the branch as it is. Update tests when behaviour changes."))
                rs["implemented"], changed = True, changed + 1
                rs["summary"] = res.summary
                store.audit(payload["payload_run_id"], "fix", {"repo": repo, "attempt": rs.get("fix_attempts_used", 0), "ok": res.ok})
            ok, results = run_repo_checks(deps, repo, payload["scope"])
            rs["checks"] = results
            if ok:
                rs["status"], rs["fix_instructions"] = "ready", []
                break
            if mode == "needs_checks":
                rs["status"] = "blocked_budget"
                break
            mode = "needs_fix"
            feedback = [f"Checks failed:\n{_j(results)}"]
        rs["diff_files"] = vcs.changed_files(repo)
        return {"repos": {repo: rs}, "code_version": changed}

    node("fix_repo", fix_repo)
    g.add_edge("fix_repo", "schedule")

    checkpoint("budget_exhausted", lambda s: {
        "repos": {n: {"fix_attempts_used": r.get("fix_attempts_used"), "checks": r.get("checks"), "summary": r.get("summary")}
                  for n, r in s["repos"].items() if r["status"] in ("blocked_budget", "pending", "needs_fix")},
        "hint": f"These repos used their {ws.max_fix_attempts} automated fix attempts for this run. Fix them by hand, then "
                "choose fixed_by_hand (checks re-run with no automated edit), or abort."}, ["fixed_by_hand", "abort"])
    node("mark_hand_fixed", lambda s: {"repos": {n: {"status": "needs_checks"} for n, r in s["repos"].items() if r["status"] == "blocked_budget"}})
    g.add_conditional_edges("budget_exhausted_wait", lambda s: "abort" if choice(s) == "abort" else "mark_hand_fixed", ["abort", "mark_hand_fixed"])
    g.add_edge("mark_hand_fixed", "schedule")

    def diffs(state, n=30000) -> dict:
        vcs = deps.vcs(state)
        return {r: _tail(_round_diff(vcs, r, rs["base_sha"]), n) for r, rs in state["repos"].items()}

    fixes_block = lambda s: _j([{k: i[k] for k in ("n", "repo", "comment", "fix_instructions")} for i in s["triage"] if i["proposed_action"] == "fix"])  # noqa: E731

    # 8 -------------------------------------------------------------------------------------------
    def integration_check(state):
        """No AI first: the repos' own `integration` commands run first; the AI contract check only when none is set."""
        nodes, feedback, report = state["dag"]["nodes"], [], {"commands": []}
        for r in nodes:
            cmd = ws.repo(r).commands.get("integration")
            if cmd:
                res = deps.run_cmd(cmd, state["scope"][r], worktrees.env_for(state["scope"]))
                out = _tail((res.stdout or "") + (res.stderr or ""), 3000)
                report["commands"].append({"repo": r, "ok": res.returncode == 0, "output": out})
                if res.returncode != 0:
                    feedback.append({"source": "integration", "repos": [r], "text": f"`{cmd}` failed:\n{out}"})
        if not report["commands"]:
            cc = llm.structured(SYSTEM, f"<review_fixes>{fixes_block(state)}</review_fixes>\n<diffs_this_round>{_j(diffs(state))}</diffs_this_round>\n\n"
                                "Check that producer and consumer repos still agree after these review fixes (routes, fields, types, "
                                "events, migrations).", ContractCheck, step="integration_check")
            report["contracts"] = cc.model_dump()
            feedback = [{"source": "integration", "repos": i.repos, "text": i.issue} for i in cc.issues]
        update = {"integration_report": report, "pending_feedback": feedback}
        if not feedback:
            update["integration_version"] = state.get("code_version", 0)
        return update

    node("integration_check", integration_check)
    g.add_conditional_edges("integration_check", lambda s: "analyze_feedback_and_route" if s.get("pending_feedback") else gate(s),
                            ["analyze_feedback_and_route", "manual_retest", "repo_review", "approve_push"])

    # 9 -------------------------------------------------------------------------------------------
    checkpoint("manual_retest", lambda s: {
        "repos": {r: {"path": s["scope"][r], "branch": s["ticket_key"], "changed_files": rs.get("diff_files", []),
                      "run": ws.repo(r).commands.get("run", "")} for r, rs in s["repos"].items()},
        "threads_fixed": [{k: i[k] for k in ("n", "repo", "summary")} for i in s["triage"] if i["proposed_action"] == "fix"],
        "hint": "The fixes change behaviour, so test them. Nothing is committed yet. ok = works; feedback = what's wrong."},
        ["ok", "feedback", "abort"])
    node("manual_ok", lambda s: {"manual_test_version": s.get("code_version", 0)})
    node("manual_feedback", lambda s: {"pending_feedback": [{"source": "manual_test", "repos": s["last_answer"].get("repos", []),
                                                              "text": s["last_answer"].get("note", "")}]})
    g.add_conditional_edges("manual_retest_wait", lambda s: {"ok": "manual_ok", "feedback": "manual_feedback", "abort": "abort"}[choice(s)],
                            ["manual_ok", "manual_feedback", "abort"])
    g.add_conditional_edges("manual_ok", gate, ["integration_check", "manual_retest", "repo_review", "approve_push"])
    g.add_edge("manual_feedback", "analyze_feedback_and_route")

    # 10 ------------------------------------------------------------------------------------------
    def repo_review(state):
        from ..workflows import pr_review
        reviewer = pr_review.build_graph(AsStep(llm, "repo_review"))
        reviews, feedback = {}, []
        for repo, diff in diffs(state, 60000).items():
            out = reviewer.invoke({"title": f"{state['ticket_key']} review fixes ({repo})", "description": fixes_block(state),
                                   "diff": diff, "findings": []})
            reviews[repo] = {"decision": out["verdict"].decision, "summary": out["verdict"].summary,
                             "findings": [f.model_dump() for f in out["findings"]]}
            feedback += [{"source": "review", "repos": [repo], "text": f"[{f.severity}] {f.file}:{f.line or '?'} {f.title}: {f.detail} -> {f.suggestion}"}
                         for f in out["findings"] if f.severity in ("blocker", "major")]
        return {"reviews": reviews, "pending_feedback": feedback}

    def contract_review(state):
        cr = llm.structured(SYSTEM, (f"<review_fixes>{fixes_block(state)}</review_fixes>\n<merge_order>{state['dag']['merge_order']}</merge_order>\n"
                                     f"<diffs_this_round>{_j(diffs(state))}</diffs_this_round>\n\nCross-repo review of these fixes: contracts "
                                     "still agree, migrations match, merge order still safe, and each thread's ask is actually addressed. "
                                     "Only real blockers."), ContractReview, step="contract_review")
        feedback = list(state.get("pending_feedback") or []) + [{"source": "contract_review", "repos": b.repos, "text": b.issue} for b in cr.blockers]
        update = {"contract_review": cr.model_dump(), "pending_feedback": feedback}
        if not feedback:
            update["review_version"] = state.get("code_version", 0)
        return update

    node("repo_review", repo_review)
    node("contract_review", contract_review)
    g.add_edge("repo_review", "contract_review")
    g.add_conditional_edges("contract_review", lambda s: "analyze_feedback_and_route" if s.get("pending_feedback") else gate(s),
                            ["analyze_feedback_and_route", "integration_check", "manual_retest", "repo_review", "approve_push"])

    # feedback routing (same rules as jira ticket implement, but scope never widens here) --------------------
    def analyze_feedback_and_route(state):
        items_in, repos = state.get("pending_feedback") or [], state["repos"]
        status = {r: {"fix_attempts_used": rs.get("fix_attempts_used", 0), "status": rs["status"]} for r, rs in repos.items()}
        answers = "\n".join(state.get("answers", []))
        fa = llm.structured(SYSTEM, (
            f"Ticket {state['ticket_key']}, review-fix round.\n<review_fixes>{fixes_block(state)}</review_fixes>\n<dag>{_j(state['dag'])}</dag>\n"
            f"<repos_in_scope>{list(repos)}</repos_in_scope>\n<repo_status>{_j(status)}</repo_status>\n"
            + (f"<developer_answers>{answers}</developer_answers>\n" if answers else "")
            + f"<feedback>{_j(items_in)}</feedback>\n\nClassify each feedback item: which repos must change, the cause and your "
            "confidence. requirement_gap when the ask itself is unclear, scope_issue when a repo outside repos_in_scope must "
            "change, unclear when the evidence does not point to a cause."), FeedbackAnalysis, step="analyze_feedback_and_route")
        fixes, ask, outside = route_feedback(fa, repos, repos, [tuple(e) for e in state["dag"]["edges"]], ws.max_fix_attempts)
        ask += [{"source": "analyzer", "category": "scope_issue", "repos": [o["repo"]], "cause": o["reason"],
                 "why_asking": "outside the repos being fixed in this round"} for o in outside]
        if not fa.items:
            ask = [{"source": "analyzer", "category": "unclear", "cause": "no actionable item found", "repos": [], "feedback": items_in, "why_asking": "unclear"}]
        store.audit(state["run_id"], "feedback_routed", {"fixes": list(fixes), "ask": len(ask)})
        return {"feedback_items": [i.model_dump() for i in fa.items], "pending_feedback": [], "route_ask_items": ask,
                "repos": {r: {"status": "needs_fix", "fix_instructions": repos[r].get("fix_instructions", []) + msgs} for r, msgs in fixes.items()}}

    node("analyze_feedback_and_route", analyze_feedback_and_route)
    g.add_conditional_edges("analyze_feedback_and_route", lambda s: "route_ask" if s.get("route_ask_items") else "schedule", ["route_ask", "schedule"])

    checkpoint("route_ask", lambda s: {"items": s["route_ask_items"],
                                       "hint": "answer = give the missing decision in note (re-analyzed); fix = name repos + instructions "
                                               "in note (only repos with attempts left); skip = ignore these items."},
               ["answer", "fix", "skip", "abort"])

    def apply_route_ask(state):
        ans, items = state["last_answer"], state["route_ask_items"]
        if ans["choice"] == "answer":
            return {"answers": [ans.get("note", "")], "route_ask_items": [],
                    "pending_feedback": [{"source": i.get("source"), "repos": i.get("repos", []), "text": i.get("cause", "")} for i in items]
                    + [{"source": "developer", "repos": [], "text": ans.get("note", "")}]}
        if ans["choice"] == "fix":
            update = {}
            for r in ans.get("repos") or []:
                rs = state["repos"].get(r)
                if rs is not None:
                    update[r] = ({"status": "blocked_budget"} if rs.get("fix_attempts_used", 0) >= ws.max_fix_attempts
                                 else {"status": "needs_fix", "fix_instructions": rs.get("fix_instructions", []) + [ans.get("note", "")]})
            return {"repos": update, "route_ask_items": []}
        cv, sources = state.get("code_version", 0), " ".join(str(i.get("source", "")) for i in items).lower()
        update = {"route_ask_items": []}
        if "integration" in sources:
            update["integration_version"] = cv
        if "review" in sources:
            update["review_version"] = cv
        return update

    node("apply_route_ask", apply_route_ask)
    g.add_conditional_edges("route_ask_wait", lambda s: "abort" if choice(s) == "abort" else "apply_route_ask", ["abort", "apply_route_ask"])
    g.add_conditional_edges("apply_route_ask", lambda s: "analyze_feedback_and_route" if s.get("pending_feedback") else "schedule",
                            ["analyze_feedback_and_route", "schedule"])

    # 11 ------------------------------------------------------------------------------------------
    checkpoint("approve_push", lambda s: {
        "repos": {r: {"changed_files": rs.get("diff_files", []), "checks": rs.get("checks"), "fix_attempts_used": rs.get("fix_attempts_used")}
                  for r, rs in s["repos"].items()},
        "replies": [{k: i[k] for k in ("n", "proposed_action", "reply")} for i in s["triage"] if i["proposed_action"] in ("fix", "answer")],
        "reviews": s.get("reviews", {}), "contract_review": s.get("contract_review", {}), "merge_order": s["dag"]["merge_order"],
        "manual_test": "passed on the current code" if s.get("behaviour_changed") else "not needed (no behaviour change)",
        "note": s["dag"].get("note", "")}, ["approve", "abort"])

    def after_push_approval(state) -> str:
        if choice(state) == "abort":
            return "abort"
        return "push_updates" if gate(state) == "approve_push" else gate(state)

    g.add_conditional_edges("approve_push_wait", after_push_approval, ["abort", "push_updates", "integration_check", "manual_retest", "repo_review"])

    # 12 ------------------------------------------------------------------------------------------
    def push_updates(state):
        vcs, key, run_id = deps.vcs(state), state["ticket_key"], state["run_id"]
        trailer = f"Devflow-Run: {run_id}"
        issues, out = [], {}
        for repo in state["dag"]["merge_order"]:
            def committed(repo=repo):
                if vcs.is_dirty(repo):
                    return None
                return vcs.head(repo) if trailer in vcs.git(repo, "log", "-1", "--format=%B") else None

            def commit(repo=repo):
                vcs.git(repo, "add", "-A")
                vcs.git(repo, "commit", "-m", f"{key}: address review comments", "-m", trailer)
                return vcs.head(repo)

            if vcs.is_dirty(repo) or committed():
                head = perform(store, Effect(run_id, "commit", repo, "commit"), committed, commit)["result"]
            else:
                head = vcs.head(repo)
                if head == state["repos"][repo]["base_sha"]:
                    out[repo] = {"no_changes": True}
                    continue
            vcs.git(repo, "fetch", "origin", key)
            ahead, behind = vcs.ahead_behind(repo, key, f"origin/{key}")
            if behind:
                issues.append({"repo": repo, "problem": f"origin/{key} has {behind} new commit(s) since this round started", "ahead": ahead})
                continue
            perform(store, Effect(run_id, "push", repo, f"push:{head}"),
                    detect=lambda repo=repo, head=head: head if vcs.git(repo, "rev-parse", f"origin/{key}") == head else None,
                    act=lambda repo=repo, head=head: vcs.git(repo, "push", "origin", key) or head)
            out[repo] = {"pushed_sha": head}
        return {"repos": out, "push_issues": issues}

    node("push_updates", push_updates)
    g.add_conditional_edges("push_updates", lambda s: "push_blocked" if s.get("push_issues") else "reply_to_discussions",
                            ["push_blocked", "reply_to_discussions"])
    checkpoint("push_blocked", lambda s: {"issues": s["push_issues"], "hint": "The workflow never force-pushes or rebases. Merge by hand, then retry, or abort."},
               ["retry", "abort"])
    g.add_conditional_edges("push_blocked_wait", lambda s: "abort" if choice(s) == "abort" else "push_updates", ["abort", "push_updates"])

    # 13 ------------------------------------------------------------------------------------------
    def reply_to_discussions(state):
        vcs, run_id = deps.vcs(state), state["run_id"]
        repos, replies = state.get("repos") or {}, {}
        for it in state["triage"]:
            if it["proposed_action"] not in ("fix", "answer"):
                continue
            body = it["reply"] or ("Done." if it["proposed_action"] == "fix" else "")
            sha = repos.get(it["repo"], {}).get("pushed_sha") if it["proposed_action"] == "fix" else None
            if sha:
                body += f"\n\nFixed in {it['repo']} commit {sha[:10]}."
            mark = REPLY_MARK.format(run_id=run_id, thread=it["thread_id"])
            text = f"{body}\n\n{mark}"
            repo, base = it["thread_repo"], f"projects/:id/merge_requests/{it['mr_iid']}/discussions"

            def mine(repo=repo, base=base, it=it, mark=mark):
                for d in json.loads(vcs.glab(repo, "api", f"{base}?per_page=100") or "[]"):
                    if d["id"] == it["thread_id"]:
                        return next((n for n in d.get("notes", []) if mark in n.get("body", "")), None)
                return None

            def detect(mine=mine, text=text):
                n = mine()
                return n["id"] if n and n.get("body") == text else None

            def act(mine=mine, repo=repo, base=base, it=it, text=text):
                n = mine()
                if n:  # an earlier reply of this run: edit it, never post a second one
                    vcs.glab(repo, "api", "--method", "PUT", f"{base}/{it['thread_id']}/notes/{n['id']}", "-f", f"body={text}")
                    return n["id"]
                return json.loads(vcs.glab(repo, "api", "--method", "POST", f"{base}/{it['thread_id']}/notes", "-f", f"body={text}") or "{}").get("id")

            replies[it["thread_id"]] = perform(store, Effect(run_id, "reply", repo, f"reply:{it['thread_id']}"), detect, act)["result"]
        return {"replies": replies}

    node("reply_to_discussions", reply_to_discussions, retry_policy=TRANSIENT)
    g.add_edge("reply_to_discussions", "jira_refresh")

    # 14 ------------------------------------------------------------------------------------------
    def jira_refresh(state):
        key, run_id, items = state["ticket_key"], state["run_id"], state["triage"]
        count = {a: sum(1 for i in items if i["proposed_action"] == a) for a in ("fix", "answer", "skip")}
        pushed = {r: rs["pushed_sha"][:10] for r, rs in (state.get("repos") or {}).items() if rs.get("pushed_sha")}
        section = (f"{count['fix']} review thread(s) fixed, {count['answer']} answered, {count['skip']} left for later. "
                   + (f"Pushed: {', '.join(f'{r} {s}' for r, s in pushed.items())}. " if pushed else "")
                   + ("Manual re-test passed. " if state.get("behaviour_changed") and pushed else "") + "MRs stay draft.")
        res = perform(store, Effect(run_id, "jira_refresh", key, "delivery_comment"), detect=lambda: None,
                      act=lambda: deps.jira.refresh_delivery_comment(key, marker(run_id, "review"), section))
        return {"jira_result": res["result"]}

    node("jira_refresh", jira_refresh, retry_policy=TRANSIENT)
    g.add_edge("jira_refresh", "summary")

    # 15 ------------------------------------------------------------------------------------------
    def summary(state):
        key, items = state["ticket_key"], state.get("triage") or []
        lines = [f"# {key}: review round", "", f"Run `{state['run_id']}` completed. Threads are left for the reviewers to resolve.", ""]
        for a, title in (("fix", "Fixed"), ("answer", "Answered"), ("skip", "Skipped")):
            sel = [i for i in items if i["proposed_action"] == a]
            if sel:
                lines += [f"## {title}"] + [f"- #{i['n']} {i['thread_repo']} {i['file']}:{i['line']}: {i['summary']}" for i in sel] + [""]
        for r, rs in (state.get("repos") or {}).items():
            lines.append(f"- **{r}**: {'pushed ' + rs['pushed_sha'][:10] if rs.get('pushed_sha') else 'no new commit'}, "
                         f"fix attempts {rs.get('fix_attempts_used', 0)}, MR {(state.get('mrs') or {}).get(r, {}).get('url', '-')}")
        if state.get("jira_result"):
            lines.append(f"\nJira delivery comment: {state['jira_result']} (status unchanged)")
        store.set_status(state["run_id"], "COMPLETED", node="summary")
        return {"output": "\n".join(lines) + "\n" + release_run_worktrees(deps, state).strip() + "\n"}

    node("summary", summary)
    g.add_edge("summary", END)

    def nothing_to_do(state):
        why = "no open MR for this ticket in the repos in scope" if not state.get("mrs") else "no unresolved review threads"
        store.set_status(state["run_id"], "COMPLETED", node="nothing_to_do", detail=why)
        return {"output": f"Run {state['run_id']} completed: {why}. Nothing was changed."}

    node("nothing_to_do", nothing_to_do)
    g.add_edge("nothing_to_do", END)
    kit.add_abort()

    g.add_edge(START, "preflight")
    g.add_edge("preflight", "prepare_worktrees")
    g.add_edge("prepare_worktrees", "load_mrs")
    return g.compile(checkpointer=checkpointer)
