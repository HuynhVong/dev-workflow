"""Minimal synchronous MCP tool client. Each call opens a short session (simple, robust across resumes)."""
import asyncio
import json
import os
import tempfile
from typing import Any, Protocol


class TransientToolError(RuntimeError):
    """Network/timeout/rate-limit style failure: safe to retry."""


class McpToolError(RuntimeError):
    """The server answered, but the tool call failed (the server's own message is the error text)."""


class McpTools(Protocol):
    def list_tools(self) -> list[str]: ...
    def call(self, tool: str, args: dict[str, Any]) -> Any: ...


def _http_transport(url: str, headers: dict | None):
    """Streamable HTTP for both mcp 1.x (`streamablehttp_client(url, headers=)`) and 2.x (`streamable_http_client`)."""
    from mcp.client import streamable_http as sh
    if hasattr(sh, "streamablehttp_client"):
        return sh.streamablehttp_client(url, headers=headers)
    return sh.streamable_http_client(url, http_client=sh.create_mcp_http_client(headers=headers))


def root_cause(e: BaseException) -> BaseException:
    """The real error inside the anyio task groups the MCP transports nest around a session: their
    "unhandled errors in a TaskGroup (1 sub-exception)" says nothing about what went wrong."""
    while isinstance(e, BaseExceptionGroup) and e.exceptions:
        inner = [x for x in e.exceptions if not isinstance(x, asyncio.CancelledError)] or list(e.exceptions)
        e = inner[0]
    return e


def describe(e: BaseException) -> str:
    text = str(e).strip()
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


class StdioOrHttpMcp:
    """config: {"command": "...", "args": [...], "env": {...}} for stdio, or {"url": "...", "type": "http"|"sse"}."""

    def __init__(self, name: str, config: dict):
        self.name, self.config = name, config
        self._stderr = ""  # the tail of a stdio server's stderr from the last failed session

    async def _with_session(self, fn):
        """Runs fn(session) and hands back ("ok", value) or ("error", exc). A tool's own error is returned rather than
        raised inside the session, so the transport's task groups don't wrap it on the way out."""
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        async def run(s):
            await s.initialize()
            try:
                return "ok", await fn(s)
            except McpToolError as e:
                return "error", e

        if "url" in self.config:
            if self.config.get("type") == "sse":
                from mcp.client.sse import sse_client
                transport = sse_client(self.config["url"], headers=self.config.get("headers"))
            else:
                transport = _http_transport(self.config["url"], self.config.get("headers"))
            async with transport as streams:
                async with ClientSession(streams[0], streams[1]) as s:
                    return await run(s)
        # Inherit the whole environment as Claude Code does (the MCP SDK passes only PATH, HOME and a few others),
        # so a server that reads JIRA_URL / JIRA_API_TOKEN from the shell sees them; the config's env wins.
        env = {**os.environ, **(self.config.get("env") or {})}
        params = StdioServerParameters(command=self.config["command"], args=self.config.get("args", []), env=env)
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace") as errlog:
            try:
                async with stdio_client(params, errlog=errlog) as (r, w):
                    async with ClientSession(r, w) as s:
                        return await run(s)
            except BaseException:
                errlog.seek(0)
                self._stderr = errlog.read()[-800:].strip()
                raise

    def list_tools(self) -> list[str]:
        async def go(s):
            return [t.name for t in (await s.list_tools()).tools]
        return self._run(go)

    def call(self, tool: str, args: dict[str, Any]) -> Any:
        async def go(s):
            res = await s.call_tool(tool, args)
            texts = [c.text for c in res.content if getattr(c, "type", "") == "text"]
            if getattr(res, "is_error", None) or getattr(res, "isError", None):
                raise McpToolError(f"{self.name}.{tool} failed: {' '.join(texts)[:500] or 'no error text from the server'}")
            joined = "\n".join(texts)
            try:
                return json.loads(joined)
            except (json.JSONDecodeError, TypeError):
                return joined
        return self._run(go)

    def _run(self, coro_fn):
        self._stderr = ""
        try:
            status, value = asyncio.run(self._with_session(coro_fn))
        except BaseException as e:  # noqa: BLE001 - unwrap task groups; KeyboardInterrupt/SystemExit pass through
            cause = root_cause(e)
            if isinstance(cause, (KeyboardInterrupt, SystemExit)):
                raise cause from None
            msg = f"MCP server '{self.name}': {describe(cause)}"
            if self._stderr:
                msg += f" (server stderr: {self._stderr})"
            if isinstance(cause, (TimeoutError, ConnectionError, OSError)):
                raise TransientToolError(msg) from cause
            raise RuntimeError(msg) from cause
        if status == "error":
            raise value
        return value
