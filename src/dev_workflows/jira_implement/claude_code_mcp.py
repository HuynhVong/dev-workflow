"""Jira and Confluence through the MCP servers of the developer's logged-in Claude Code (Pro/Max account connectors
such as "claude.ai Atlassian", plugin servers, or servers added with `claude mcp add`).

Chosen at setup under `claude_code_mcp:` in workspace.yaml; a blank entry falls back to devflow's own MCP client
(mcp_client.StdioOrHttpMcp). Each call runs headless Claude Code (`claude -p`, Haiku) allowed to use only the tools
that can do that one operation, and devflow reads the tool's raw result from Claude Code's event stream, so the model
never retypes ticket data. The connector's OAuth stays inside Claude Code: no Jira or Confluence token is needed.

This class speaks the same McpTools protocol as StdioOrHttpMcp and presents the tool names devflow is configured
with (jira_tools / confluence_tools, sooperset/mcp-atlassian names by default). Each one is mapped to the server's own
tool: the configured name if the server has it, else a known equivalent (e.g. getJiraIssue on the official Atlassian
connector). Confluence stays read-only: a Confluence write tool is never allowed, whatever is configured.
"""
import json
import os
import re
import subprocess
import tempfile
import time
from typing import Any, Callable

from .confluence import is_write_tool
from .mcp_client import TransientToolError

