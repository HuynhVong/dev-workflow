"""Environment checks for first-run setup, the UI's Connections page, `devflow doctor` and every run's preflight.

Each check is read-only: nothing is created or changed in Jira, GitLab, Confluence, git or Claude Code's config. A
`fail` that is `blocking` stops a run's preflight; a `warn` never does. Confluence write tools are listed as blocked
and are never called, not even to test them.
"""
import json
import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from .routing import Routing, repo_stack


@dataclass
class Check:
    id: str
    group: str
    label: str
    status: str                 # ok | warn | fail
    blocking: bool = False
    detail: str = ""
    fix: str = ""
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


GROUPS = ("AI", "CLIs", "Jira MCP", "Confluence MCP", "Playwright MCP", "Repos", "Skills", "Optional")


def _ok(id, group, label, detail="", **kw):
    return Check(id, group, label, "ok", detail=detail, **kw)


def _fail(id, group, label, detail, fix="", blocking=True, **kw):
    return Check(id, group, label, "fail", blocking=blocking, detail=detail, fix=fix, **kw)


def _warn(id, group, label, detail, fix="", **kw):
    return Check(id, group, label, "warn", detail=detail, fix=fix, **kw)


def tool_checks(ws, which: Callable[[str], str | None], repos: list[str]) -> list[Check]:
    out = []
    prefix = ws.vcs_prefix.split()[0] if ws.vcs_prefix else ""
    if prefix:
        out.append(_ok("cli.rtk", "CLIs", f"{prefix} installed", which(prefix) or "") if which(prefix) else
                   _fail("cli.rtk", "CLIs", f"{prefix} installed", f"'{prefix}' is not installed (every git/glab call goes through it)",
                         f"Install {prefix} and make sure it is on PATH."))
    out.append(_ok("cli.claude", "CLIs", "Claude Code CLI installed", which("claude") or "") if which("claude") else
               _fail("cli.claude", "CLIs", "Claude Code CLI installed", "Claude Code CLI ('claude') is not installed",
                     "Install Claude Code (npm i -g @anthropic-ai/claude-code) and run `claude` once to log in."))
    if any(ws.repos[r].has_ui for r in repos if r in ws.repos):
        has = any("playwright" in k.lower() for k in ws.mcp_servers)
        out.append(_ok("mcp.playwright", "Playwright MCP", "Playwright MCP configured") if has else
                   _fail("mcp.playwright", "Playwright MCP", "Playwright MCP configured",
                         "a UI repo is in scope but no Playwright MCP server is configured",
                         "Add a `playwright` server under mcp_servers in workspace.yaml (npx @playwright/mcp@latest)."))
    return out


def mcp_checks(ws, jira, confluence, need_confluence: bool = True) -> list[Check]:
    out = []
    if jira is None:
        from .jira_implement.mcp_config import ADD_JIRA_HINT
        out.append(_fail("mcp.jira", "Jira MCP", "Jira MCP reachable",
                         f"no Jira MCP: '{ws.jira_server}' is neither in workspace.yaml nor connected to Claude Code", ADD_JIRA_HINT))
    else:
        try:
            missing = jira.missing_tools()
            out.append(_fail("mcp.jira", "Jira MCP", "Jira MCP read and write tools", f"Jira MCP is missing tools {missing} (read + write needed)",
                             "Point jira_tools in workspace.yaml at your server's tool names.") if missing else
                       _ok("mcp.jira", "Jira MCP", "Jira MCP read and write tools", f"{len(ws.jira_tools)} tools present"))
        except Exception as e:  # noqa: BLE001
            out.append(_fail("mcp.jira", "Jira MCP", "Jira MCP reachable", f"Jira MCP unreachable: {e}", "Check the server command, URL and token."))
    if need_confluence:
        if confluence is None:
            out.append(_fail("mcp.confluence", "Confluence MCP", "Confluence MCP reachable",
                             f"Confluence MCP server '{ws.confluence_server}' is not configured in workspace.yaml"))
        else:
            try:
                missing, blocked = confluence.tool_report() if hasattr(confluence, "tool_report") else (confluence.missing_tools(), [])
                out.append(_fail("mcp.confluence", "Confluence MCP", "Confluence MCP read tools", f"Confluence MCP is missing read tools {missing}",
                                 "Point confluence_tools in workspace.yaml at your server's read tools.") if missing else
                           _ok("mcp.confluence", "Confluence MCP", "Confluence MCP read tools",
                               "read tools present" + (f"; write tools blocked by devflow: {', '.join(blocked)}" if blocked else ""),
                               data={"blocked_write_tools": blocked}))
            except Exception as e:  # noqa: BLE001
                out.append(_fail("mcp.confluence", "Confluence MCP", "Confluence MCP reachable", f"Confluence MCP unreachable: {e}"))
    return out


