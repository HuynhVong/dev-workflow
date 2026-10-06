"""Run a workflow from the terminal.

  devflow plan   --ticket ticket.md [--context stack.md] [--no-interactive]

Jira workflows (need workspace.yaml, see workspace.example.yaml):
  devflow standup [--from 2026-10-01] [--to 2026-10-03] [--template report.md] [--no-input]
  devflow setup [--ticket AQS-5512] [--jira-mcp NAME] [--confluence-mcp NAME] [--playwright-mcp NAME] [--mysql-mcp NAME]
                                          first run: which Claude Code MCP servers to use, Jira MCP (required),
                                          models per step, global skills to install
  devflow implement AQS-5512 [--repos api-service,web-portal] [--no-input]
  devflow address-review AQS-5512 [--repos api-service,web-portal] [--no-input]
  devflow review AQS-5512 --commit web-portal=3f9a1c2 [--commit api-service=88be0d4,a17c3e9] [--app-url URL] [--no-input]
  devflow answer <run_id> --choice approve [--note "..."] [--repos a,b] [--approve-repos x] [--fix 1,3 --answer-only 2 --skip 4]
                 [--app-url URL] [--run TC1,TC2] [--retest TC2] [--comment-file comment.md]
                 [--keep AQS-1,AQS-2] [--report-file report.md]
  devflow resume <run_id> [--reopen]      devflow abort <run_id> [--note "..."]
  devflow status <run_id>                 devflow show <run_id>          devflow runs
  devflow worktrees                       devflow worktree-clean <ticket> [--repo api-service]
  devflow doctor [--json]                 devflow ui [--port 8765] [--no-browser]
"""
import argparse
import sys
import uuid
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from .workflows import ticket_to_plan


def _read(path: str | None) -> str:
    if not path:
        return ""
    return sys.stdin.read() if path == "-" else Path(path).read_text()


def run_plan(args) -> str:
    g = ticket_to_plan.build_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    result = g.invoke(
        {"ticket": _read(args.ticket), "repo_context": _read(args.context), "interactive": not args.no_interactive},
        config,
    )
    while "__interrupt__" in result:
        questions = result["__interrupt__"][0].value["questions"]
        print("The ticket has open questions. Answer each (blank = make an assumption):\n", file=sys.stderr)
        answers = []
        for q in questions:
            a = input(f"? {q}\n> ").strip()
            answers.append(f"Q: {q}\nA: {a or '(no answer: make a reasonable assumption and list it)'}")
        result = g.invoke(Command(resume="\n\n".join(answers)), config)
    return result["output"]


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="devflow")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("plan", help="ticket -> implementation plan")
    sp.add_argument("--ticket", required=True, help="file path, or - for stdin")
    sp.add_argument("--context", help="notes about the repo/stack")
    sp.add_argument("--no-interactive", action="store_true", help="don't stop for clarifying questions")
    sp.set_defaults(fn=run_plan)

    _add_implement_commands(sub)
    args = p.parse_args(argv)
    from .jira_implement.runner import RunConflict
    try:
        result = args.fn(args)
    except RunConflict as e:
        raise SystemExit(str(e)) from None
    if result is not None:
        print(result)


def _session(args):
    from .jira_implement.runner import Session, interactive_ask, load
    return Session(load(args.workspace), ask=None if getattr(args, "no_input", False) else interactive_ask)


def _csv(value):
    return [x.strip() for x in (value or "").split(",") if x.strip()]


