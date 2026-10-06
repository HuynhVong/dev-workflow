"""A tiny stdio MCP server for the client tests: `python fake_mcp_server.py` (or `... crash` to die at start-up)."""
import os
import sys

if sys.argv[1:] == ["crash"]:
    print("JIRA_URL is not set", file=sys.stderr)
    sys.exit(1)

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer

server = MCPServer("fake-jira")


@server.tool()
def jira_get_issue(issue_key: str, fields: str = "", comment_limit: int = 10) -> str:
    if issue_key == "NOPE-1":
        raise ValueError(f"Issue {issue_key} does not exist or you do not have permission to see it")
    return '{"key": "%s", "fields": {"status": {"name": "In Progress"}}}' % issue_key


@server.tool()
def env_var(name: str) -> str:
    return os.environ.get(name, "<unset>")


server.run()
