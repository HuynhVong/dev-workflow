"""Ticket review: review the commits that implement a Jira ticket against that ticket, prove it in the browser, and
post the result on the ticket (design: docs/ticket-review-design.md).

`devflow review AQS-5512 --commit web-portal=3f9a1c2 --commit api-service=88be0d4,a17c3e9`

It reviews the developer's own checkout (no worktree): the developer refreshes and checks out the branch, devflow
only verifies it with read-only `rtk git`. It never commits, pushes, switches branches, calls glab, touches CI/CD or
writes to Confluence. The only writes are to Jira (one comment and the proof screenshots), after the developer approves
the exact comment. When Jira can't take them, the comment and screenshots are saved locally to post by hand.
"""
import operator
import re
import shutil
from pathlib import Path
from typing import Annotated

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from .. import doctor
from .confluence import compact_pages, page_ids_from_urls
from .graph import SYSTEM, TRANSIENT, Deps, GraphKit, PreflightFailed, _j, _tail, diff_digest
from .jira import IMAGE_EXT, JiraWriteRefused, marker
from .ledger import Effect, perform
from .mcp_config import has_playwright
from .models import CommentDraft, Coverage, TestPlanDraft, TicketUnderstanding
from .scope import ScopeGuard

WORKFLOW = "ticket_review"
STEPS = ("ticket_review.understand", "pr_review.triage", "pr_review.lens", "pr_review.verdict", "ticket_review.coverage",
         "ticket_review.test_plan", "ticket_review.e2e", "ticket_review.comment")
SHA = re.compile(r"^[0-9a-fA-F]{7,40}$")
DIFF_LIMIT = 80000

FORM = [
    {"name": "ticket", "label": "Jira ticket", "type": "ticket", "required": True, "placeholder": "AQS-5512"},
    {"name": "commits", "label": "Commits per repo", "type": "list", "required": True, "mono": True,
     "placeholder": "web-portal=3f9a1c2\napi-service=88be0d4,a17c3e9",
     "help": "One line per changed repo: <repo from workspace.yaml>=<sha>[,<sha>...]"},
    {"name": "app_url", "label": "App URL", "type": "text",
     "help": "Where the app runs for the E2E tests. Default: app_url of the repo in workspace.yaml. You can also give it at the checkout step."},
]
CHECKPOINTS = {"checkout_gate": "Check out the commits", "approve_test_plan": "Approve the test plan",
               "human_step": "A test needs you", "review_results": "Review the test results",
               "approve_comment": "Approve the Jira comment"}
DEVFLOW_UI = {
    "title": "Ticket review",
    "description": "Reviews a ticket's commits against the ticket, runs your approved test plan with Playwright and posts the proof to Jira after you approve.",
    "icon": "search-code", "color": "#eeb747", "form": FORM, "checkpoints": CHECKPOINTS, "run_prefix": "qa",
    "hidden_nodes": ["verify_checkout", "apply_test_plan", "apply_human_step", "apply_results", "apply_comment"],
    "steps": ["preflight", "checkout_gate", "understand_ticket", "code_review", "test_plan", "approve_test_plan", "run_case",
              "review_results", "draft_comment", "approve_comment", "post_to_jira", "summary"],
    "nodes": {"preflight": "Preflight", "checkout_gate": "Check out", "verify_checkout": "Verify checkout",
              "understand_ticket": "Understand the ticket", "code_review": "Code review", "test_plan": "Test plan",
              "approve_test_plan": "Approve test plan", "run_case": "E2E test", "human_step": "Your step",
              "review_results": "Test results", "draft_comment": "Draft comment", "approve_comment": "Approve comment",
              "post_to_jira": "Post to Jira", "summary": "Summary", "abort": "Aborted"},
    "node_details": {"preflight": "Jira MCP, Playwright MCP, rtk, repos and the ticket checked",
                     "checkout_gate": "You check out the branch with the commits and start the app",
                     "understand_ticket": "Ticket, attachments and linked Confluence pages (read only)",
                     "code_review": "Lens review of the commits plus acceptance-criteria coverage",
                     "test_plan": "E2E cases mapped to the acceptance criteria", "approve_test_plan": "Your decision",
                     "run_case": "Playwright runs one case and saves the proof screenshots",
                     "review_results": "Your decision", "draft_comment": "Ticket review comment for Jira",
                     "approve_comment": "Your decision", "post_to_jira": "One comment and the screenshots (or saved locally)",
                     "summary": "Final summary"},
}