def _choose_claude_code_mcp(a, ws) -> bool:
    """Setup step: which MCP servers of the logged-in Claude Code devflow uses for Jira, Confluence and Playwright.
    Saved under claude_code_mcp in workspace.yaml; a blank answer keeps the default. Returns True if it changed."""
    import os

    from .jira_implement.claude_code_mcp import USES, claude_code_servers, suggest
    from .jira_implement.workspace import set_claude_code_mcp
    given = {u: getattr(a, f"{u}_mcp") for u in USES}
    ask = not a.no_input and sys.stdin.isatty() and any(v is None for v in given.values())
    if not ask and all(v is None for v in given.values()):
        return False
    try:
        servers = claude_code_servers()
    except Exception as e:  # noqa: BLE001
        print(f"Claude Code MCP servers: could not list them ({e}); keeping workspace.yaml as is.\n")
        return False
    print("MCP servers in your Claude Code login:")
    for name, info in servers.items() or {"(none)": {"status": "", "tools": []}}.items():
        print(f"  - {name}: {info['status']}" + (f", {len(info['tools'])} tools" if info["tools"] else ""))
    hint = suggest(servers)
    choice = {}
    for use in USES:
        current = ws.claude_code_mcp.get(use, "")
        if given[use] is not None:
            choice[use] = given[use].strip()
            continue
        default = current or hint[use]
        if not ask:
            choice[use] = current
            continue
        label = "MySQL (dev DB, optional, read-only)" if use == "mysql" else use.title()
        answer = input(f"{label} via which Claude Code MCP? [{default or 'none: use the default'}] "
                       "(Enter = keep, '-' = none): ").strip()
        choice[use] = "" if answer == "-" else (answer or default)
        if choice[use] and choice[use] not in servers:
            print(f"    note: Claude Code has no server named '{choice[use]}' right now")
    path = a.workspace or os.getenv("DEVFLOW_WORKSPACE", "workspace.yaml")
    if choice == {u: ws.claude_code_mcp.get(u, "") for u in USES}:
        print()
        return False
    set_claude_code_mcp(path, choice)
    print(f"Saved claude_code_mcp in {path}: " + ", ".join(f"{u}={v or 'default'}" for u, v in choice.items()) + "\n")
    return True