def jira_access_checks(ws, jira, source: str = "", ticket: str = "") -> list[Check]:
    """First-time setup: a Jira MCP connected to Claude Code is required. Read access is required; comment and
    attachment access decide whether the ticket review can post its result (else it saves it for you to post by hand).
    Nothing is written to test this: the real permission check is the first post. `ticket` adds a read of that ticket."""
    from .jira_implement.mcp_config import ADD_JIRA_HINT
    if jira is None:
        return [_fail("mcp.jira.setup", "Jira MCP", "Jira MCP connected to Claude Code",
                      f"no Jira MCP found ('{ws.jira_server}' is neither in workspace.yaml nor in Claude Code's MCP servers)", ADD_JIRA_HINT)]
    out = []
    try:
        rep = jira.access_report()
    except Exception as e:  # noqa: BLE001
        return [_fail("mcp.jira.setup", "Jira MCP", "Jira MCP connected to Claude Code", f"Jira MCP did not answer: {e}",
                      "Check the server command, URL and token (`claude mcp list` shows its state).")]
    where = f" (from {source})" if source else ""
    out.append(_ok("mcp.jira.read", "Jira MCP", "Jira MCP can read tickets", f"read tools present{where}") if rep["read"] else
               _fail("mcp.jira.read", "Jira MCP", "Jira MCP can read tickets", f"missing read tools {rep['missing_read']}{where}",
                     "Point jira_tools in workspace.yaml at your server's tool names."))
    if rep["comment"]:
        out.append(_ok("mcp.jira.comment", "Jira MCP", "Jira MCP can post comments", "comment tools present"))
    else:
        out.append(_warn("mcp.jira.comment", "Jira MCP", "Jira MCP can post comments",
                         f"no comment tool {rep['missing_write']} (read-only mode?): the ticket review saves its comment "
                         "locally for you to post by hand",
                         "Turn off the server's read-only mode (e.g. READ_ONLY_MODE for mcp-atlassian) or map jira_tools.add_comment."))
    out.append(_ok("mcp.jira.attach", "Jira MCP", "Jira MCP can attach files", "attachment tool present") if rep["attach"] else
               _warn("mcp.jira.attach", "Jira MCP", "Jira MCP can attach files",
                     "no attachment tool: proof screenshots stay local and the comment lists their names",
                     "Map jira_tools.attach to your server's upload tool (mcp-atlassian: jira_update_issue)."))
    out.append(_ok("mcp.jira.search", "Jira MCP", "Jira MCP can search (JQL)", "search tool present") if rep.get("search") else
               _warn("mcp.jira.search", "Jira MCP", "Jira MCP can search (JQL)",
                     f"no JQL search tool ('{ws.jira_tools.get('search')}'): the standup can't find your tickets",
                     "Map jira_tools.search to your server's JQL search tool (mcp-atlassian: jira_search)."))
    if ticket:
        try:
            jira.status_and_assignee(ticket)
            out.append(_ok("mcp.jira.ticket", "Jira MCP", f"Read {ticket}", "ticket readable"))
        except Exception as e:  # noqa: BLE001
            out.append(_fail("mcp.jira.ticket", "Jira MCP", f"Read {ticket}", f"cannot read {ticket}: {str(e)[:200]}",
                             "Check the token's permissions for that project."))
    return out


def jira_gateway(ws):
    """(JiraGateway, source) for the Jira server from workspace.yaml or Claude Code, or (None, "")."""
    from .jira_implement.jira import JiraGateway
    from .jira_implement.mcp_client import StdioOrHttpMcp
    from .jira_implement.mcp_config import resolve_jira_server
    found = resolve_jira_server(ws)
    if not found:
        return None, ""
    name, cfg, source = found
    return JiraGateway(StdioOrHttpMcp(name, cfg), ws.jira_tools, ws.status_order), source


