"""workspace.yaml: the repos a developer works on and the tool names of their MCP servers."""
import os
import re
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
    setup_commands: tuple[str, ...] = ()  # run once in a fresh worktree, e.g. `npm ci`
    copy_files: tuple[str, ...] = ()      # copied from the main clone into a fresh worktree, e.g. `.env.local`
    parallel_checks: bool = True          # false: checks run one run at a time (fixed ports, shared local DB)

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
    worktree_root: str = "~/devflow-worktrees"  # each run works in <worktree_root>/<TICKET>/<repo>
    max_parallel_runs: int = 3                  # the UI's run manager queues the rest
    prices: dict = field(default_factory=dict)  # USD per million tokens per model, for cost estimates
    ui: dict = field(default_factory=dict)      # graph_sources, port, notifications
    ai: dict = field(default_factory=dict)  # per-step models and skills, see dev_workflows.routing

    def repo(self, name: str) -> RepoConfig:
        return self.repos[name]

    def worktree(self, ticket: str, repo: str) -> str:
        return str(Path(self.worktree_root, ticket, repo))


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
            setup_commands=tuple(r.get("setup_commands") or ()), copy_files=tuple(r.get("copy_files") or ()),
            parallel_checks=bool(r.get("parallel_checks", True)),
        )
    state_dir = Path(raw.get("state_dir", ".devflow")).expanduser()
    if not state_dir.is_absolute():
        state_dir = base / state_dir
    worktree_root = Path(raw.get("worktree_root", "~/devflow-worktrees")).expanduser()
    if not worktree_root.is_absolute():
        worktree_root = base / worktree_root
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
        worktree_root=str(worktree_root),
        max_parallel_runs=int(raw.get("max_parallel_runs", 3)),
        prices=dict(raw.get("prices") or {}),
        ui=dict(raw.get("ui") or {}),
        ai=dict(raw.get("ai") or {}),
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


# --- reading and writing workspace.yaml from the UI (secrets never leave the machine) --------------------------
MASK = "•••• (set; kept as is)"
_SECRET_KEY = ("token", "secret", "password", "apikey", "api_key", "authorization")
_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def read_raw(path: str | Path) -> dict:
    p = Path(path).expanduser()
    return (yaml.safe_load(p.read_text()) or {}) if p.exists() else {}


def masked(raw: dict) -> tuple[dict, dict[str, str]]:
    """workspace.yaml as the browser may see it: literal secrets in MCP server settings are replaced by MASK, and
    ${VAR} references are kept with whether VAR is set. Returns (masked copy, {VAR: "set"|"missing"})."""
    refs: dict[str, str] = {}

    def walk(value, secret=False):
        if isinstance(value, dict):
            return {k: walk(v, secret or k == "env" or k == "headers" or any(s in k.lower() for s in _SECRET_KEY))
                    for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v, secret) for v in value]
        if isinstance(value, str):
            for m in re.finditer(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value):
                refs[m.group(1)] = "set" if os.environ.get(m.group(1)) else "missing"
            if secret and value and not _REF.match(value) and not _looks_public(value):
                return MASK
        return value

    out = dict(raw)
    if "mcp_servers" in out:
        out["mcp_servers"] = walk(out["mcp_servers"])
    return out, refs


def _looks_public(v: str) -> bool:
    """URLs and e-mail addresses in an env block are settings, not secrets."""
    return v.startswith(("http://", "https://")) or ("@" in v and " " not in v and len(v) < 120)


def unmask(new: dict, old: dict) -> dict:
    """Put the real secrets back wherever the browser sent MASK."""
    if isinstance(new, dict):
        return {k: unmask(v, (old or {}).get(k) if isinstance(old, dict) else None) for k, v in new.items()}
    if isinstance(new, list):
        return [unmask(v, old[i] if isinstance(old, list) and i < len(old) else None) for i, v in enumerate(new)]
    return old if new == MASK else new


def save_raw(path: str | Path, data: dict) -> Workspace:
    """Validate (it must load) and write workspace.yaml. Masked values keep their current secret."""
    p = Path(path).expanduser()
    data = unmask(data, read_raw(p))
    tmp = p.with_suffix(p.suffix + ".tmp")
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
    try:
        ws = load_workspace(tmp)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(p)
    return ws
