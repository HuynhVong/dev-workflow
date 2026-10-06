"""devflow-sql: a small read-only MCP server in front of the developer's MySQL MCP, with reconnect and retry.

The coding agents never get the MySQL MCP itself. They get this proxy (`python -m dev_workflows.jira_implement.sql_mcp`,
started by Claude Code like any stdio server), which keeps one session to the real server open and:

- lets through only single read statements (SELECT, WITH ... SELECT, SHOW, DESCRIBE, EXPLAIN), so retrying is always
  safe and nothing on the dev DB is ever changed; scripts that change data are written as files for the developer;
- gives every upstream call a timeout, and when the server sleeps, drops the MySQL connection ("gone away", 2006,
  2013...), hangs or dies, tears the session down (a stdio server process is stopped) and retries the same statement
  on a fresh one, waiting 1 s, 2 s, 4 s...;
- pings with `SELECT 1` first when the session sat idle for a while;
- after the last retry, answers with a clear "unavailable" error so the agent carries on from the code.

Config comes from the DEVFLOW_SQL_UPSTREAM environment variable (see mcp_config.sql_servers).
"""
import asyncio
import concurrent.futures
import json
import os
import re
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from typing import Callable

try:
    from mcp.shared.exceptions import MCPError as McpError
except ImportError:  # mcp 1.x
    from mcp.shared.exceptions import McpError

from .mcp_client import McpToolError, TransientToolError, _http_transport, describe, root_cause

PROXY_NAME = "devflow-sql"
ENV_VAR = "DEVFLOW_SQL_UPSTREAM"
NOTE_TAG = f"[{PROXY_NAME}:"
DEFAULTS = {"query_tool": "", "timeout_s": 30.0, "max_retries": 3, "idle_ping_s": 240.0, "max_rows": 200,
            "max_chars": 40000, "deny_hosts": []}


# --- read-only guard -----------------------------------------------------------------------------------------------
def _code_only(sql: str) -> str | None:
    """The statement with comments removed and every quoted string or `identifier` replaced by a placeholder, so
    keywords inside them don't count. None when a quote or comment is never closed."""
    out, i, n = [], 0, len(sql)
    while i < n:
        c = sql[i]
        if c in "'\"`":
            j = i + 1
            while j < n:
                if sql[j] == "\\" and c != "`":
                    j += 2
                    continue
                if sql[j] == c:
                    if j + 1 < n and sql[j + 1] == c:  # '' inside a string
                        j += 2
                        continue
                    break
                j += 1
            if j >= n:
                return None
            out.append(" `x` " if c == "`" else " 's' ")
            i = j + 1
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            if j < 0:
                return None
            out.append(" ")
            i = j + 2
        elif c == "#" or (sql.startswith("--", i) and (i + 2 >= n or sql[i + 2] in " \t\r\n")):
            j = sql.find("\n", i)
            i = n if j < 0 else j
        else:
            out.append(c)
            i += 1
    return "".join(out)


READ_VERBS = ("select", "with", "show", "describe", "desc", "explain")
# Inside a SELECT these are the only ways to change or lock anything (multiple statements are refused separately).
_SELECT_DENY = re.compile(r"\b(update|delete|into|lock|share)\b|\binsert\b(?!\s*\()|\breplace\b(?!\s*\()|:=", re.I)
_SIDE_EFFECT_FN = re.compile(r"\b(sleep|benchmark|get_lock|release_lock|release_all_locks|load_file|sys_exec|sys_eval)\s*\(", re.I)


def read_only_problem(sql: str) -> str:
    """'' when `sql` is one read-only statement, else why it is refused."""
    if "/*!" in (sql or ""):
        return "MySQL executable comments (/*! ... */) are not allowed"
    code = _code_only(sql or "")
    if code is None:
        return "unclosed quote or comment"
    code = code.strip().rstrip(";").strip()
    if not code:
        return "empty statement"
    if ";" in code:
        return "only one statement per call"
    first = (re.match(r"\(*\s*([A-Za-z]+)", code) or [None, ""])[1].lower()
    if first not in READ_VERBS:
        return (f"{first.upper() or 'this statement'} is not allowed: only read statements (SELECT, SHOW, DESCRIBE, "
                "EXPLAIN) run here. Write changes as a script file for the developer to review and run.")
    if first == "show":
        return ""
    if first in ("explain", "describe", "desc"):
        rest = re.sub(r"^\s*(format\s*=\s*\w+|extended|partitions)\s*", "", code.split(None, 1)[1] if " " in code else "", flags=re.I)
        if re.match(r"analyze\b", rest, re.I):
            return "EXPLAIN ANALYZE runs the statement; use plain EXPLAIN"
        if re.match(r"[\w$.`]+(\s+\S+)?\s*$", rest) and not re.match(r"(select|with|update|delete|insert|replace|for)\b", rest, re.I):
            return ""  # DESCRIBE table [column]
        return read_only_problem(rest) if re.match(r"\(*\s*(select|with)\b", rest, re.I) else "EXPLAIN only for SELECT statements"
    m = _SELECT_DENY.search(code) or _SIDE_EFFECT_FN.search(code)
    if m:
        return f"`{m.group(0).strip(' (').upper()}` is not allowed in a read-only query"
    return ""


