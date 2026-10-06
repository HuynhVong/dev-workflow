"""Which MCP server to use for Jira: workspace.yaml first, else the one the developer connected to Claude Code.

Claude Code keeps MCP servers in ~/.claude.json (user scope at the top level, local scope under `projects`),
~/.claude/settings.json and a project's .mcp.json. They are only read here, never changed.
"""
import json
import os
import re
from pathlib import Path


def claude_code_config_files() -> list[Path]:
    home = Path(os.getenv("CLAUDE_CONFIG_DIR", Path.home() / ".claude")).expanduser()
    return [Path.home() / ".claude.json", home / "settings.json", Path.cwd() / ".mcp.json"]


def claude_code_mcp_servers(files: list[Path] | None = None) -> dict[str, dict]:
    """name -> {"config": {...}, "source": file}. The first file that defines a name wins."""
    found: dict[str, dict] = {}
    for f in files if files is not None else claude_code_config_files():
        try:
            raw = json.loads(Path(f).read_text())
        except (OSError, json.JSONDecodeError):
            continue
        blocks = [raw.get("mcpServers") or {}]
        blocks += [(p or {}).get("mcpServers") or {} for p in (raw.get("projects") or {}).values() if isinstance(p, dict)]
        for block in blocks:
            for name, cfg in block.items():
                if isinstance(cfg, dict):
                    found.setdefault(name, {"config": _normalize(cfg), "source": str(f)})
    return found


_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand(value):
    """Claude Code's `${VAR}` / `${VAR:-default}` in command, args, env, url and headers, at any depth."""
    if isinstance(value, str):
        def sub(m):
            name, default = m.groups()
            return os.environ.get(name, m.group(0)) if default is None else os.environ.get(name) or default
        return os.path.expandvars(_VAR_RE.sub(sub, value))
    if isinstance(value, list):
        return [expand(v) for v in value]
    if isinstance(value, dict):
        return {k: expand(v) for k, v in value.items()}
    return value


def _normalize(cfg: dict) -> dict:
    """Claude Code's {"type": "http"|"sse", "url": ..., "headers": ...} / {"command", "args", "env"} as our MCP client
    takes it."""
    return expand({k: v for k, v in cfg.items() if k in ("type", "command", "args", "env", "url", "headers")})


def resolve_jira_server(ws, files: list[Path] | None = None) -> tuple[str, dict, str] | None:
    """(name, config, source) of the Jira MCP server, or None. workspace.yaml's `jira_server` wins; otherwise the
    Claude Code server with that name, else the first Claude Code server whose name mentions jira or atlassian."""
    if ws.jira_server in ws.mcp_servers:
        return ws.jira_server, ws.mcp_servers[ws.jira_server], "workspace.yaml"
    cc = claude_code_mcp_servers(files)
    if ws.jira_server in cc:
        return ws.jira_server, cc[ws.jira_server]["config"], cc[ws.jira_server]["source"]
    for name, v in cc.items():
        if "jira" in name.lower() or "atlassian" in name.lower():
            return name, v["config"], v["source"]
    return None


ADD_JIRA_HINT = ("Connect a Jira MCP to Claude Code, e.g. `claude mcp add --scope user atlassian -- uvx mcp-atlassian` with "
                 "JIRA_URL, JIRA_USERNAME and JIRA_API_TOKEN set (see workspace.example.yaml), then run `devflow setup` again.")


# --- which client talks to each server ---------------------------------------------------------------------------
def jira_mcp_for(ws, files: list[Path] | None = None):
    """(McpTools, source) for Jira, or (None, ""): the Claude Code server named under claude_code_mcp.jira (its
    login and connectors), else devflow's own client for the server resolve_jira_server finds."""
    from .mcp_client import StdioOrHttpMcp
    if ws.claude_code_mcp.get("jira"):
        from .claude_code_mcp import jira_mcp
        return jira_mcp(ws), f"Claude Code login: {ws.claude_code_mcp['jira']}"
    found = resolve_jira_server(ws, files)
    if not found:
        return None, ""
    name, cfg, source = found
    return StdioOrHttpMcp(name, cfg), source