class State(TypedDict, total=False):
    run_id: str
    ticket_key: str
    requested_repos: list[str]
    commits: dict[str, list[str]]
    app_url: str
    scope: dict[str, str]
    env_warnings: list[str]
    jira_access: dict
    confluence_ok: bool
    checkout: dict
    checkout_issues: list[dict]
    ticket: dict
    understanding: dict
    reviews: dict
    coverage: dict
    test_plan: dict
    plan_notes: Annotated[list[str], operator.add]
    run_cases: list[str]
    tester_notes: Annotated[list[str], operator.add]
    results: dict
    human_inputs: dict
    human_ask: dict | None
    comment: str
    conclusion: str
    uploads: list[str]
    comment_notes: Annotated[list[str], operator.add]
    jira_result: dict
    pending_checkpoint: dict | None
    last_answer: dict
    decisions: Annotated[list[dict], operator.add]
    output: str
    aborted_at: str


def parse_commits(values) -> dict[str, list[str]]:
    """["web=abc1234", "api=88be0d4,a17c3e9"] or {"web": "abc1234"} -> {"web": ["abc1234"], "api": [...]}."""
    items = values.items() if isinstance(values, dict) else (
        (line.partition("=")[0], line.partition("=")[2]) for line in (values or []) if str(line).strip())
    out: dict[str, list[str]] = {}
    for repo, shas in items:
        shas = shas if isinstance(shas, list) else re.split(r"[,\s]+", str(shas))
        out.setdefault(str(repo).strip(), []).extend(s.strip() for s in shas if s and s.strip())
    return out


def inputs(values: dict, ws) -> dict:
    """Extra start inputs for this ticket workflow (the runner adds run_id and ticket_key)."""
    commits = parse_commits(values.get("commits"))
    url = str(values.get("app_url") or "").strip() or next((ws.repos[r].app_url for r in commits if r in ws.repos and ws.repos[r].app_url), "")
    return {"commits": commits, "app_url": url, "requested_repos": list(commits)}


DEVFLOW_UI["inputs"] = inputs


