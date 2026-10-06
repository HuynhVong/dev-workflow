"""A Jira MCP with its own tool and argument names (like pal-jira), for the direct-client tests. `... die-next`
behaviour comes from FAKE_PAL_STATE ({"die": N}); each call is logged as "<pid> <tool>" to FAKE_PAL_LOG."""
import json
import os

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer

server = MCPServer("fake-pal")


def log(tool: str) -> None:
    with open(os.environ["FAKE_PAL_LOG"], "a") as f:
        f.write(f"{os.getpid()} {tool}\n")
    state_file = os.environ.get("FAKE_PAL_STATE")
    if state_file and os.path.exists(state_file):
        state = json.load(open(state_file))
        if state.get("die", 0) > 0:
            state["die"] -= 1
            json.dump(state, open(state_file, "w"))
            os._exit(1)


@server.tool()
def get_issue(issueKey: str, fields: list[str] | None = None) -> str:
    log("get_issue")
    return json.dumps({"key": issueKey, "fields": {"status": {"name": "In Progress"}, "asked": fields}})


@server.tool()
def search_issues(jql: str, maxResults: int = 50) -> str:
    log("search_issues")
    return json.dumps({"issues": [{"key": "AQS-1"}], "total": 1, "jql": jql, "max": maxResults})


@server.tool()
def add_comment(issueKey: str, body: str) -> str:
    log("add_comment")
    return json.dumps({"id": "c1", "body": body})


@server.tool()
def transition(issueKey: str, transitionId: int) -> str:
    log("transition")
    return json.dumps({"ok": True, "to": transitionId})


@server.tool()
def update_comment(commentRef: str, newText: str) -> str:
    log("update_comment")
    return "{}"


@server.tool()
def get_page(pageId: str) -> str:
    log("get_page")
    return json.dumps({"id": pageId, "title": "Spec"})


@server.tool()
def create_page(title: str) -> str:
    log("create_page")
    return "{}"


server.run()
