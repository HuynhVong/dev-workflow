"""Jira and Confluence straight to a Claude Code MCP server's own process, with Claude Code as the per-call fallback.

When the server chosen under `claude_code_mcp:` (e.g. pal-jira) has a local config in Claude Code's files, devflow
starts it itself and keeps one session open, so a call takes about a second instead of a fresh `claude -p` (8-10 s).
Each devflow operation is matched to the server's own tool (same rules as claude_code_mcp.match_tool) and its
arguments to that tool's schema (issue_key -> issueKey / issueIdOrKey, comment -> body, ...). When an operation has
no tool, its required arguments can't be matched, or the server rejects the arguments, that call goes through
Claude Code (ClaudeCodeMcp), where the model maps them. Confluence stays read-only either way.
"""
import json
import re
from typing import Any, Callable

from .claude_code_mcp import SKIPPABLE, _norm, _unwrap, match_tool
from .confluence import is_write_tool
from .mcp_client import McpToolError, TransientToolError
from .sql_mcp import ConnectionLost, Upstream

# devflow argument (normalised) -> other names servers use for it, tried after an exact or normalised match.
ARG_SYNONYMS = {
    "issuekey": ("issueidorkey", "key", "issue", "issueid", "ticket", "ticketkey", "idorkey"),
    "jql": ("query", "jqlquery", "q"),
    "limit": ("maxresults", "max", "size", "pagesize", "count"),
    "startat": ("start", "offset", "startindex"),
    "commentlimit": ("maxcomments",),
    "comment": ("body", "text", "commentbody", "content", "message"),
    "commentid": ("id",),
    "transitionid": ("transition", "id"),
    "useridentifier": ("user", "accountid", "email", "username", "query"),
    "issueidsorkeys": ("issuekeys", "keys", "issues", "issueids"),
    "targetdir": ("directory", "dir", "path", "outputdir", "downloaddir"),
    "pageid": ("id", "contentid", "page"),
    "parentid": ("pageid", "id", "contentid", "parent"),
    "query": ("cql", "q", "search", "text"),
}
# Operations that only read: a lost connection may be retried and then sent to Claude Code. A write is not resent
# here (it may have gone through); the graph's retry re-runs the idempotent step instead.
READ_OPS = {"get_issue", "get_transitions", "search", "batch_changelogs", "get_user_profile", "download_attachments",
            "get_page", "get_page_children", "get_attachments"}
_ARG_ERROR = re.compile(r"validation error|invalid (?:argument|param|input)|required (?:property|field|argument)|"
                        r"unexpected keyword|missing \d* ?required|is a required|field required|input should be", re.I)


def map_args(args: dict[str, Any], schema: dict) -> tuple[dict, list[str]] | None:
    """(arguments in the tool's own names, devflow arguments it has no place for), or None when a required argument
    of the tool would stay empty. Exact and normalised names are matched before synonyms, so commentId beats id."""
    props = (schema or {}).get("properties") or {}
    if not props:
        return (dict(args), []) if not (schema or {}).get("required") else None
    by_norm = {_norm(p): p for p in props}
    mapped, used, left = {}, set(), []
    for k, v in args.items():
        p = k if k in props else by_norm.get(_norm(k))
        if p and p not in used:
            mapped[p] = _coerce(v, props[p])
            used.add(p)
        else:
            left.append(k)
    dropped = []
    for k in left:
        p = next((by_norm[s] for s in ARG_SYNONYMS.get(_norm(k), ()) if s in by_norm and by_norm[s] not in used), None)
        if p:
            mapped[p] = _coerce(args[k], props[p])
            used.add(p)
        else:
            dropped.append(k)
    if any(r not in mapped for r in (schema or {}).get("required") or []):
        return None
    return mapped, dropped


def _types(prop: dict) -> set[str]:
    """JSON-schema types a property takes, including `anyOf` / `oneOf` variants (e.g. list[str] | None)."""
    prop = prop or {}
    kinds = prop.get("type") or []
    kinds = {kinds} if isinstance(kinds, str) else set(kinds)
    for alt in (prop.get("anyOf") or []) + (prop.get("oneOf") or []):
        kinds |= _types(alt)
    return kinds


def _coerce(value, prop: dict):
    kinds = _types(prop)
    if not kinds:
        return value
    if isinstance(value, str) and "string" not in kinds:
        if "array" in kinds:
            return [x.strip() for x in value.split(",") if x.strip()]
        if "integer" in kinds and value.isdigit():
            return int(value)
    if isinstance(value, list) and "array" not in kinds and "string" in kinds:
        return ",".join(str(x) for x in value)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and kinds == {"string"}:
        return str(value)
    return value