def _add_implement_commands(sub) -> None:
    import json

    def common(sp, run=True):
        sp.add_argument("--workspace", help="path to workspace.yaml (default: $DEVFLOW_WORKSPACE or ./workspace.yaml)")
        sp.add_argument("--no-input", action="store_true", help="stop at checkpoints instead of prompting")
        if run:
            sp.add_argument("run_id")

    si = sub.add_parser("implement", help="Jira ticket -> local multi-repo implementation -> draft MRs")
    si.add_argument("ticket")
    si.add_argument("--repos", help="hard allow-list of repos (comma separated)")
    common(si, run=False)
    si.set_defaults(fn=lambda a: _session(a).start(a.ticket, _csv(a.repos)) and None)

    st = sub.add_parser("review", help="Jira ticket + its commits -> code review, approved E2E tests with proof, Jira comment")
    st.add_argument("ticket")
    st.add_argument("--commit", action="append", required=True, metavar="REPO=SHA[,SHA]",
                    help="the ticket's commits in one repo (repeat per changed repo)")
    st.add_argument("--app-url", help="where the running app is (default: app_url of the repo in workspace.yaml)")
    common(st, run=False)

    def do_ticket_review(a):
        from .jira_implement.ticket_review import WORKFLOW as TICKET_REVIEW
        _session(a).start_run(TICKET_REVIEW, {"ticket": a.ticket, "commits": a.commit, "app_url": a.app_url or ""})
    st.set_defaults(fn=do_ticket_review)

    sp = sub.add_parser("standup", help="work report: Jira tickets you started and reviewed in a date range, in your template")
    sp.add_argument("--from", dest="from_date", metavar="YYYY-MM-DD", help="inclusive (default: previous working day)")
    sp.add_argument("--to", dest="to_date", metavar="YYYY-MM-DD", help="inclusive (default: today)")
    sp.add_argument("--template", help="Markdown report template file (default: the last one you used, else the built-in one)")
    common(sp, run=False)

    def do_standup(a):
        from .jira_implement.standup import WORKFLOW as STANDUP
        _session(a).start_run(STANDUP, {"from_date": a.from_date or "", "to_date": a.to_date or "",
                                        "template": _read(a.template)})
    sp.set_defaults(fn=do_standup)

    su = sub.add_parser("setup", help="first-time setup: Jira MCP (required), models per step, global Claude Code skills")
    su.add_argument("--workspace", help="path to workspace.yaml (default: $DEVFLOW_WORKSPACE or ./workspace.yaml)")
    su.add_argument("--ticket", help="a ticket key you can see, to prove the Jira MCP can read it")
    for use, label in (("jira", "Jira"), ("confluence", "Confluence"), ("playwright", "Playwright"),
                       ("mysql", "the dev MySQL database (optional, read-only)")):
        su.add_argument(f"--{use}-mcp", metavar="NAME", default=None,
                        help=f"Claude Code MCP server for {label} (as `claude mcp list` names it); '' = default")
    su.add_argument("--no-input", action="store_true", help="do not ask which Claude Code MCP servers to use")

    def do_setup(a):
        from . import doctor
        from .jira_implement.runner import load
        from .routing import Routing, setup_report
        ws = load(a.workspace)
        if _choose_claude_code_mcp(a, ws):
            ws = load(a.workspace)
        jira, source = doctor.jira_gateway(ws)
        checks = doctor.jira_access_checks(ws, jira, source, ticket=a.ticket or "")
        print("Jira MCP (required)\n" + "\n".join(
            f"  {dict(ok='✓', warn='!', fail='✗')[c.status]} {c.label}: {c.detail}" + (f"\n      fix: " + c.fix.replace("\n", "\n      ") if c.fix and c.status != "ok" else "")
            for c in checks) + "\n")
        report, ok = setup_report(Routing.from_config(ws.ai), {n: r.path for n, r in ws.repos.items()})
        print(report)
        if not ok or doctor.summary(checks)["blocking"]:
            raise SystemExit(1)
    su.set_defaults(fn=do_setup)

    sv = sub.add_parser("address-review", help="fix / answer reviewer comments on the ticket's draft MRs")
    sv.add_argument("ticket")
    sv.add_argument("--repos", help="hard allow-list of repos (comma separated)")
    common(sv, run=False)

    def do_review(a):
        from .jira_implement.address_review import WORKFLOW as REVIEW
        _session(a).start(a.ticket, _csv(a.repos), workflow=REVIEW)
    sv.set_defaults(fn=do_review)

    sa = sub.add_parser("answer", help="answer the checkpoint a run is waiting at")
    common(sa)
    sa.add_argument("--choice")
    sa.add_argument("--note", default="")
    sa.add_argument("--repos", help="repos for a fix or for manual-test feedback")
    sa.add_argument("--approve-repos", help="out-of-scope repos you approve at clarify")
    sa.add_argument("--fix", help="address-review triage (choice edit): thread numbers to fix")
    sa.add_argument("--answer-only", help="address-review triage (choice edit): thread numbers to answer without code")
    sa.add_argument("--skip", help="address-review triage (choice edit): thread numbers to skip")
    sa.add_argument("--value", help="answer for a plain interrupt() of another graph (free text)")
    sa.add_argument("--app-url", help="ticket review checkout (choice ready): where the running app is")
    sa.add_argument("--run", help="ticket review test plan (choice approve): case ids to run, default all")
    sa.add_argument("--retest", help="ticket review results (choice retest): case ids to run again")
    sa.add_argument("--comment-file", help="ticket review comment (choice edit): file with your edited comment")
    sa.add_argument("--keep", help="standup tickets (choice continue): ticket keys to keep, default all")
    sa.add_argument("--report-file", help="standup report (choice edit): file with your edited report")

    def do_answer(a):
        if a.value is not None:
            return _session(a).answer(a.run_id, {"value": a.value}) and None
        ans = {"choice": a.choice, "note": a.note}
        if a.repos:
            ans["repos"] = _csv(a.repos)
        if a.approve_repos:
            ans["approve_repos"] = _csv(a.approve_repos)
        for key, value in (("fix", a.fix), ("answer", a.answer_only), ("skip", a.skip), ("run", a.run), ("retest", a.retest)):
            if value:
                ans[key] = _csv(value)
        if a.app_url:
            ans["app_url"] = a.app_url
        if a.keep:
            ans["keep"] = _csv(a.keep)
        if a.report_file:
            ans["report"] = Path(a.report_file).expanduser().read_text()
        if a.comment_file:
            ans["comment"] = Path(a.comment_file).expanduser().read_text()
        _session(a).answer(a.run_id, ans)
    sa.set_defaults(fn=do_answer)

    sr = sub.add_parser("resume", help="continue a FAILED/interrupted run (or --reopen an ABORTED one)")
    common(sr)
    sr.add_argument("--reopen", action="store_true")
    sr.set_defaults(fn=lambda a: _session(a).resume(a.run_id, reopen=a.reopen) and None)

    sb = sub.add_parser("abort", help="abort a run that is waiting, failed or stale")
    common(sb)
    sb.add_argument("--note", default="")
    sb.set_defaults(fn=lambda a: _session(a).abort(a.run_id, a.note) and None)

    ss = sub.add_parser("status", help="lifecycle state, next step and repo status of a run")
    common(ss)
    ss.set_defaults(fn=lambda a: json.dumps(_session(a).status(a.run_id), indent=2, default=str))

    sw = sub.add_parser("show", help="audit log and side-effect ledger of a run")
    common(sw)
    sw.set_defaults(fn=lambda a: json.dumps(_session(a).show(a.run_id), indent=2, default=str))

    sk = sub.add_parser("worktrees", help="every devflow worktree: ticket, repo, branch, uncommitted changes, size")
    common(sk, run=False)

    def do_worktrees(a):
        from .jira_implement import worktrees
        from .jira_implement.runner import load
        rows = worktrees.list_all(load(a.workspace))
        return "\n".join(f"{r['ticket']:<12} {r['repo']:<20} {r['branch']:<14} {'dirty' if r['dirty'] else 'clean':<6} "
                         f"{r['size_bytes'] // 1_000_000:>6} MB  {r['path']}" for r in rows) or "No worktrees."
    sk.set_defaults(fn=do_worktrees)

    sc = sub.add_parser("worktree-clean", help="remove a finished ticket's clean worktrees (never forced, branches are kept)")
    sc.add_argument("ticket")
    sc.add_argument("--repo", help="only this repo")
    common(sc, run=False)

    def do_clean(a):
        from .jira_implement import worktrees
        from .jira_implement.runner import Session, load
        ws = load(a.workspace)
        busy = Session(ws).store.active_runs(a.ticket)
        if busy:
            raise SystemExit(f"{a.ticket} has an unfinished run {busy[0]['run_id']} ({busy[0]['status']}); finish or abort it first")
        rows = [r for r in worktrees.list_all(ws) if r["ticket"] == a.ticket and (not a.repo or r["repo"] == a.repo)]
        return "\n".join(f"removed {worktrees.remove(ws, a.ticket, r['repo'])}" for r in rows) or "Nothing to remove."
    sc.set_defaults(fn=do_clean)

    sd = sub.add_parser("doctor", help="check every connection: AI key, CLIs, MCP servers, repos, skills")
    sd.add_argument("--workspace", help="path to workspace.yaml (default: $DEVFLOW_WORKSPACE or ./workspace.yaml)")
    sd.add_argument("--json", action="store_true")

    def do_doctor(a):
        from . import doctor
        from .jira_implement.runner import load
        checks = doctor.run(load(a.workspace))
        if a.json:
            return json.dumps({"checks": [c.to_dict() for c in checks], "summary": doctor.summary(checks)}, indent=2)
        print(doctor.render(checks))
        if doctor.summary(checks)["blocking"]:
            raise SystemExit(1)
    sd.set_defaults(fn=do_doctor)

    sg = sub.add_parser("ui", help="open the devflow web app (local only)")
    sg.add_argument("--workspace", help="path to workspace.yaml (default: $DEVFLOW_WORKSPACE or ./workspace.yaml)")
    sg.add_argument("--port", type=int, default=0, help="default: ui.port in workspace.yaml, else 8765")
    sg.add_argument("--no-browser", action="store_true")

    def do_ui(a):
        from .ui.launch import serve
        serve(a.workspace, port=a.port, open_browser=not a.no_browser)
    sg.set_defaults(fn=do_ui)

    sl = sub.add_parser("runs", help="recent runs")
    common(sl, run=False)
    sl.set_defaults(fn=lambda a: "\n".join(f"{r['run_id']}  {r['status']:<14} {r['node']}" for r in _session(a).store.runs()))


if __name__ == "__main__":
    main()