# Logical operation (the jira_tools / confluence_tools key) -> tool names that do it, normalised (lowercase, no _ or -).
JIRA_EQUIVALENTS = {
    "get_issue": {"jiragetissue", "getjiraissue"},
    "get_transitions": {"jiragettransitions", "gettransitionsforjiraissue"},
    "transition_issue": {"jiratransitionissue", "transitionjiraissue"},
    "add_comment": {"jiraaddcomment", "addcommenttojiraissue"},
    "edit_comment": {"jiraeditcomment", "editcommentonjiraissue", "updatejiraissuecomment"},
    "download_attachments": {"jiradownloadattachments"},
    "get_user_profile": {"jiragetuserprofile", "lookupjiraaccountid"},
    "search": {"jirasearch", "searchjiraissuesusingjql"},
    "batch_changelogs": {"jirabatchgetchangelogs"},
    "attach": {"jiraupdateissue"},
}
CONFLUENCE_EQUIVALENTS = {
    "get_page": {"confluencegetpage", "getconfluencepage"},
    "get_page_children": {"confluencegetpagechildren", "getconfluencepagedescendants"},
    "search": {"confluencesearch", "searchconfluenceusingcql"},
    "get_attachments": {"confluencegetattachments"},
}
# Word rules for servers with their own naming (e.g. `transition`, `update_comment`): op -> (words it must have,
# words that rule it out). Used only when exactly one tool fits.
WORD_RULES = {
    "get_issue": ({"issue"}, {"search", "create", "update", "delete", "transition", "comment", "link", "worklog"}, {"get", "read", "fetch"}),
    "get_transitions": ({"transitions"}, set(), set()),
    "transition_issue": ({"transition"}, {"get", "list", "fetch"}, set()),
    "add_comment": ({"comment"}, {"edit", "update", "delete", "get", "list"}, {"add", "create", "post"}),
    "edit_comment": ({"comment"}, {"add", "create", "delete", "get", "list"}, {"edit", "update", "modify"}),
    "search": ({"search"}, {"confluence", "user"}, set()),
}
# Lookups some servers need before the real call (the official Atlassian connector wants a cloudId).
HELPERS = {"getaccessibleatlassianresources", "atlassianuserinfo"}
# Operations that may quietly do nothing when the server has no tool for them (the caller copes with no result).
SKIPPABLE = {"download_attachments"}
DEFAULT_MODEL = "haiku"


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _words(name: str) -> set[str]:
    """`transitionJiraIssue` / `jira_transition-issue` -> {"transition", "jira", "issue"}."""
    return {w.lower() for w in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", name)}


def tool_prefix(server: str) -> str:
    """Claude Code names a server's tools mcp__<server>__<tool>, with characters outside [A-Za-z0-9_-] as `_`."""
    return f"mcp__{re.sub(r'[^A-Za-z0-9_-]', '_', server)}__"


def _env() -> dict:
    # Without the key Claude Code uses the logged-in account, which is what owns the connectors.
    return {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}


def read_init(binary: str = "claude", popen: Callable = subprocess.Popen, timeout_s: int = 90) -> dict:
    """Claude Code's startup event: {"mcp_servers": [{"name", "status"}...], "tools": ["mcp__x__y", ...]}.
    The process is stopped as soon as the event arrives, before any model call."""
    with tempfile.TemporaryDirectory(prefix="devflow-mcp-") as cwd:
        proc = popen([binary, "-p", "--output-format", "stream-json", "--verbose", "--tools", "", "--model", DEFAULT_MODEL,
                      "--no-session-persistence"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                     text=True, cwd=cwd, env=_env())
        try:
            proc.stdin.write("Reply OK.")
            proc.stdin.close()
            for line in proc.stdout:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "system" and event.get("subtype") == "init":
                    return event
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=timeout_s)
    err = (proc.stderr.read() if proc.stderr else "") or ""
    raise RuntimeError(f"Claude Code did not start (is `claude` installed and logged in?): {err.strip()[:300]}")


_INIT_CACHE: dict = {}


def cached_init(ttl_s: float = 60.0) -> dict:
    """read_init, reused for a minute so Jira and Confluence (often the same connector) start Claude Code once."""
    if not _INIT_CACHE or time.time() - _INIT_CACHE["at"] > ttl_s:
        _INIT_CACHE.update(at=time.time(), event=read_init())
    return _INIT_CACHE["event"]


def claude_code_servers(init: dict | None = None) -> dict[str, dict]:
    """name -> {"status": connected | needs-auth | failed | ..., "tools": [bare tool names]} for every MCP server
    the logged-in Claude Code sees."""
    init = init if init is not None else read_init()
    tools = init.get("tools") or []
    out = {}
    for s in init.get("mcp_servers") or []:
        name = s.get("name", "")
        prefix = tool_prefix(name)
        out[name] = {"status": s.get("status", ""), "tools": [t[len(prefix):] for t in tools if t.startswith(prefix)]}
    return out


class ClaudeCodeMcp:
    """McpTools over one MCP server of the logged-in Claude Code. `tools` is devflow's op -> tool-name map for this
    use (workspace jira_tools or confluence_tools); `read_only` blocks every write tool (Confluence)."""

    def __init__(self, server: str, tools: dict[str, str], equivalents: dict[str, set[str]], read_only: bool = False,
                 run: Callable = subprocess.run, init: Callable[[], dict] = cached_init, binary: str = "claude",
                 model: str = DEFAULT_MODEL, timeout_s: int = 300):
        self.server, self.tools, self.equivalents, self.read_only = server, dict(tools), equivalents, read_only
        self._run, self._init, self.binary, self.model, self.timeout_s = run, init, binary, model, timeout_s
        self._info: dict | None = None
        self._context: dict[str, str] = {}  # helper results (e.g. the cloudId list), reused so later calls skip them
        self.name = f"Claude Code: {server}"

    # --- discovery ---------------------------------------------------------------------------------------------------
    def info(self) -> dict:
        if self._info is None:
            servers = claude_code_servers(self._init())
            if self.server not in servers:
                known = ", ".join(sorted(servers)) or "none"
                raise RuntimeError(f"Claude Code has no MCP server named '{self.server}' (it has: {known}); "
                                   "fix claude_code_mcp in workspace.yaml or run `devflow setup` again")
            self._info = servers[self.server]
        return self._info

    def status(self) -> str:
        return self.info()["status"]

    def _server_tools(self) -> list[str]:
        info = self.info()
        if info["status"] != "connected":
            hint = "sign in: run `claude`, then `/mcp`" if info["status"] == "needs-auth" else "check it with `claude mcp list`"
            raise RuntimeError(f"Claude Code MCP '{self.server}' is {info['status']} ({hint})")
        return [t for t in info["tools"] if not (self.read_only and is_write_tool(t))]

    def resolve(self, op: str) -> str | None:
        """The server's tool for one devflow operation, or None when it has none."""
        available = self._server_tools()
        wanted = self.tools.get(op, "")
        if wanted in available:
            return wanted
        names = {_norm(wanted), _norm(op)} | self.equivalents.get(op, set())
        found = next((t for t in available if _norm(t) in names), None)
        if found or op not in WORD_RULES:
            return found
        need, never, one_of = WORD_RULES[op]
        fits = [t for t in available
                if need <= _words(t) and not never & _words(t) and (not one_of or one_of & _words(t))]
        return fits[0] if len(fits) == 1 else None

    def related(self, op: str) -> list[str]:
        """The server's tools that share a key word with `op` (shown by doctor when it can't pick one)."""
        key = (WORD_RULES.get(op, ({op.split("_")[-1]}, set(), set()))[0]) or {op}
        stems = {w.rstrip("s") for w in key}
        return [t for t in self._server_tools() if {w.rstrip("s") for w in _words(t)} & stems]

    def list_tools(self) -> list[str]:
        """devflow's configured names this server can serve (so JiraGateway's missing-tool checks keep working)."""
        return [name for op, name in self.tools.items() if name and (self.resolve(op) or op in SKIPPABLE)]

    # --- calls -------------------------------------------------------------------------------------------------------
    def call(self, tool: str, args: dict[str, Any]) -> Any:
        op = next((k for k, v in self.tools.items() if v == tool), None)
        real = self.resolve(op) if op else None
        if real is None:
            if op in SKIPPABLE:
                return {"skipped": f"'{self.server}' has no tool for {op}"}
            raise RuntimeError(f"unknown tool: '{self.server}' has no tool for {tool}")
        if self.read_only and is_write_tool(real):
            raise PermissionError(f"refusing write tool {real} on read-only server '{self.server}'")
        helpers = [t for t in self._server_tools() if _norm(t) in HELPERS and t not in self._context]
        allowed = [tool_prefix(self.server) + t for t in [real, *helpers]]
        same = real == tool
        known = "".join(f"\nAlready known from {t}: {v[:2000]}" for t, v in self._context.items())
        prompt = (
            f"Call the tool {tool_prefix(self.server)}{real} exactly once"
            + (" with exactly these arguments" if same else
               f" to do what `{tool}` does with the arguments below. Map each argument to that tool's own parameter "
               "(for example issue_key -> issueIdOrKey); keep every value, and pass text verbatim. If it requires a "
               "cloudId, use the one already known below, else get it first from the accessible-resources tool")
            + f":\n{json.dumps(args, ensure_ascii=False)}\n{known}\n\n"
            "Call no other tool. After it returns, reply with just DONE."
        )
        cmd = [self.binary, "-p", "--output-format", "stream-json", "--verbose", "--model", self.model, "--tools", "",
               "--allowedTools", *allowed, "--no-session-persistence"]
        for _ in range(2):  # the server can still be connecting when the model starts: then it is not called
            with tempfile.TemporaryDirectory(prefix="devflow-mcp-") as cwd:
                try:
                    proc = self._run(cmd, input=prompt, capture_output=True, text=True, cwd=cwd, env=_env(), timeout=self.timeout_s)
                except subprocess.TimeoutExpired as e:
                    raise TransientToolError(f"{self.server}.{real} timed out") from e
            for helper in helpers:
                seen = tool_result(proc.stdout or "", tool_prefix(self.server) + helper)
                if seen and not seen[1]:
                    self._context[helper] = _unwrap(seen[0])
            result = tool_result(proc.stdout or "", tool_prefix(self.server) + real)
            if result is not None:
                break
        else:
            raise TransientToolError(f"{self.server}.{real} was not called (exit {proc.returncode}): "
                                     f"{_last_text(proc.stdout or '') or (proc.stderr or '').strip()[-300:]}")
        text, is_error = result
        if is_error:
            raise RuntimeError(f"{self.server}.{real} failed: {text[:500]}")
        text = _unwrap(text)
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return text


def _unwrap(text: str) -> str:
    """Servers with structured output wrap a plain-text return as {"result": "<text>"}; take the text back out."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return text
    if isinstance(data, dict) and set(data) == {"result"} and isinstance(data["result"], str):
        return data["result"]
    return text


def _last_text(stream: str) -> str:
    """The model's last words in a stream-json transcript (why it did not call the tool)."""
    for line in reversed(stream.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result" and event.get("result"):
            return str(event["result"])[:300]
    return ""


def tool_result(stream: str, tool: str) -> tuple[str, bool] | None:
    """(text, is_error) of the last result of `tool` in a stream-json transcript, or None if it was never called."""
    ids, last = set(), None
    for line in stream.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        content = (event.get("message") or {}).get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == tool:
                ids.add(block.get("id"))
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in ids:
                raw = block.get("content")
                if isinstance(raw, list):
                    raw = "\n".join(c.get("text", "") for c in raw if isinstance(c, dict) and c.get("type") == "text")
                last = (str(raw or ""), bool(block.get("is_error")))
    return last


def jira_mcp(ws, init: Callable[[], dict] = cached_init, run: Callable = subprocess.run):
    return ClaudeCodeMcp(ws.claude_code_mcp["jira"], ws.jira_tools, JIRA_EQUIVALENTS, init=init, run=run)


def confluence_mcp(ws, init: Callable[[], dict] = cached_init, run: Callable = subprocess.run):
    return ClaudeCodeMcp(ws.claude_code_mcp["confluence"], ws.confluence_tools, CONFLUENCE_EQUIVALENTS, read_only=True,
                         init=init, run=run)


# --- setup: picking the servers ----------------------------------------------------------------------------------
USES = ("jira", "confluence", "playwright")


def suggest(servers: dict[str, dict]) -> dict[str, str]:
    """A likely Claude Code server per use ("" = none): a connected server with the right tools first, else one
    whose name says so (it may still need a sign-in)."""
    def fits(info: dict, use: str) -> bool:
        names = {_norm(t) for t in info["tools"]}
        if use == "playwright":
            return any(n.startswith("browser") for n in names)
        return bool(names & (JIRA_EQUIVALENTS["get_issue"] if use == "jira" else CONFLUENCE_EQUIVALENTS["get_page"]))
    words = {"jira": ("jira", "atlassian"), "confluence": ("confluence", "atlassian"), "playwright": ("playwright",)}
    out = {}
    for use in USES:
        by_tools = [n for n, i in servers.items() if i["status"] == "connected" and fits(i, use)]
        by_name = [n for n in servers if any(w in _norm(n) for w in words[use])]
        out[use] = (by_tools + by_name + [""])[0]
    return out