def build_graph(deps: Deps, checkpointer=None):
    ws, store, llm = deps.workspace, deps.store, deps.llm
    g = StateGraph(State)
    kit = GraphKit(g, store)
    node, checkpoint, choice = kit.node, kit.checkpoint, kit.choice
    prefix = ws.vcs_prefix

    def run_dir(state) -> Path:
        return Path(ws.state_dir, state["run_id"])

    # 0 preflight -----------------------------------------------------------------------------------------------
    def preflight(state):
        gaps, warnings = [], []
        commits = state.get("commits") or {}
        if not commits:
            gaps.append("no commits given: --commit <repo>=<sha>[,<sha>] (one per changed repo)")
        for repo, shas in commits.items():
            if repo not in ws.repos:
                gaps.append(f"'{repo}' is not a repo in workspace.yaml (known: {', '.join(ws.repos)})")
            if not shas:
                gaps.append(f"{repo}: no commit SHA given")
            gaps += [f"{repo}: '{s}' is not a commit SHA" for s in shas if not SHA.match(s)]
        repos = {r: ws.repos[r].path for r in commits if r in ws.repos}
        for r, path in repos.items():
            if not Path(path, ".git").exists():
                gaps.append(f"{r}: {path} is not a git repository (fix `path` in workspace.yaml)")
        blocking = lambda checks: [c.detail for c in checks if c.status == "fail" and c.blocking]  # noqa: E731
        gaps += blocking(doctor.tool_checks(ws, deps.which, []))
        if not has_playwright(ws):
            gaps.append("no Playwright MCP: add a `playwright` server under mcp_servers in workspace.yaml (npx @playwright/mcp@latest)")
        access = {"can_post": False, "can_attach": False, "reason": ""}
        try:
            rep = deps.jira.access_report()
            if not rep["read"]:
                gaps.append(f"Jira MCP cannot read tickets (missing {rep['missing_read']}); run `devflow setup`")
            access = {"can_post": rep["comment"], "can_attach": rep["attach"],
                      "reason": "" if rep["comment"] else f"the Jira MCP has no comment tool ({', '.join(rep['missing_write'])}); read-only mode?"}
            if not rep["comment"]:
                warnings.append(f"Jira posting is off: {access['reason']} The comment will be saved for you to post by hand.")
            elif not rep["attach"]:
                warnings.append("the Jira MCP has no attachment tool: screenshots stay local and the comment lists them")
        except Exception as e:  # noqa: BLE001
            gaps.append(f"Jira MCP not reachable: {e}. Connect one to Claude Code and run `devflow setup`.")
        if not gaps:
            try:
                deps.jira.status_and_assignee(state["ticket_key"])
            except Exception as e:  # noqa: BLE001
                gaps.append(f"cannot read {state['ticket_key']} from Jira: {e}")
        confluence_ok = True
        try:
            missing = deps.confluence.missing_tools()
            if missing:
                confluence_ok = False
                warnings.append(f"Confluence read tools missing {missing}: linked pages will not be read")
        except Exception as e:  # noqa: BLE001
            confluence_ok = False
            warnings.append(f"Confluence not reachable ({str(e)[:120]}): linked pages will not be read")
        if gaps:
            raise PreflightFailed(gaps)
        missing = {s: m for s, m in deps.routing.missing().items() if s in STEPS}
        if missing:
            warnings.append("global Claude Code skills not installed (steps run without them): "
                            + "; ".join(f"{s}: {', '.join(m)}" for s, m in missing.items()))
        store.audit(state["run_id"], "preflight", {"repos": repos, "warnings": warnings, "jira": access})
        return {"scope": ScopeGuard.create(repos).to_state(), "env_warnings": warnings, "jira_access": access,
                "confluence_ok": confluence_ok, "results": {}, "human_inputs": {}}

    node("preflight", preflight)
    g.add_edge("preflight", "checkout_gate")

    # 1 checkout gate: the developer refreshes and checks out; devflow only verifies ------------------------------
    def checkout_payload(state):
        vcs, repos = deps.vcs(state), {}
        for r, shas in state["commits"].items():
            path = state["scope"][r]
            known = {s: vcs.commit_of(r, s) for s in shas}
            branches = sorted({b for s, full in known.items() if full for b in vcs.branches_containing(r, s)})
            pick = next((b for b in branches if state["ticket_key"] in b), branches[0] if branches else "")
            branch = pick.split("/", 1)[1] if pick.startswith("origin/") else pick
            repos[r] = {"path": path, "commits": shas, "current_branch": vcs.current_branch(r), "branches_with_commits": branches,
                        "not_in_clone_yet": [s for s, full in known.items() if not full],
                        "commands": [f"cd {path}", f"{prefix} git fetch origin".strip(),
                                     f"{prefix} git checkout {branch or '<the branch with these commits>'}".strip(),
                                     f"{prefix} git pull --ff-only".strip()],
                        "run": ws.repo(r).commands.get("run", "")}
        return {"repos": repos, "app_url": state.get("app_url", ""), "issues": state.get("checkout_issues", []),
                "warnings": state.get("env_warnings", []),
                "hint": "Run these in your own clone (devflow never checks out or pulls for you), start the app, then choose "
                        "ready with its URL in app_url. devflow then checks every commit is in your checked-out branch, "
                        "nothing tracked is uncommitted and the branch isn't behind its remote."}

    checkpoint("checkout_gate", checkout_payload, ["ready", "abort"])

    def verify_checkout(state):
        vcs, ans = deps.vcs(state), state["last_answer"]
        app_url = str(ans.get("app_url") or "").strip() or state.get("app_url", "")
        issues, checkout = [], {}
        for r, shas in state["commits"].items():
            branch, full = vcs.current_branch(r), {}
            for s in shas:
                full[s] = vcs.commit_of(r, s)
                if not full[s]:
                    issues.append({"repo": r, "problem": f"commit {s} is not in your clone: run `{prefix} git fetch origin`".replace("  ", " ")})
                elif not vcs.in_head(r, s):
                    issues.append({"repo": r, "problem": f"commit {s} is not in the checked-out branch {branch}: check out the branch that has it"})
            if vcs.git(r, "status", "--porcelain", "--untracked-files=no"):
                issues.append({"repo": r, "problem": "uncommitted changes to tracked files: commit or stash them so you test exactly the commits"})
            up = vcs.upstream(r)
            if up and not up.startswith("@"):
                _, behind = vcs.ahead_behind(r, "HEAD", up)
                if behind:
                    issues.append({"repo": r, "problem": f"{behind} commit(s) behind {up}: run `{prefix} git pull --ff-only`".replace("  ", " ")})
            checkout[r] = {"branch": branch, "head": vcs.head(r), "commits": full}
        if not app_url:
            issues.append({"repo": "-", "problem": "no app URL: start the app and give its URL (app_url)"})
        store.audit(state["run_id"], "checkout_verified", {"issues": issues, "checkout": checkout})
        return {"checkout_issues": issues, "checkout": checkout, "app_url": app_url}

    node("verify_checkout", verify_checkout)
    g.add_conditional_edges("checkout_gate_wait", lambda s: "abort" if choice(s) == "abort" else "verify_checkout", ["abort", "verify_checkout"])
    g.add_conditional_edges("verify_checkout", lambda s: "checkout_gate" if s["checkout_issues"] else "understand_ticket",
                            ["checkout_gate", "understand_ticket"])

    # 2 understand the ticket ---------------------------------------------------------------------------------------
    def understand_ticket(state):
        t = deps.jira.fetch_ticket(state["ticket_key"], str(run_dir(state) / "attachments"))
        pages = []
        if state.get("confluence_ok"):
            for pid in page_ids_from_urls(t.get("confluence_urls", [])):
                try:  # read only, and optional: a page you can't read is skipped
                    pages.append({"id": pid, "page": deps.confluence.get_page(pid)})
                except Exception as e:  # noqa: BLE001
                    pages.append({"id": pid, "error": str(e)[:200]})
        prompt = (f"<ticket key='{t['key']}'>\n<title>{t['title']}</title>\n<description>\n{t['description']}\n</description>\n</ticket>\n"
                  + (f"<confluence>\n{_tail(_j(compact_pages(pages)), 30000)}\n</confluence>\n" if pages else "")
                  + "\nYou are about to review and test the code that implements this ticket. State what it requires and the "
                    "acceptance criteria a tester must prove. Derive criteria from the description when it has none.")
        u = llm.structured(SYSTEM, prompt, TicketUnderstanding, images=t.get("images", []), step="ticket_review.understand")
        return {"ticket": t, "understanding": u.model_dump()}

    node("understand_ticket", understand_ticket, retry_policy=TRANSIENT)
    g.add_edge("understand_ticket", "code_review")

    def ticket_block(state) -> str:
        t, u = state["ticket"], state["understanding"]
        return (f"<ticket key='{t['key']}'><title>{t['title']}</title></ticket>\n<requirement>\n{_j(u)}\n</requirement>")

    def commit_diffs(state) -> dict[str, str]:
        vcs = deps.vcs(state)
        out = {}
        for r, shas in state["commits"].items():
            text = "\n\n".join(vcs.show_commit(r, s) for s in shas)
            out[r] = text if len(text) <= DIFF_LIMIT else text[:DIFF_LIMIT] + "\n…(diff truncated)"
        return out

    # 3 code review -------------------------------------------------------------------------------------------------
    def code_review(state):
        from ..workflows import pr_review
        reviewer = pr_review.build_graph(llm)  # its own pr_review.* steps, so workspace.yaml model overrides apply
        diffs, reviews = commit_diffs(state), {}
        for r, diff in diffs.items():
            out = reviewer.invoke({"title": f"{state['ticket_key']}: {state['ticket']['title']} ({r})",
                                   "description": ticket_block(state), "diff": diff, "findings": []})
            reviews[r] = {"decision": out["verdict"].decision, "summary": out["verdict"].summary, "risk": out["triage"].risk,
                          "findings": [f.model_dump() for f in out["findings"]]}
        cov = llm.structured(SYSTEM, ticket_block(state) + f"\n<commits_by_repo>\n{_j({r: diff_digest(d) for r, d in diffs.items()})}\n</commits_by_repo>\n\n"
                             "For every acceptance criterion, say whether these commits implement it (met, partial, missing or "
                             "unclear) with the evidence. Then list changes the ticket does not ask for.",
                             Coverage, step="ticket_review.coverage")
        return {"reviews": reviews, "coverage": cov.model_dump()}

    node("code_review", code_review)
    g.add_edge("code_review", "test_plan")

    # 4 test plan ---------------------------------------------------------------------------------------------------
    def test_plan(state):
        notes = state.get("plan_notes") or []
        findings = {r: [f for f in rv["findings"] if f["severity"] in ("blocker", "major")] for r, rv in state["reviews"].items()}
        prompt = (ticket_block(state) + f"\n<coverage>{_j(state['coverage'])}</coverage>\n<review_findings>{_j(findings)}</review_findings>\n"
                  f"<app_url>{state['app_url']}</app_url>\n"
                  + ("<developer_notes>\n" + "\n".join(notes) + "\n</developer_notes>\n" if notes else "")
                  + "\nWrite end-to-end browser test cases that prove each acceptance criterion against the running app, plus "
                    "cases for risky areas the review found. Keep each case small and independent. Mark needs_you only when "
                    "a person must really act.")
        tp = llm.structured(SYSTEM, prompt, TestPlanDraft, step="ticket_review.test_plan")
        cases = [{"id": f"TC{i}", **c.model_dump()} for i, c in enumerate(tp.cases, start=1)]
        return {"test_plan": {"cases": cases, "not_testable": tp.not_testable}}

    node("test_plan", test_plan)
    g.add_edge("test_plan", "approve_test_plan")

    checkpoint("approve_test_plan", lambda s: {
        "cases": s["test_plan"]["cases"], "not_testable": s["test_plan"]["not_testable"], "app_url": s["app_url"],
        "coverage": s["coverage"], "reviews": {r: {k: v[k] for k in ("decision", "risk", "summary")} for r, v in s["reviews"].items()},
        "hint": "Tick the cases to run (run = case ids, default all), edit them (cases = the full edited list) and add a note "
                "the tester should know (test users, data). regenerate = rewrite the plan with your note."},
        ["approve", "regenerate", "abort"])

    def apply_test_plan(state):
        ans = state["last_answer"]
        if ans["choice"] == "regenerate":
            return {"plan_notes": [ans.get("note") or "Rewrite the plan."]}
        cases = state["test_plan"]["cases"]
        if isinstance(ans.get("cases"), list) and ans["cases"]:
            cases = [{"id": c.get("id") or f"TC{i}", "title": str(c.get("title", "")), "criterion": str(c.get("criterion", "")),
                      "preconditions": list(c.get("preconditions") or []), "steps": list(c.get("steps") or []),
                      "expected": str(c.get("expected", "")), "mode": "needs_you" if c.get("mode") == "needs_you" else "auto",
                      "needs_you_reason": str(c.get("needs_you_reason", "")), "proof": list(c.get("proof") or [])}
                     for i, c in enumerate(ans["cases"], start=1) if isinstance(c, dict)]
        ids = [c["id"] for c in cases]
        run = [i for i in (ans.get("run") or ids) if i in ids]
        update = {"test_plan": {**state["test_plan"], "cases": cases}, "run_cases": run, "results": {}, "human_ask": None}
        if ans.get("note"):
            update["tester_notes"] = [ans["note"]]
        store.audit(state["run_id"], "test_plan_approved", {"run": run})
        return update

    node("apply_test_plan", apply_test_plan)
    g.add_conditional_edges("approve_test_plan_wait", lambda s: "abort" if choice(s) == "abort" else "apply_test_plan",
                            ["abort", "apply_test_plan"])

    def next_case(state) -> str | None:
        done = state.get("results") or {}
        return next((c for c in state.get("run_cases") or [] if c not in done), None)

    def after_plan(state) -> str:
        if choice(state) == "regenerate":
            return "test_plan"
        return "run_case" if next_case(state) else "draft_comment"

    g.add_conditional_edges("apply_test_plan", after_plan, ["test_plan", "run_case", "draft_comment"])

    # 5 E2E, one case at a time ---------------------------------------------------------------------------------------
    def run_case(state):
        cid = next_case(state)
        case = next(c for c in state["test_plan"]["cases"] if c["id"] == cid)
        evidence = run_dir(state) / "evidence"
        evidence.mkdir(parents=True, exist_ok=True)
        profile = Path(ws.state_dir, "playwright-profile")
        agent = deps.e2e_agent(state, str(evidence), str(profile))
        given = (state.get("human_inputs") or {}).get(cid, [])
        notes = state.get("tester_notes") or []
        prompt = (f"Test case {cid} for {state['ticket_key']} against the running app at {state['app_url']}.\n<case>\n{_j(case)}\n</case>\n"
                  + ("<developer_notes>\n" + "\n".join(notes) + "\n</developer_notes>\n" if notes else "")
                  + ("<developer_input>\n" + "\n".join(given) + "\n</developer_input>\n" if given else "")
                  + "\nUse only the Playwright MCP browser tools. Follow the steps and check the expected result. Take a screenshot "
                    f"for each proof point with browser_take_screenshot, named {cid}-1.png, {cid}-2.png and so on. "
                    "If a step needs a person (an OTP, a captcha, a login only they have, data you cannot create) and the "
                    "developer input does not cover it, stop and return status needs_human saying exactly what they must do "
                    "or give you. Never change the app's code. status passed only if every expected result was seen.")
        res = agent.test_case(dict(state["scope"]), prompt, step="ticket_review.e2e")
        shots = sorted(str(p) for p in evidence.glob(f"{cid}-*") if p.suffix.lower() in IMAGE_EXT)
        if res.get("status") == "needs_human":
            return {"human_ask": {"case": cid, "title": case["title"], "ask": res.get("needs_human") or res.get("summary", ""),
                                  "summary": res.get("summary", ""), "screenshots": shots, "inputs_given": len(given)}}
        result = {"status": "passed" if res.get("status") == "passed" else "failed", "summary": res.get("summary", ""),
                  "steps": res.get("steps", []), "screenshots": shots}
        store.audit(state["run_id"], "test_case", {"case": cid, "status": result["status"], "screenshots": len(shots)})
        return {"results": {**(state.get("results") or {}), cid: result}, "human_ask": None}

    node("run_case", run_case)
    g.add_conditional_edges("run_case", lambda s: "human_step" if s.get("human_ask") else "run_case" if next_case(s) else "review_results",
                            ["human_step", "run_case", "review_results"])

    checkpoint("human_step", lambda s: {
        **s["human_ask"], "app_url": s["app_url"],
        "hint": "Do what the test asks. Give a value it needs (an OTP, a test account) in the note, or log in yourself in the "
                "test browser: its profile is kept between cases. continue = re-run this case with your note; skip = leave it "
                "out; fail = record it as failed (say why in the note)."},
        ["continue", "skip", "fail", "abort"])

    def apply_human_step(state):
        ans, ask = state["last_answer"], state["human_ask"]
        cid = ask["case"]
        if ans["choice"] == "continue":
            inputs_ = dict(state.get("human_inputs") or {})
            inputs_[cid] = inputs_.get(cid, []) + [ans.get("note") or "Done: I did that step in the test browser."]
            return {"human_inputs": inputs_, "human_ask": None}
        status = "skipped" if ans["choice"] == "skip" else "failed"
        summary = ans.get("note") or ("skipped by you" if status == "skipped" else "marked failed by you")
        return {"results": {**(state.get("results") or {}), cid: {"status": status, "summary": summary, "steps": [],
                                                                    "screenshots": ask.get("screenshots", [])}}, "human_ask": None}

    node("apply_human_step", apply_human_step)
    g.add_conditional_edges("human_step_wait", lambda s: "abort" if choice(s) == "abort" else "apply_human_step", ["abort", "apply_human_step"])
    g.add_conditional_edges("apply_human_step", lambda s: "run_case" if next_case(s) else "review_results", ["run_case", "review_results"])

    def results_list(state) -> list[dict]:
        by_id = {c["id"]: c for c in state["test_plan"]["cases"]}
        return [{"id": cid, "title": by_id[cid]["title"], "criterion": by_id[cid]["criterion"], **state["results"][cid]}
                for cid in state.get("run_cases") or [] if cid in (state.get("results") or {})]

    checkpoint("review_results", lambda s: {
        "results": results_list(s),
        "counts": {k: sum(1 for r in s["results"].values() if r["status"] == k) for k in ("passed", "failed", "skipped")},
        "hint": "accept = write the Jira comment with these results (failures are reported, never hidden); retest = run the "
                "cases in retest again (default: the failed and skipped ones), e.g. after you fixed something."},
        ["accept", "retest", "abort"])

    def apply_results(state):
        ans = state["last_answer"]
        if ans["choice"] != "retest":
            return {}
        results = dict(state["results"])
        again = [c for c in (ans.get("retest") or [c for c, r in results.items() if r["status"] != "passed"]) if c in results]
        for c in again:
            results.pop(c)
        update = {"results": results}
        if ans.get("note"):
            update["tester_notes"] = [ans["note"]]
        return update

    node("apply_results", apply_results)
    g.add_conditional_edges("review_results_wait", lambda s: "abort" if choice(s) == "abort" else "apply_results", ["abort", "apply_results"])
    g.add_conditional_edges("apply_results", lambda s: "run_case" if next_case(s) else "draft_comment", ["run_case", "draft_comment"])

    # 6 the Jira comment ----------------------------------------------------------------------------------------------
    def draft_comment(state):
        results = results_list(state)
        notes = state.get("comment_notes") or []
        cd = llm.structured(SYSTEM, ticket_block(state) + f"\n<coverage>{_j(state['coverage'])}</coverage>\n"
                            f"<code_review>{_j(state['reviews'])}</code_review>\n<test_results>{_j(results)}</test_results>\n"
                            + ("<developer_notes>\n" + "\n".join(notes) + "\n</developer_notes>\n" if notes else "")
                            + "\nWrite the conclusion and summary of this ticket review for the Jira comment. Be factual: "
                              "name failed tests and missing criteria.", CommentDraft, step="ticket_review.comment")
        uploads_dir = run_dir(state) / "upload"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        uploads = []
        for r in results:  # unique names on the ticket, so a later review never collides with this one
            for p in r.get("screenshots", []):
                dst = uploads_dir / f"{state['run_id']}-{Path(p).name}"
                if Path(p).exists():
                    shutil.copyfile(p, dst)
                    uploads.append(str(dst))
        return {"comment": render_comment(state, cd, results, uploads), "conclusion": cd.conclusion, "uploads": uploads}

    node("draft_comment", draft_comment)
    g.add_edge("draft_comment", "approve_comment")

    checkpoint("approve_comment", lambda s: {
        "comment": s["comment"], "attachments": s.get("uploads", []), "jira": s["jira_access"],
        "will_post": bool(s["jira_access"].get("can_post")),
        "hint": ("approve = post this comment" + (" and attach the screenshots" if s["jira_access"].get("can_attach") else "")
                 + f" on {s['ticket_key']}" if s["jira_access"].get("can_post") else
                 f"Jira posting is off ({s['jira_access'].get('reason')}): approve saves the comment and screenshots for you to post by hand")
                + ". edit = send your edited text in comment; regenerate = rewrite it with your note."},
        ["approve", "edit", "regenerate", "abort"])

    def apply_comment(state):
        ans = state["last_answer"]
        if ans["choice"] == "edit" and str(ans.get("comment") or "").strip():
            return {"comment": str(ans["comment"]).strip()}
        if ans["choice"] == "regenerate":
            return {"comment_notes": [ans.get("note") or "Rewrite the comment."]}
        return {}

    def after_comment(state) -> str:
        return {"approve": "post_to_jira", "edit": "approve_comment", "regenerate": "draft_comment"}[choice(state)]

    node("apply_comment", apply_comment)
    g.add_conditional_edges("approve_comment_wait", lambda s: "abort" if choice(s) == "abort" else "apply_comment", ["abort", "apply_comment"])
    g.add_conditional_edges("apply_comment", after_comment, ["post_to_jira", "approve_comment", "draft_comment"])

    # 7 post (or save for posting by hand) -----------------------------------------------------------------------------
    def post_to_jira(state):
        key, run_id, access = state["ticket_key"], state["run_id"], state["jira_access"]
        uploads, body = state.get("uploads") or [], state["comment"]
        local = run_dir(state) / "jira-comment.md"
        local.write_text(body + "\n")
        result = {"posted": False, "comment": "", "attached": [], "local": str(local), "evidence": str(run_dir(state) / "upload"),
                  "reason": ""}
        if not access.get("can_post"):
            result["reason"] = access.get("reason") or "Jira posting is off"
        else:
            attach_note = ""
            if uploads and access.get("can_attach"):
                try:
                    result["attached"] = perform(store, Effect(run_id, "jira_attach", key, "proof_screenshots"), detect=lambda: None,
                                                 act=lambda: deps.jira.attach_files(key, uploads))["result"]
                except JiraWriteRefused as e:
                    attach_note = f"Jira refused the upload ({str(e)[:200]})"
            elif uploads:
                attach_note = "the Jira MCP has no attachment tool"
            if attach_note:
                result["attach_problem"] = attach_note
                body += ("\n\nProof screenshots could not be attached (" + attach_note + "); the developer keeps them: "
                         + ", ".join(Path(p).name for p in uploads))
            try:
                result["comment"] = perform(store, Effect(run_id, "jira_comment", key, "ticket_review"), detect=lambda: None,
                                            act=lambda: deps.jira.upsert_comment(key, marker(run_id, "ticket-review"), body))["result"]
                result["posted"] = True
            except JiraWriteRefused as e:
                result["reason"] = f"Jira refused the comment: {str(e)[:300]}"
        store.audit(run_id, "jira_post", result)
        return {"jira_result": result}

    node("post_to_jira", post_to_jira, retry_policy=TRANSIENT)
    g.add_edge("post_to_jira", "summary")

    def summary(state):
        jr, results = state.get("jira_result") or {}, results_list(state)
        counts = {k: sum(1 for r in results if r["status"] == k) for k in ("passed", "failed", "skipped")}
        lines = [f"# {state['ticket_key']}: ticket review", "",
                 f"Conclusion: **{state.get('conclusion', '?')}** · tests {counts['passed']} passed, {counts['failed']} failed, "
                 f"{counts['skipped']} skipped", ""]
        for r, rv in (state.get("reviews") or {}).items():
            lines.append(f"- **{r}**: code review {rv['decision']} (risk {rv['risk']}), {len(rv['findings'])} finding(s)")
        if jr.get("posted"):
            lines.append(f"\nPosted to Jira ({jr['comment']}); {len(jr.get('attached', []))} screenshot(s) attached."
                         + (f" {jr['attach_problem']}." if jr.get("attach_problem") else ""))
        else:
            lines.append(f"\nNot posted to Jira: {jr.get('reason')}. The comment is in {jr.get('local')} and the screenshots "
                         f"in {jr.get('evidence')}: post them by hand.")
        lines.append("Nothing was committed, pushed or changed in your clones.")
        store.set_status(state["run_id"], "COMPLETED", node="summary", detail="" if jr.get("posted") else f"not posted to Jira: {jr.get('reason')}")
        return {"output": "\n".join(lines) + "\n"}

    node("summary", summary)
    g.add_edge("summary", END)
    kit.add_abort()
    g.add_edge(START, "preflight")
    return g.compile(checkpointer=checkpointer)