def origin_host(url: str) -> str:
    """Host of a git remote URL: https://host/..., ssh://git@host:2222/..., or scp-style git@host:group/repo.git."""
    url = url.strip()
    if "://" in url:
        return (urlparse(url).hostname or "").lower()
    m = re.match(r"^(?:[^@/]+@)?([^:/]+):", url)
    return m.group(1).lower() if m else ""


def glab_auth_checks(ws, vcs, repos: list[str], first: str | None) -> list[Check]:
    """`glab auth status --hostname <host>` once per GitLab host the repos' origins point to (e.g. a self-hosted
    GitLab), so a stale gitlab.com login the workflows never use does not fail the check."""
    hosts: dict[str, str] = {}  # host -> a repo on it, to run glab from
    for name in repos:
        if Path(ws.repos[name].path, ".git").exists():
            try:
                url = vcs.run(name, "git", "remote", "get-url", "origin", check=False).stdout or ""
            except Exception:  # noqa: BLE001
                url = ""
            if host := origin_host(url):
                hosts.setdefault(host, name)
    if not hosts:
        label = f"{ws.vcs_prefix} glab auth status".strip()
        return [_fail("cli.glab", "CLIs", label, "not checked: no repo with an origin remote found yet", blocking=False)
                if first is None or not repos else
                _fail("cli.glab", "CLIs", label, "not checked: the repos in workspace.yaml have no origin remote", blocking=False)]
    out = []
    for host, repo in hosts.items():
        label = f"{ws.vcs_prefix} glab auth status --hostname {host}".strip()
        cid = "cli.glab" if len(hosts) == 1 else f"cli.glab.{host}"
        try:
            r = vcs.run(repo, "glab", "auth", "status", "--hostname", host, check=False)
            text = (r.stdout or r.stderr or "").strip()[:200]
            out.append(_ok(cid, "CLIs", label, text or "logged in") if r.returncode == 0 else
                       _fail(cid, "CLIs", label, f"`{label}` failed: {text}",
                             f"Create a personal access token on {host} (scopes: api, read_repository, write_repository), then "
                             f"run `glab auth login --hostname {host}`."))
        except Exception as e:  # noqa: BLE001
            out.append(_fail(cid, "CLIs", label, f"`{label}` failed: {e}"))
    return out


def vcs_checks(ws, vcs, repos: list[str], glab: bool = True) -> tuple[list[Check], list[str]]:
    """git/glab through rtk, then each repo: a git repo whose origin is reachable. Returns (checks, reachable repos)."""
    out, ok_repos = [], []
    if not repos:
        return out, ok_repos
    first = next((r for r in repos if Path(ws.repos[r].path).is_dir()), None)
    label = f"{ws.vcs_prefix} git --version".strip()
    if first is None:
        out.append(_fail("cli.git", "CLIs", label, "not checked: no repo folder in workspace.yaml exists yet", blocking=False))
    else:
        try:
            r = vcs.run(first, "git", "--version", check=False)
            out.append(_ok("cli.git", "CLIs", label, (r.stdout or "").strip()[:200]) if r.returncode == 0 else
                       _fail("cli.git", "CLIs", label, f"`{label}` failed: {(r.stderr or r.stdout).strip()[:200]}"))
        except Exception as e:  # noqa: BLE001
            out.append(_fail("cli.git", "CLIs", label, f"`{label}` failed: {e}"))
    if glab:
        out += glab_auth_checks(ws, vcs, repos, first)
    for name in repos:
        cfg = ws.repos[name]
        if not Path(cfg.path, ".git").exists():
            out.append(_fail(f"repo.{name}", "Repos", name, f"{name}: {cfg.path} is not a git repository",
                             "Fix `path` for this repo in workspace.yaml."))
            continue
        if vcs.run(name, "git", "ls-remote", "--heads", "origin", check=False).returncode != 0:
            out.append(_fail(f"repo.{name}", "Repos", name, f"{name}: cannot reach origin", "Check your network and git credentials."))
            continue
        has_base = bool(vcs.git(name, "ls-remote", "--heads", "origin", cfg.base_branch, check=False))
        dirty = vcs.is_dirty(name)
        detail = f"origin reachable, {cfg.base_branch} " + ("exists" if has_base else "missing")
        if dirty:
            detail += "; your clone has uncommitted changes (fine: devflow works in worktrees)"
        out.append(_ok(f"repo.{name}", "Repos", name, detail, data={"path": cfg.path, "stack": repo_stack(cfg.path)}) if has_base else
                   _fail(f"repo.{name}", "Repos", name, f"{name}: origin has no branch {cfg.base_branch}", "Set base_branch for this repo."))
        if has_base:
            ok_repos.append(name)
    return out, ok_repos