def with_limit(sql: str, max_rows: int) -> str:
    """A SELECT without any LIMIT gets one, so a big table can't flood the agent."""
    code = (_code_only(sql) or "").strip().lower()
    if max_rows and re.match(r"\(*\s*(select|with)\b", code) and not re.search(r"\blimit\b", code):
        return f"{sql.rstrip().rstrip(';').rstrip()}\nLIMIT {int(max_rows)}"
    return sql


# --- what counts as a lost connection ------------------------------------------------------------------------------
_CONN_RE = re.compile(
    r"gone away|lost connection|can'?t connect|cannot connect|unable to connect|connection (?:was )?(?:refused|reset|"
    r"closed|is closed|timed out|lost|aborted)|not connected|broken pipe|server has closed|pool is closed|"
    r"connection not available|closed state|ECONNREFUSED|ECONNRESET|ETIMEDOUT|EPIPE|PROTOCOL_CONNECTION_LOST|"
    r"\b(?:2002|2003|2006|2013|2055|4031)\b", re.I)


def is_connection_error(text: str) -> bool:
    return bool(_CONN_RE.search(text or ""))


class ConnectionLost(TransientToolError):
    """The upstream session broke (timeout, process gone, MySQL connection dropped): reconnecting may fix it."""


class SqlUnavailable(RuntimeError):
    """Still no database after every retry."""


