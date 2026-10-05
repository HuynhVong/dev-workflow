"""Minimal synchronous MCP tool client. Each call opens a short session (simple, robust across resumes)."""
import asyncio
import json
from typing import Any, Protocol


class TransientToolError(RuntimeError):
    """Network/timeout/rate-limit style failure: safe to retry."""


class McpTools(Protocol):
    def list_tools(self) -> list[str]: ...
    def call(self, tool: str, args: dict[str, Any]) -> Any: ...


def _http_transport(url: str, headers: dict | None):
    """Streamable HTTP for both mcp 1.x (`streamablehttp_client(url, headers=)`) and 2.x (`streamable_http_client`)."""
    from mcp.client import streamable_http as sh
    if hasattr(sh, "streamablehttp_client"):
        return sh.streamablehttp_client(url, headers=headers)
    return sh.streamable_http_client(url, http_client=sh.create_mcp_http_client(headers=headers))


class StdioOrHttpMcp:
    """config: {"command": "...", "args": [...], "env": {...}} for stdio, or {"url": "..."} for streamable HTTP."""

    def __init__(self, name: str, config: dict):
        self.name, self.config = name, config

    async def _with_session(self, fn):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        if "url" in self.config:
            async with _http_transport(self.config["url"], self.config.get("headers")) as streams:
                async with ClientSession(streams[0], streams[1]) as s:
                    await s.initialize()
                    return await fn(s)
        params = StdioServerParameters(command=self.config["command"], args=self.config.get("args", []), env=self.config.get("env"))
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return await fn(s)

    def list_tools(self) -> list[str]:
        async def go(s):
            return [t.name for t in (await s.list_tools()).tools]
        return self._run(go)

    def call(self, tool: str, args: dict[str, Any]) -> Any:
        async def go(s):
            res = await s.call_tool(tool, args)
            texts = [c.text for c in res.content if getattr(c, "type", "") == "text"]
            if getattr(res, "is_error", None) or getattr(res, "isError", None):
                raise RuntimeError(f"{self.name}.{tool} failed: {' '.join(texts)[:500]}")
            joined = "\n".join(texts)
            try:
                return json.loads(joined)
            except (json.JSONDecodeError, TypeError):
                return joined
        return self._run(go)

    def _run(self, coro_fn):
        try:
            return asyncio.run(self._with_session(coro_fn))
        except (TimeoutError, ConnectionError, OSError) as e:
            raise TransientToolError(str(e)) from e
