"""Hand the coding to the developer: the approved plan as one Markdown file to give Claude Code CLI (or any editor).

With `manual_code` a run plans, branches and reviews as usual, but a repo is coded by the developer in its worktree
instead of by the coding agent. This file is the agent's prompt, written for a person: what to build in which repo, the
contracts, the checks that must pass, and, on a later round, what failed or what the review asked to change."""
from pathlib import Path

from . import mockups


def _list(title: str, items: list) -> str:
    return f"\n### {title}\n" + "\n".join(f"- {i}" for i in items) + "\n" if items else ""


def plan_markdown(state: dict, checks: dict[str, list[tuple[str, str]]]) -> str:
    """The plan for every repo that still needs code. `checks` is {repo: [(name, command)]}."""
    t = state.get("ticket") or {}
    task_lines = (state.get("task") or "").strip().splitlines()
    title = t.get("title") or (task_lines[0] if task_lines else "Task")
    out = [f"# {state.get('ref') or state['ticket_key']}: {title}\n"]
    if t.get("description") or state.get("task"):
        out.append(f"## Requirement\n{t.get('description') or state['task']}\n")
    a = state.get("analysis") or {}
    if a:
        out.append(f"## Summary\n{a.get('summary', '')}\n" + _list("Acceptance criteria", a.get("acceptance_criteria", [])))
    notes = mockups.context_block(state)
    if notes:
        out.append("## Developer notes and design brief\n" + notes + "\n")
    plan = state.get("plan") or {}
    out.append(_list("Contracts fixed up front (code every repo against these)", plan.get("contracts", []))
               + _list("Migrations", plan.get("migrations", [])))
    edges = (state.get("dag") or {}).get("edges") or []
    if edges:
        out.append("\n### Order\n" + "\n".join(f"- {u} before {d}" for u, d in edges) + "\n")
    out.append("\n## Repos\n")
    for r, rs in (state.get("repos") or {}).items():
        todo = rs.get("status") in ("pending", "needs_fix")
        out.append(f"### {r}{'' if todo else ' (done, nothing to do)'}\n" + f"Worktree: {state['scope'].get(r, '')}\n")
        if todo:
            out.append(_list("Tasks", rs.get("tasks", [])) + _list("Likely files", rs.get("likely_files", [])))
            out.append(_list("Fix this first (from the checks, the review or your manual test)", rs.get("fix_instructions", [])))
            out.append(_list("Checks that must pass (run them before you finish)", [f"{n}: `{c}`" for n, c in checks.get(r, [])]))
    tests = plan.get("test_plan") or []
    if tests:
        out.append("\n## Test plan\n" + "\n".join(f"- {x['criterion']}: " + "; ".join(x["tests"]) for x in tests) + "\n")
    out.append("\nDo not commit or push: devflow commits, reviews and pushes after you say you are done.\n")
    return "".join(out)


def write_plan(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return str(path)