# --- one long-lived upstream session -------------------------------------------------------------------------------
class Upstream:
    """One MCP session to the real server, owned by a background event loop so the session is opened and closed in
    the same task (the MCP transports require it). Opened on the first call; after a failure the next call opens a
    new one. Thread-safe, one call at a time."""

    def __init__(self, name: str, config: dict, timeout_s: float = 30.0):
        self.name, self.config, self.timeout_s = name, config, float(timeout_s)
        self.connects = 0
        self.connected = False
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._stderr = ""

    # sync API
    def list_tools(self) -> list[dict]:
        return self._submit("list")

    def call(self, tool: str, args: dict) -> tuple[str, bool]:
        """(text, is_error). A lost connection raises ConnectionLost and the session is closed."""
        return self._submit("call", tool, args)

    def drop(self) -> None:
        """Close the session on purpose (doctor's reconnect check); the next call opens a new one."""
        if self._loop is not None:
            self._submit("drop")

    def close(self) -> None:
        if self._loop is not None:
            self._submit("close")
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._loop = None

    def _submit(self, kind: str, *args):
        with self._lock:
            self._start()
            fut: concurrent.futures.Future = concurrent.futures.Future()
            self._loop.call_soon_threadsafe(self._queue.put_nowait, (kind, args, fut))
            try:
                return fut.result(timeout=self.timeout_s * 2 + 30)
            except concurrent.futures.TimeoutError:
                raise ConnectionLost(f"MCP server '{self.name}' did not answer") from None

    def _start(self) -> None:
        if self._loop is not None:
            return
        ready = threading.Event()

        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop, self._queue = loop, asyncio.Queue()
            loop.create_task(self._owner())
            ready.set()
            loop.run_forever()

        threading.Thread(target=run, daemon=True, name=f"{PROXY_NAME}-upstream").start()
        ready.wait()

    @asynccontextmanager
    async def _session(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        if "url" in self.config:
            if self.config.get("type") == "sse":
                from mcp.client.sse import sse_client
                transport = sse_client(self.config["url"], headers=self.config.get("headers"))
            else:
                transport = _http_transport(self.config["url"], self.config.get("headers"))
            async with transport as streams:
                async with ClientSession(streams[0], streams[1]) as s:
                    async with asyncio.timeout(self.timeout_s):
                        await s.initialize()
                    yield s
            return
        # The whole environment, as Claude Code passes it (DB host/user/password often come from the shell).
        env = {**os.environ, **(self.config.get("env") or {})}
        params = StdioServerParameters(command=self.config["command"], args=self.config.get("args", []), env=env)
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as errlog:
            try:
                async with stdio_client(params, errlog=errlog) as (r, w):
                    async with ClientSession(r, w) as s:
                        async with asyncio.timeout(self.timeout_s):
                            await s.initialize()
                        yield s
            except BaseException:
                errlog.seek(0)
                self._stderr = errlog.read()[-500:].strip()
                raise

    async def _owner(self):
        pending = None
        while True:
            if pending is None:
                pending = await self._queue.get()
            if pending[0] in ("drop", "close"):
                pending[2].set_result(None)
                if pending[0] == "close":
                    return
                pending = None
                continue
            stop = None
            self._stderr = ""
            try:
                async with self._session() as s:
                    self.connects += 1
                    self.connected = True
                    while True:
                        kind, args, fut = pending
                        if kind in ("drop", "close"):
                            stop = pending
                            break
                        fut.set_result(await self._handle(s, kind, args))
                        pending = await self._queue.get()
            except BaseException as e:  # noqa: BLE001 - every failure closes the session and is reported to the caller
                cause = root_cause(e)
                msg = str(cause) if isinstance(cause, ConnectionLost) else describe(cause)
                if isinstance(cause, TimeoutError):
                    msg = f"timed out after {self.timeout_s:g}s"
                if self._stderr and not isinstance(cause, ConnectionLost):
                    msg += f" (server stderr: {self._stderr})"
                if stop is None and pending is not None and not pending[2].done():
                    # A bug on our side is not a lost connection: reconnecting would not help.
                    kind = RuntimeError if isinstance(cause, (AttributeError, TypeError, KeyError, NameError)) else ConnectionLost
                    pending[2].set_exception(kind(f"MCP server '{self.name}': {msg}"))
                pending = None
            finally:
                self.connected = False
            if stop is not None:
                if not stop[2].done():
                    stop[2].set_result(None)
                if stop[0] == "close":
                    return
                pending = None

    async def _handle(self, s, kind: str, args: tuple):
        async with asyncio.timeout(self.timeout_s):
            if kind == "list":
                return [{"name": t.name, "schema": getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {}}
                        for t in (await s.list_tools()).tools]
            tool, arguments = args
            try:
                res = await s.call_tool(tool, arguments)
            except McpError as e:  # the server refused the request itself (bad arguments...): not a connection problem
                if is_connection_error(str(e)):
                    raise ConnectionLost(str(e)[:300]) from e
                return str(e), True
        text = "\n".join(c.text for c in res.content if getattr(c, "type", "") == "text")
        is_error = bool(getattr(res, "is_error", None) or getattr(res, "isError", None))
        if is_error and is_connection_error(text):
            raise ConnectionLost(text[:300])  # the server is up but its MySQL connection is not: start it afresh
        return text, is_error


# --- the gateway: guard, retry policy, idle ping -------------------------------------------------------------------
QUERY_TOOLS = ("mysqlquery", "executesql", "query", "runsql", "executequery", "sqlquery", "mysqlexecutesql",
               "readquery", "mysqlexecute", "runquery", "execute")
QUERY_ARGS = ("sql", "query", "statement", "sql_query", "sqlquery")


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def pick_query_tool(tools: list[dict], wanted: str = "") -> tuple[str, str] | None:
    """(tool, argument name) of the server's run-a-query tool: the configured one, else a known name, else the first
    tool whose name says query or sql."""
    by = {t["name"]: t for t in tools}
    if wanted:
        tool = by.get(wanted)
    else:
        tool = next((by[n] for key in QUERY_TOOLS for n in by if _norm(n) == key), None) or \
            next((t for t in tools if re.search(r"query|sql", t["name"], re.I)), None)
    if tool is None:
        return None
    props = (tool.get("schema") or {}).get("properties") or {}
    arg = next((p for p in QUERY_ARGS if p in props), None) or next(iter(props), "sql")
    return tool["name"], arg


_IDENT = re.compile(r"^[A-Za-z0-9_$]{1,64}$")


class SqlGateway:
    def __init__(self, name: str, config: dict, settings: dict | None = None, upstream: Upstream | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self.name = name
        self.settings = {**DEFAULTS, **{k: v for k, v in (settings or {}).items() if v not in (None, "")}}
        self.up = upstream or Upstream(name, config, self.settings["timeout_s"])
        self.sleep, self.clock = sleep, clock
        self.reconnects = 0
        self.last_ok: float | None = None
        self.notes: list[str] = []
        self._tool: tuple[str, str] | None = None

    def _retrying(self, fn):
        attempt, delay = 0, 1.0
        while True:
            try:
                value = fn()
                self.last_ok = self.clock()
                return value
            except ConnectionLost as e:
                if attempt >= int(self.settings["max_retries"]):
                    raise SqlUnavailable(f"MySQL MCP '{self.name}' unavailable after {attempt} reconnects: {e}. "
                                         "Carry on from the code and say the live schema was not checked.") from None
                attempt += 1
                self.reconnects += 1
                self.notes.append(f"reconnected after: {str(e)[:200]}")
                self.sleep(delay)
                delay *= 2

    def query_tool(self) -> tuple[str, str]:
        if self._tool is None:
            tools = self._retrying(self.up.list_tools)
            found = pick_query_tool(tools, self.settings["query_tool"])
            if found is None:
                names = ", ".join(t["name"] for t in tools) or "none"
                raise RuntimeError(f"MySQL MCP '{self.name}' has no query tool (it has: {names}); set sql.query_tool "
                                   "in workspace.yaml")
            self._tool = found
        return self._tool

    def _run(self, sql: str) -> str:
        tool, arg = self.query_tool()
        idle = self.settings["idle_ping_s"]
        if self.last_ok is not None and idle and self.clock() - self.last_ok > float(idle):
            try:  # a session that sat idle may be stale: find out with a cheap query, not the real one
                self.up.call(tool, {arg: "SELECT 1"})
            except ConnectionLost as e:
                self.reconnects += 1
                self.notes.append(f"idle connection was stale, reconnected ({str(e)[:150]})")
        text, is_error = self._retrying(lambda: self.up.call(tool, {arg: sql}))
        if is_error:
            raise McpToolError(f"MySQL: {text[:1000] or 'error with no message'}")
        return text

    def query(self, sql: str) -> str:
        problem = read_only_problem(sql)
        if problem:
            raise PermissionError(f"refused by {PROXY_NAME} (read-only): {problem}")
        text = self._run(with_limit(sql.strip(), int(self.settings["max_rows"])))
        limit = int(self.settings["max_chars"])
        return text if len(text) <= limit else text[:limit] + f"\n... (cut at {limit} characters; add a LIMIT or select fewer columns)"

    def schema(self, table: str = "", database: str = "") -> str:
        for v in (table, database):
            if v and not _IDENT.match(v):
                raise ValueError(f"not a plain table or database name: {v!r}")
        if not table:
            return self.query(f"SHOW TABLES FROM `{database}`" if database else "SHOW TABLES")
        return self.query(f"SHOW CREATE TABLE {f'`{database}`.' if database else ''}`{table}`")

    def status(self) -> dict:
        try:
            self._run("SELECT 1")
            ok, error = True, ""
        except Exception as e:  # noqa: BLE001
            ok, error = False, str(e)[:300]
        return {"server": self.name, "reachable": ok, "reconnects": self.reconnects, "connects": self.up.connects,
                "query_tool": (self._tool or ("", ""))[0], **({"error": error} if error else {})}

    def take_notes(self) -> str:
        """The reconnects since the last call, as a line the agent (and devflow's activity log) can see."""
        notes, self.notes = self.notes, []
        return "".join(f"\n{NOTE_TAG} {n}]" for n in notes)


# --- the stdio server Claude Code starts ---------------------------------------------------------------------------
INSTRUCTIONS = ("Read-only access to the developer's dev MySQL database. Use it to check tables, columns, indexes and "
                "sample rows while writing endpoint logic or SQL scripts. Only one SELECT/SHOW/DESCRIBE/EXPLAIN per "
                "call; a SELECT without LIMIT gets one. Never try to change data: write migrations and data scripts "
                "as files for the developer to review and run.")


def build_server(gw: SqlGateway):
    try:
        from mcp.server.mcpserver import MCPServer
        from mcp.server.mcpserver.exceptions import ToolError
    except ImportError:  # mcp 1.x
        from mcp.server.fastmcp import FastMCP as MCPServer
        from mcp.server.fastmcp.exceptions import ToolError
    server = MCPServer(PROXY_NAME, instructions=INSTRUCTIONS)

    def answer(fn) -> str:
        """The result plus any reconnect notes; an error goes back to the agent with its real message."""
        try:
            return fn() + gw.take_notes()
        except Exception as e:  # noqa: BLE001
            raise ToolError(f"{e}{gw.take_notes()}") from e

    @server.tool(description="Run ONE read-only statement (SELECT, WITH ... SELECT, SHOW, DESCRIBE, EXPLAIN) on the "
                             "dev MySQL database and return the rows. Qualify tables as db.table when needed.")
    def sql_query(sql: str) -> str:
        return answer(lambda: gw.query(sql))

    @server.tool(description="List the tables (no table given), or show one table's CREATE TABLE statement: "
                             "columns, types, indexes and foreign keys.")
    def sql_schema(table: str = "", database: str = "") -> str:
        return answer(lambda: gw.schema(table, database))

    @server.tool(description="Is the dev database reachable right now, and how many reconnects so far.")
    def sql_status() -> str:
        return answer(lambda: json.dumps(gw.status()))

    return server


def gateway_from_env(env=os.environ) -> SqlGateway:
    spec = json.loads(env[ENV_VAR])
    return SqlGateway(spec["name"], spec["config"], spec.get("settings"))


def main() -> None:
    build_server(gateway_from_env()).run()


if __name__ == "__main__":
    main()
