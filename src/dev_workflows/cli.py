"""Run a workflow from the terminal.

  devflow plan   --ticket ticket.md [--context stack.md] [--no-interactive]
  devflow review --title "Add search" --diff <(git diff main...HEAD) [--description pr.md]
  devflow standup --repo ~/code/api --repo ~/code/web [--since "yesterday"] [--author me@x.com] [--notes notes.md]

Jira ticket implement (needs workspace.yaml, see workspace.example.yaml):
  devflow implement AQS-5512 [--repos api-service,web-portal] [--no-input]
  devflow address-review AQS-5512 [--repos api-service,web-portal] [--no-input]
  devflow answer <run_id> --choice approve [--note "..."] [--repos a,b] [--approve-repos x] [--fix 1,3 --answer-only 2 --skip 4]
  devflow resume <run_id> [--reopen]      devflow abort <run_id> [--note "..."]
  devflow status <run_id>                 devflow show <run_id>          devflow runs
"""
import argparse
import sys
import uuid
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from .workflows import pr_review, standup, ticket_to_plan


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


def run_review(args) -> str:
    return pr_review.graph.invoke(
        {"title": args.title, "description": _read(args.description), "diff": _read(args.diff), "findings": []}
    )["output"]


def run_standup(args) -> str:
    return standup.graph.invoke(
        {"repo_paths": args.repo or [], "since": args.since, "author": args.author, "notes": _read(args.notes)}
    )["output"]


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="devflow")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("plan", help="ticket -> implementation plan")
    sp.add_argument("--ticket", required=True, help="file path, or - for stdin")
    sp.add_argument("--context", help="notes about the repo/stack")
    sp.add_argument("--no-interactive", action="store_true", help="don't stop for clarifying questions")
    sp.set_defaults(fn=run_plan)

    sr = sub.add_parser("review", help="review a diff")
    sr.add_argument("--title", required=True)
    sr.add_argument("--diff", required=True, help="file path, or - for stdin")
    sr.add_argument("--description")
    sr.set_defaults(fn=run_review)

    ss = sub.add_parser("standup", help="standup notes from git + notes")
    ss.add_argument("--repo", action="append")
    ss.add_argument("--since", default="yesterday")
    ss.add_argument("--author")
    ss.add_argument("--notes")
    ss.set_defaults(fn=run_standup)

    _add_implement_commands(sub)
    args = p.parse_args(argv)
    result = args.fn(args)
    if result is not None:
        print(result)


def _session(args):
    from .jira_implement.runner import Session, interactive_ask, load
    return Session(load(args.workspace), ask=None if getattr(args, "no_input", False) else interactive_ask)


def _csv(value):
    return [x.strip() for x in (value or "").split(",") if x.strip()]


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
    sa.add_argument("--choice", required=True)
    sa.add_argument("--note", default="")
    sa.add_argument("--repos", help="repos for a fix or for manual-test feedback")
    sa.add_argument("--approve-repos", help="out-of-scope repos you approve at clarify")
    sa.add_argument("--fix", help="address-review triage (choice edit): thread numbers to fix")
    sa.add_argument("--answer-only", help="address-review triage (choice edit): thread numbers to answer without code")
    sa.add_argument("--skip", help="address-review triage (choice edit): thread numbers to skip")

    def do_answer(a):
        ans = {"choice": a.choice, "note": a.note}
        if a.repos:
            ans["repos"] = _csv(a.repos)
        if a.approve_repos:
            ans["approve_repos"] = _csv(a.approve_repos)
        for key, value in (("fix", a.fix), ("answer", a.answer_only), ("skip", a.skip)):
            if value:
                ans[key] = _csv(value)
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

    sl = sub.add_parser("runs", help="recent runs")
    common(sl, run=False)
    sl.set_defaults(fn=lambda a: "\n".join(f"{r['run_id']}  {r['status']:<14} {r['node']}" for r in _session(a).store.runs()))


if __name__ == "__main__":
    main()