def ai_checks(env=os.environ, which: Callable[[str], str | None] = shutil.which) -> list[Check]:
    from .config import llm_backend
    if llm_backend(env=env) == "claude-cli":
        label = "AI steps via Claude Code login"
        return [_ok("ai.key", "AI", label, "no API key; runs on your Claude Pro/Max plan and its usage limits") if which("claude") else
                _fail("ai.key", "AI", label, "Claude Code CLI ('claude') is not installed",
                      "Install Claude Code and run `claude` once to log in, or set ANTHROPIC_API_KEY to use the API instead.")]
    key = bool(env.get("ANTHROPIC_API_KEY"))
    return [_ok("ai.key", "AI", "ANTHROPIC_API_KEY", "set") if key else
            _fail("ai.key", "AI", "ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY is not set",
                  "Add it to .env or your shell profile, or unset DEVFLOW_LLM_BACKEND to use your Claude Code login.")]


def skill_checks(routing: Routing) -> list[Check]:
    missing = routing.missing()
    out = []
    for step in routing.steps:
        gone = missing.get(step, [])
        wanted = list(routing.step(step).skills)
        label = f"{step} ({routing.model(step)})"
        if gone:
            out.append(_warn(f"skill.{step}", "Skills", label, f"not installed: {', '.join(gone)} (the step runs without them)",
                             "Install them under ~/.claude/skills/<name>/SKILL.md or map the step to a skill you have.",
                             data={"step": step, "missing": gone, "wanted": wanted, "model": routing.model(step)}))
        else:
            out.append(_ok(f"skill.{step}", "Skills", label, ", ".join(wanted) or "no preferred skills",
                           data={"step": step, "missing": [], "wanted": wanted, "model": routing.model(step)}))
    return out


def optional_checks(ws) -> list[Check]:
    """MCP servers in the developer's Claude Code config that workspace.yaml does not list (read only)."""
    found = {}
    for f in (Path.home() / ".claude.json", Path.home() / ".claude" / "settings.json", Path.cwd() / ".mcp.json"):
        try:
            raw = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        for name, cfg in (raw.get("mcpServers") or {}).items():
            found.setdefault(name, {"source": str(f), "config": {k: v for k, v in cfg.items() if k != "env"}})
    return [_ok(f"optional.{n}", "Optional", f"{n} (from Claude Code)", f"not in workspace.yaml; found in {v['source']}", data=v)
            for n, v in found.items() if n not in ws.mcp_servers]


