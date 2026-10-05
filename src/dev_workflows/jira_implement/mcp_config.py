"""Which MCP server to use for Jira: workspace.yaml first, else the one the developer connected to Claude Code.

Claude Code keeps MCP servers in ~/.claude.json (user scope at the top level, local scope under `projects`),
~/.claude/settings.json and a project's .mcp.json. They are only read here, never changed.
"""
import json
import os
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


def _normalize(cfg: dict) -> dict:
    """Claude Code's {"type": "http", "url": ..., "headers": ...} / {"command", "args", "env"} as our MCP client takes it."""
    out = {k: v for k, v in cfg.items() if k in ("command", "args", "env", "url", "headers")}
    return {k: os.path.expandvars(v) if isinstance(v, str) else v for k, v in out.items()}


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