class DirectMcp:
    """McpTools over one server started by devflow from Claude Code's config, presenting devflow's configured tool
    names (like ClaudeCodeMcp). `fallback` builds the ClaudeCodeMcp used for calls that can't go direct."""

    def __init__(self, server: str, config: dict, tools: dict[str, str], equivalents: dict[str, set[str]],
                 read_only: bool = False, fallback: Callable[[], Any] | None = None, upstream: Upstream | None = None,
                 timeout_s: float = 120.0):
        self.server, self.tools, self.equivalents, self.read_only = server, dict(tools), equivalents, read_only
        self.up = upstream or Upstream(server, config, timeout_s)
        self._fallback_factory, self._fallback = fallback, None
        self._schemas: dict[str, dict] | None = None
        self.start_error = ""
        self.via_fallback: dict[str, str] = {}  # op -> why it goes through Claude Code
        self.name = f"{server} (direct)"

    # --- discovery ---------------------------------------------------------------------------------------------------
    def schemas(self) -> dict[str, dict]:
        """name -> input schema of the server's tools ({} when it could not be started; then every call falls back)."""
        if self._schemas is None:
            try:
                listed = self.up.list_tools()
            except ConnectionLost:
                try:
                    listed = self.up.list_tools()  # one reconnect
                except ConnectionLost as e:
                    self.start_error = str(e)
                    return {}
            self._schemas = {t["name"]: t["schema"] for t in listed if not (self.read_only and is_write_tool(t["name"]))}
        return self._schemas

    def resolve(self, op: str) -> str | None:
        return match_tool(op, self.tools.get(op, ""), list(self.schemas()), self.equivalents)

    def fallback(self):
        if self._fallback is None:
            if self._fallback_factory is None:
                raise RuntimeError(f"'{self.server}' can't do this directly and has no Claude Code fallback")
            self._fallback = self._fallback_factory()
        return self._fallback

    def route(self, op: str, sample: dict | None = None) -> tuple[str, str]:
        """("direct", tool) or ("claude_code", why) for one operation. `sample` = the arguments devflow sends."""
        if op in self.via_fallback:
            return "claude_code", self.via_fallback[op]
        if not self.schemas():
            return "claude_code", f"could not start {self.server}: {self.start_error[:200]}"
        real = self.resolve(op)
        if real is None:
            return "claude_code", "no matching tool"
        if sample is not None and map_args(sample, self._schemas[real]) is None:
            return "claude_code", f"{real} needs arguments devflow can't fill"
        return "direct", real

    def list_tools(self) -> list[str]:
        """devflow's configured names this server can serve, directly or through Claude Code."""
        names = [n for op, n in self.tools.items() if n and (op in SKIPPABLE or self.resolve(op))]
        if not self.schemas() and self._fallback_factory is not None:
            return self.fallback().list_tools()
        return names

    # --- calls -------------------------------------------------------------------------------------------------------
    def call(self, tool: str, args: dict[str, Any]) -> Any:
        op = next((k for k, v in self.tools.items() if v == tool), None)
        if op is None:
            raise RuntimeError(f"unknown tool: {tool}")
        kind, real = self.route(op)
        mapped = map_args(args, self._schemas[real]) if kind == "direct" else None
        if kind == "direct" and mapped is None:
            kind, self.via_fallback[op] = "claude_code", f"{real} needs arguments devflow can't fill"
        if kind != "direct":
            if op in SKIPPABLE and real == "no matching tool":
                return {"skipped": f"'{self.server}' has no tool for {op}"}
            return self.fallback().call(tool, args)
        if self.read_only and is_write_tool(real):
            raise PermissionError(f"refusing write tool {real} on read-only server '{self.server}'")
        attempts = 2 if op in READ_OPS else 1
        for attempt in range(attempts):
            try:
                text, is_error = self.up.call(real, mapped[0])
                break
            except ConnectionLost as e:
                if attempt + 1 < attempts:
                    continue
                if op in READ_OPS and self._fallback_factory is not None:
                    return self.fallback().call(tool, args)
                raise TransientToolError(f"{self.server}.{real}: {e}") from e
        if is_error:
            if _ARG_ERROR.search(text or "") and self._fallback_factory is not None:
                self.via_fallback[op] = f"{real} rejected the arguments: {text[:150]}"
                return self.fallback().call(tool, args)  # nothing was done, so Claude Code can map them instead
            raise McpToolError(f"{self.server}.{real} failed: {text[:500] or 'no error text from the server'}")
        text = _unwrap(text)
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text

    def close(self) -> None:
        self.up.close()
