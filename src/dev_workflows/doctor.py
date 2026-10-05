"""Environment checks for first-run setup, the UI's Connections page, `devflow doctor` and every run's preflight.

Each check is read-only: nothing is created or changed in Jira, GitLab, Confluence, git or Claude Code's config. A
`fail` that is `blocking` stops a run's preflight; a `warn` never does. Confluence write tools are listed as blocked
and are never called, not even to test them.
"""
import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

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
        out.append(_fail("mcp.jira", "Jira MCP", "Jira MCP reachable", f"Jira MCP server '{ws.jira_server}' is not configured in workspace.yaml",
                         "Add it under mcp_servers (see workspace.example.yaml)."))
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


def vcs_checks(ws, vcs, repos: list[str]) -> tuple[list[Check], list[str]]:
    """git/glab through rtk, then each repo: a git repo whose origin is reachable. Returns (checks, reachable repos)."""
    out, ok_repos = [], []
    if not repos:
        return out, ok_repos
    first = next((r for r in repos if Path(ws.repos[r].path).is_dir()), None)
    for args in (("git", "--version"), ("glab", "auth", "status")):
        label = f"{ws.vcs_prefix} {' '.join(args)}".strip()
        if first is None:
            out.append(_fail(f"cli.{args[0]}", "CLIs", label, "not checked: no repo folder in workspace.yaml exists yet", blocking=False))
            continue
        try:
            r = vcs.run(first, *args, check=False)
            out.append(_ok(f"cli.{args[0]}", "CLIs", label, (r.stdout or "").strip()[:200]) if r.returncode == 0 else
                       _fail(f"cli.{args[0]}", "CLIs", label, f"`{label}` failed: {(r.stderr or r.stdout).strip()[:200]}",
                             "Install it and log in (`glab auth login`)." if args[0] == "glab" else ""))
        except Exception as e:  # noqa: BLE001
            out.append(_fail(f"cli.{args[0]}", "CLIs", label, f"`{label}` failed: {e}"))
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


def ai_checks(env=os.environ) -> list[Check]:
    key = bool(env.get("ANTHROPIC_API_KEY"))
    return [_ok("ai.key", "AI", "ANTHROPIC_API_KEY", "set") if key else
            _fail("ai.key", "AI", "ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY is not set", "Add it to .env or your shell profile.")]


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
        vcs_runner=None) -> list[Check]:
    """Every check, for the setup screen and `devflow doctor`. `deps` (a jira_implement Deps) supplies the MCP
    gateways and fakes in tests; without it the gateways are built from workspace.yaml."""
    from .jira_implement import worktrees
    repos = list(repos if repos is not None else ws.repos)
    jira = confluence = None
    if deps is not None:
        jira, confluence, which, vcs_runner = deps.jira, deps.confluence, deps.which, deps.vcs_runner
    else:
        from .jira_implement.confluence import ConfluenceReader
        from .jira_implement.jira import JiraGateway
        from .jira_implement.mcp_client import StdioOrHttpMcp
        if ws.jira_server in ws.mcp_servers:
            jira = JiraGateway(StdioOrHttpMcp(ws.jira_server, ws.mcp_servers[ws.jira_server]), ws.jira_tools, ws.status_order)
        if ws.confluence_server in ws.mcp_servers:
            confluence = ConfluenceReader(StdioOrHttpMcp(ws.confluence_server, ws.mcp_servers[ws.confluence_server]), ws.confluence_tools)
    checks = ai_checks() + tool_checks(ws, which, repos) + mcp_checks(ws, jira, confluence)
    known = [r for r in repos if r in ws.repos]
    if known:
        more, _ = vcs_checks(ws, worktrees.source_vcs(ws, known, vcs_runner), known)
        checks += more
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
    import anthropic
    out, client = [], anthropic.Anthropic()
    for tier in ("haiku", "sonnet", "opus"):
        model = routing.models.get(tier, tier)
        try:
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
