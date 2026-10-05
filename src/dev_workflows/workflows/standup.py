"""Standup notes from your recent git activity plus any notes you paste in.

collect_git -> digest -> render
"""
import os
import shlex
import subprocess

from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field
from typing_extensions import NotRequired, TypedDict

from ..llm import StructuredLLM, default_llm


class Standup(BaseModel):
    yesterday: list[str] = Field(description="Done since last standup, outcome-focused, one line each.")
    today: list[str]
    blockers: list[str]


def _prepare(values: dict, workspace=None) -> tuple[str, dict]:
    import time
    names = values.get("repos") or (list(workspace.repos) if workspace else [])
    paths = [workspace.repos[n].path for n in names if workspace and n in workspace.repos] or list(values.get("repo_paths") or [])
    return f"Standup {time.strftime('%a %d %b')}", {"repo_paths": paths, "since": values.get("since") or "yesterday",
                                                   "author": values.get("author") or "", "notes": values.get("notes", "")}


DEVFLOW_UI = {
    "title": "Standup",
    "description": "Reads your git log across repos plus your notes and writes Yesterday / Today / Blockers.",
    "icon": "calendar", "color": "#4dd0e1",
    "steps": ["collect_git", "digest", "render"],
    "nodes": {"collect_git": "Read git log", "digest": "Summarize", "render": "Write standup"},
    "form": [{"name": "repos", "label": "Repos", "type": "repos", "help": "Empty means every repo in workspace.yaml."},
             {"name": "since", "label": "Since", "type": "text", "default": "yesterday"},
             {"name": "author", "label": "Author", "type": "text", "help": "Defaults to every author; use your git email."},
             {"name": "notes", "label": "Notes (optional)", "type": "textarea"}],
    "prepare": _prepare,
}


class State(TypedDict):
    notes: NotRequired[str]  # free text: PRs, tickets, meetings, anything
    repo_paths: NotRequired[list[str]]
    since: NotRequired[str]  # passed to git --since, e.g. "yesterday" or "3 days ago"
    author: NotRequired[str]
    git_log: NotRequired[str]
    standup: NotRequired[Standup]
    output: NotRequired[str]


def read_git_log(repo: str, since: str, author: str | None) -> str:
    # Project rule: every git call goes through rtk (override with DEVFLOW_VCS_PREFIX="" to disable).
    prefix = shlex.split(os.getenv("DEVFLOW_VCS_PREFIX", "rtk"))
    cmd = [*prefix, "git", "-C", repo, "log", f"--since={since}", "--no-merges", "--pretty=format:%h %ad %s", "--date=short", "--all"]
    if author:
        cmd.append(f"--author={author}")
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=True).stdout
    except (subprocess.SubprocessError, FileNotFoundError) as e:
        return f"[{repo}] could not read git log: {e}"
    return f"[{repo}]\n{out.strip() or '(no commits)'}"


SYSTEM = (
    "You write crisp daily standup updates for a full-stack developer. Group related commits "
    "into one outcome, skip noise like formatting or merge commits, and never invent work."
)


def build_graph(llm: StructuredLLM | None = None, checkpointer=None):
    def get_llm() -> StructuredLLM:
        return llm or default_llm()

    def collect_git(state: State):
        since = state.get("since", "yesterday")
        logs = [read_git_log(r, since, state.get("author")) for r in state.get("repo_paths", [])]
        return {"git_log": "\n\n".join(logs)}

    def digest(state: State):
        prompt = (
            f"<git_log>\n{state.get('git_log') or '(none)'}\n</git_log>\n"
            f"<notes>\n{state.get('notes') or '(none)'}\n</notes>\n\n"
            "Write my standup. Infer 'today' from unfinished work and my notes; leave lists empty rather than guess."
        )
        return {"standup": get_llm().structured(SYSTEM, prompt, Standup, step="standup")}

    def render(state: State):
        s = state["standup"]
        sec = lambda title, items: [f"**{title}**", *([f"- {x}" for x in items] or ["- None"]), ""]
        return {"output": "\n".join(sec("Yesterday", s.yesterday) + sec("Today", s.today) + sec("Blockers", s.blockers))}

    g = StateGraph(State)
    g.add_node("collect_git", collect_git)
    g.add_node("digest", digest)
    g.add_node("render", render)
    g.add_edge(START, "collect_git")
    g.add_edge("collect_git", "digest")
    g.add_edge("digest", "render")
    g.add_edge("render", END)
    return g.compile(checkpointer=checkpointer)


graph = build_graph()