STATUS_MARK = {"met": "✅", "partial": "⚠️", "missing": "❌", "unclear": "❔", "passed": "✅", "failed": "❌", "skipped": "⏭️"}
CONCLUSION = {"ready": "✅ Ready: the commits meet the ticket and the tests pass",
              "needs_changes": "❌ Needs changes", "blocked": "⚠️ Review blocked"}


def render_comment(state, cd: CommentDraft, results: list[dict], uploads: list[str]) -> str:
    """The Jira comment, in Markdown (the Jira MCP converts it). Facts come from state, only the summary from the model."""
    t = state["ticket"]
    lines = [f"## Ticket review: {t['key']} {t['title']}", "", f"**{CONCLUSION.get(cd.conclusion, cd.conclusion)}**", "", cd.summary, "",
             "**Commits reviewed**"]
    for r, shas in state["commits"].items():
        branch = (state.get("checkout") or {}).get(r, {}).get("branch", "")
        lines.append(f"- {r}: {', '.join(f'`{s[:10]}`' for s in shas)}" + (f" on `{branch}`" if branch else ""))
    lines += ["", "**Acceptance criteria**"]
    lines += [f"- {STATUS_MARK.get(c['status'], '')} {c['criterion']}: {c['evidence']}" for c in state["coverage"]["criteria"]] or ["- none found"]
    if state["coverage"].get("unrelated_changes"):
        lines += ["", "**Changes the ticket does not ask for**"] + [f"- {u}" for u in state["coverage"]["unrelated_changes"]]
    lines += ["", "**Code review**"]
    for r, rv in state["reviews"].items():
        lines.append(f"- {r}: {rv['decision'].replace('_', ' ')} (risk {rv['risk']}). {rv['summary']}")
        lines += [f"  - [{f['severity']}] {f['file']}{':' + str(f['line']) if f.get('line') else ''} {f['title']}"
                  for f in rv["findings"] if f["severity"] in ("blocker", "major")]
    lines += ["", "**E2E tests (Playwright)**"]
    if results:
        lines += ["| Case | Criterion | Result | Proof |", "|---|---|---|---|"]
        for r in results:
            proof = ", ".join(f"{state['run_id']}-{Path(p).name}" for p in r.get("screenshots", [])) or "-"
            lines.append(f"| {r['id']} {r['title']} | {r['criterion']} | {STATUS_MARK.get(r['status'], '')} {r['status']} | {proof} |")
        failed = [r for r in results if r["status"] == "failed"]
        lines += [f"- {r['id']} failed: {r['summary']}" for r in failed]
    else:
        lines.append("- no E2E tests were run")
    if uploads:
        lines += ["", f"Proof screenshots are attached to this ticket ({len(uploads)} file(s))."]
    lines += ["", "_Reviewed with devflow; the developer approved this comment before it was posted._"]
    return "\n".join(lines)
