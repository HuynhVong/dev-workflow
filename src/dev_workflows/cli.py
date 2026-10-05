"""Run a workflow from the terminal.

  devflow plan   --ticket ticket.md [--context stack.md] [--no-interactive]
  devflow review --title "Add search" --diff <(git diff main...HEAD) [--description pr.md]
  devflow standup --repo ~/code/api --repo ~/code/web [--since "yesterday"] [--author me@x.com] [--notes notes.md]
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

    args = p.parse_args(argv)
    print(args.fn(args))


if __name__ == "__main__":
    main()
