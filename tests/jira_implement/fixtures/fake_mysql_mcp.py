"""A fake MySQL MCP server for the devflow-sql tests. Its behaviour comes from a JSON file named by FAKE_SQL_STATE:
{"gone_away": N, "hang": N, "die": N} make the next N queries fail that way; every query is logged as "<pid> <sql>"
to FAKE_SQL_LOG."""
import json
import os
import time

try:
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer
    from mcp.server.fastmcp.exceptions import ToolError

server = MCPServer("fake-mysql")
STATE = os.environ["FAKE_SQL_STATE"]


def take(kind: str) -> bool:
    with open(STATE) as f:
        state = json.load(f)
    if state.get(kind, 0) > 0:
        state[kind] -= 1
        with open(STATE, "w") as f:
            json.dump(state, f)
        return True
    return False


@server.tool()
def mysql_query(sql: str) -> str:
    with open(os.environ["FAKE_SQL_LOG"], "a") as f:
        f.write(f"{os.getpid()} {' '.join(sql.split())}\n")
    if take("die"):
        os._exit(1)
    if take("hang"):
        time.sleep(30)
    if take("gone_away"):
        raise ToolError("Error: MySQL server has gone away (2006)")
    if "SELEC " in sql:
        raise ToolError("You have an error in your SQL syntax near 'SELEC' (1064)")
    if sql.startswith("SHOW CREATE TABLE"):
        return "CREATE TABLE `orders` (`id` int NOT NULL, PRIMARY KEY (`id`))"
    return json.dumps([{"1": 1}])


server.run()