def run(ws, deps=None, repos: list[str] | None = None, which: Callable = shutil.which, include_optional: bool = True,
        vcs_runner=None, workflow: str = "") -> list[Check]:
    """Every check, for the setup screen and `devflow doctor`. `deps` (a jira_implement Deps) supplies the MCP
    gateways and fakes in tests; without it the gateways come from workspace.yaml or Claude Code. `workflow` =
    "ticket_review" checks only what that workflow needs: no glab, no worktrees, Confluence optional, Playwright MCP."""
    review = workflow == "ticket_review"
    from .jira_implement import worktrees
    repos = list(repos if repos is not None else ws.repos)
    jira = confluence = None
    source = ""
    if deps is not None:
        jira, confluence, which, vcs_runner = deps.jira, deps.confluence, deps.which, deps.vcs_runner
    else:
        from .jira_implement.confluence import ConfluenceReader
        from .jira_implement.mcp_client import StdioOrHttpMcp
        jira, source = jira_gateway(ws)
        if ws.confluence_server in ws.mcp_servers:
            confluence = ConfluenceReader(StdioOrHttpMcp(ws.confluence_server, ws.mcp_servers[ws.confluence_server]), ws.confluence_tools)
        elif jira is not None and ws.confluence_server == ws.jira_server:
            confluence = ConfluenceReader(jira.mcp, ws.confluence_tools)  # one Atlassian server; Confluence stays read-only
    checks = ai_checks() + tool_checks(ws, which, repos) + mcp_checks(ws, jira, confluence, need_confluence=not review)
    checks += [c for c in jira_access_checks(ws, jira, source) if c.id != "mcp.jira.setup"]
    if review and not any(c.id == "mcp.playwright" for c in checks):
        has = any("playwright" in k.lower() for k in ws.mcp_servers)
        checks.append(_ok("mcp.playwright", "Playwright MCP", "Playwright MCP configured") if has else
                      _fail("mcp.playwright", "Playwright MCP", "Playwright MCP configured", "the ticket review's E2E tests need a Playwright MCP",
                            "Add a `playwright` server under mcp_servers in workspace.yaml (npx @playwright/mcp@latest)."))
    known = [r for r in repos if r in ws.repos]
    if known:
        more, _ = vcs_checks(ws, worktrees.source_vcs(ws, known, vcs_runner), known, glab=not review)
        checks += more
    if not review:
        root = Path(ws.worktree_root)
        probe = root if root.exists() else next((p for p in root.parents if p.exists()), Path("/"))
        checks.append(_ok("repo.worktree_root", "Repos", "Worktree folder", f"{root} (writable)") if os.access(probe, os.W_OK) else
                      _fail("repo.worktree_root", "Repos", "Worktree folder", f"{root} is not writable", "Set worktree_root in workspace.yaml."))
    routing = deps.routing if deps is not None else Routing.from_config(ws.ai)
    checks += skill_checks(routing)
    if include_optional:
        checks += optional_checks(ws)
    return checks


def ping_models(routing: Routing) -> list[Check]:
    """Optional: one tiny call per model tier (costs a few tokens). Only run when the developer asks."""
    import subprocess

    import anthropic

    from .config import llm_backend
    cli = llm_backend() == "claude-cli"
    out, client = [], None if cli else anthropic.Anthropic()
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    for tier in ("haiku", "sonnet", "opus"):
        model = routing.models.get(tier, tier)
        try:
            if cli:
                proc = subprocess.run(["claude", "-p", "--model", model, "--tools", "", "--no-session-persistence"], input="Reply with OK.",
                                      capture_output=True, text=True, env=env, timeout=120)
                if proc.returncode:
                    raise RuntimeError((proc.stderr or proc.stdout).strip())
            else:
                client.messages.create(model=model, max_tokens=8, messages=[{"role": "user", "content": "Reply with OK."}])
            out.append(_ok(f"ai.{tier}", "AI", f"{tier}: {model}", "reachable"))
        except Exception as e:  # noqa: BLE001
            out.append(_fail(f"ai.{tier}", "AI", f"{tier}: {model}", f"{type(e).__name__}: {str(e)[:200]}", blocking=False))
    return out


def summary(checks: list[Check]) -> dict:
    return {"ok": sum(c.status == "ok" for c in checks), "warn": sum(c.status == "warn" for c in checks),
            "fail": sum(c.status == "fail" for c in checks), "blocking": sum(c.status == "fail" and c.blocking for c in checks),
            "status": "fail" if any(c.status == "fail" and c.blocking for c in checks) else
                      "warn" if any(c.status != "ok" for c in checks) else "ok"}


def render(checks: list[Check]) -> str:
    marks = {"ok": "✓", "warn": "!", "fail": "✗"}
    lines = []
    for g in GROUPS:
        sel = [c for c in checks if c.group == g]
        if sel:
            lines.append(f"\n{g}")
            lines += [f"  {marks[c.status]} {c.label}: {c.detail}" + (f"\n      fix: {c.fix}" if c.fix and c.status != "ok" else "") for c in sel]
    s = summary(checks)
    lines.append(f"\n{s['ok']} ok, {s['warn']} warnings, {s['fail']} failures ({s['blocking']} blocking)")
    return "\n".join(lines).strip()