def confluence_mcp_for(ws, jira_mcp=None, files: list[Path] | None = None):
    """McpTools for Confluence (always read-only above this layer), or None: claude_code_mcp.confluence, else the
    workspace's confluence_server, else the Jira server when both are the same Atlassian server."""
    from .mcp_client import StdioOrHttpMcp
    if ws.claude_code_mcp.get("confluence"):
        from .claude_code_mcp import confluence_mcp
        return confluence_mcp(ws)
    if ws.confluence_server in ws.mcp_servers:
        return StdioOrHttpMcp(ws.confluence_server, ws.mcp_servers[ws.confluence_server])
    if ws.confluence_server != ws.jira_server:
        return None
    if ws.claude_code_mcp.get("jira"):  # the default Jira server, not the Claude Code one chosen for Jira only
        found = resolve_jira_server(ws, files)
        return StdioOrHttpMcp(found[0], found[1]) if found else None
    return jira_mcp


def playwright_servers(ws, files: list[Path] | None = None) -> dict[str, dict]:
    """Playwright MCP configs for the coding/E2E agents: workspace mcp_servers, else the Claude Code server named
    under claude_code_mcp.playwright (its config from Claude Code's files, so devflow can still set its screenshot
    and profile folders). An account-level server with no local config returns {} and is inherited by the agent."""
    found = {k: v for k, v in ws.mcp_servers.items() if "playwright" in k.lower()}
    name = ws.claude_code_mcp.get("playwright")
    if found or not name:
        return found
    cc = claude_code_mcp_servers(files)
    return {name: cc[name]["config"]} if name in cc else {}


def has_playwright(ws) -> bool:
    return bool(ws.claude_code_mcp.get("playwright")) or any("playwright" in k.lower() for k in ws.mcp_servers)


# --- the dev MySQL MCP, always behind devflow-sql ------------------------------------------------------------------
def resolve_sql_server(ws, files: list[Path] | None = None) -> tuple[str, dict, str] | None:
    """(name, config, source) of the MySQL MCP server, or None when none is configured. workspace.yaml's
    `sql.server` under mcp_servers wins, else the Claude Code server chosen under claude_code_mcp.mysql (or named by
    sql.server). Raises ValueError when the chosen server has no local config devflow can start (an account-level
    connector), or its config names a host in sql.deny_hosts."""
    sql = getattr(ws, "sql", None) or {}
    name = str(sql.get("server") or "").strip()
    found = None
    if name and name in ws.mcp_servers:
        found = name, ws.mcp_servers[name], "workspace.yaml"
    else:
        name = ws.claude_code_mcp.get("mysql") or name
        if not name:
            return None
        cc = claude_code_mcp_servers(files)
        if name not in cc:
            raise ValueError(f"no local config for MySQL MCP '{name}' (looked in workspace.yaml mcp_servers and "
                             f"Claude Code's {', '.join(str(f) for f in (files if files is not None else claude_code_config_files()))}); "
                             "devflow has to start it itself to reconnect it, so add it with `claude mcp add` or "
                             "under mcp_servers in workspace.yaml")
        found = name, cc[name]["config"], cc[name]["source"]
    text = json.dumps(found[1]).lower()
    for host in sql.get("deny_hosts") or []:
        if str(host).strip() and str(host).strip().lower() in text:
            raise ValueError(f"MySQL MCP '{found[0]}' points at {host}, which is in sql.deny_hosts")
    return found


def sql_servers(ws, files: list[Path] | None = None) -> tuple[dict[str, dict], str]:
    """({"devflow-sql": stdio config}, upstream server name) for the coding agents, or ({}, "") when no MySQL MCP
    is configured or it can't be used (doctor says why). The agents get only the proxy; the upstream's own tools are
    denied to them so every query goes through the read-only guard and the reconnect logic."""
    import sys

    from .sql_mcp import ENV_VAR, PROXY_NAME
    try:
        found = resolve_sql_server(ws, files)
    except ValueError:
        return {}, ws.claude_code_mcp.get("mysql", "")
    if not found:
        return {}, ""
    name, cfg, _ = found
    settings = {k: v for k, v in (getattr(ws, "sql", None) or {}).items() if k != "server"}
    spec = json.dumps({"name": name, "config": cfg, "settings": settings})
    return {PROXY_NAME: {"command": sys.executable, "args": ["-m", "dev_workflows.jira_implement.sql_mcp"],
                         "env": {ENV_VAR: spec}}}, name


def sql_gateway(ws, files: list[Path] | None = None):
    """A SqlGateway in this process (doctor), or None when no MySQL MCP is configured. Raises like resolve_sql_server."""
    from .sql_mcp import SqlGateway
    found = resolve_sql_server(ws, files)
    if not found:
        return None
    settings = {k: v for k, v in (getattr(ws, "sql", None) or {}).items() if k != "server"}
    return SqlGateway(found[0], found[1], settings)
