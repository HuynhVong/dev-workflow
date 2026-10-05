"""workspace.yaml: the repos a developer works on and the tool names of their MCP servers."""
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class RepoConfig:
    name: str
    path: str
    gitlab_project: str = ""
    base_branch: str = "develop"
    has_ui: bool = False
    commands: dict[str, str] = field(default_factory=dict)  # lint, typecheck, test, build, run

    @property
    def check_commands(self) -> list[tuple[str, str]]:
        return [(k, self.commands[k]) for k in ("lint", "typecheck", "test", "build") if self.commands.get(k)]


# Defaults match the sooperset/mcp-atlassian server; override under `jira_tools` / `confluence_tools`.
DEFAULT_JIRA_TOOLS = {
    "get_issue": "jira_get_issue",
    "get_transitions": "jira_get_transitions",
    "transition_issue": "jira_transition_issue",
    "add_comment": "jira_add_comment",
    "edit_comment": "jira_edit_comment",
    "download_attachments": "jira_download_attachments",
    "get_user_profile": "jira_get_user_profile",
}
DEFAULT_CONFLUENCE_READ_TOOLS = {
    "get_page": "confluence_get_page",
    "get_page_children": "confluence_get_page_children",
    "search": "confluence_search",
    "get_attachments": "confluence_get_attachments",
}


@dataclass(frozen=True)
class Workspace:
    repos: dict[str, RepoConfig]
    mcp_servers: dict[str, dict]
    jira_tools: dict[str, str]
    confluence_tools: dict[str, str]
    jira_user: str = ""
    jira_server: str = "atlassian"
    confluence_server: str = "atlassian"
    status_order: tuple[str, ...] = ("To Do", "In Progress", "Code Review")
    review_status: str = "Code Review"
    vcs_prefix: str = "rtk"
    state_dir: str = ".devflow"
    max_fix_attempts: int = 3

    def repo(self, name: str) -> RepoConfig:
        return self.repos[name]


def load_workspace(path: str | Path) -> Workspace:
    path = Path(path).expanduser()
    raw = yaml.safe_load(path.read_text()) or {}
    base = path.parent
    repos = {}
    for name, r in (raw.get("repos") or {}).items():
        repo_path = Path(r["path"]).expanduser()
        if not repo_path.is_absolute():
            repo_path = (base / repo_path).resolve()
        repos[name] = RepoConfig(
            name=name, path=str(repo_path), gitlab_project=r.get("gitlab_project", ""),
            base_branch=r.get("base_branch", "develop"), has_ui=bool(r.get("has_ui", False)),
            commands=dict(r.get("commands") or {}),
        )
    state_dir = Path(raw.get("state_dir", ".devflow")).expanduser()
    if not state_dir.is_absolute():
        state_dir = base / state_dir
    return Workspace(
        repos=repos,
        mcp_servers=_expand(dict(raw.get("mcp_servers") or {})),
        jira_tools={**DEFAULT_JIRA_TOOLS, **(raw.get("jira_tools") or {})},
        confluence_tools={**DEFAULT_CONFLUENCE_READ_TOOLS, **(raw.get("confluence_tools") or {})},
        jira_user=raw.get("jira_user", ""),
        jira_server=raw.get("jira_server", "atlassian"),
        confluence_server=raw.get("confluence_server", "atlassian"),
        status_order=tuple(raw.get("status_order") or ("To Do", "In Progress", "Code Review")),
        review_status=raw.get("review_status", "Code Review"),
        vcs_prefix=raw.get("vcs_prefix", "rtk"),
        state_dir=str(state_dir),
        max_fix_attempts=int(raw.get("max_fix_attempts", 3)),
    )


def _expand(value):
    """${VAR} in MCP server settings comes from the environment, so tokens stay out of workspace.yaml."""
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value
