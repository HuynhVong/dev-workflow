"""Claude Code (headless, via the Agent SDK) as the coding engine. LangGraph owns flow, state and side effects.

Every tool call Claude Code makes passes `tool_policy` (a PreToolUse hook):
- files: only inside the in-scope repo it was launched for
- git/glab: only `rtk git` read commands (status/diff/log/show/blame/grep); no commits, pushes, branches or glab
- MCP: no write tools on any server (Jira and Confluence writes belong to the workflow, Confluence never)
"""
import asyncio
import json
import os
import re
import shlex
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from .. import telemetry
from ..routing import Routing, skills_hint
from .confluence import is_write_tool
from .scope import ScopeGuard

READ_ONLY_GIT = {"status", "diff", "log", "show", "blame", "grep", "ls-files", "rev-parse", "branch", "merge-base"}
FILE_TOOLS = {"Read": "file_path", "Edit": "file_path", "MultiEdit": "file_path", "Write": "file_path",
              "NotebookEdit": "notebook_path", "Glob": "path", "Grep": "path", "LS": "path"}
EDIT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}
MCP_WRITE = re.compile(r"(create|update|edit|delete|remove|add_|_add|transition|assign|move|comment|upload|publish|merge|approve|write|post)", re.I)


def tool_policy(tool: str, tool_input: dict, roots: list[str], read_only: bool = False,
                read_roots: list[str] = ()) -> tuple[bool, str]:
    """Pure policy check (unit-tested). Returns (allowed, reason). `read_roots` (the global skill folders) may be
    read and their scripts run, never edited."""
    def inside(p: str, among=None) -> bool:
        real = os.path.realpath(p)
        return any(real == r or real.startswith(r + os.sep) for r in (roots if among is None else among))

    if tool in FILE_TOOLS:
        path = tool_input.get(FILE_TOOLS[tool]) or roots[0]
        if not os.path.isabs(path):
            path = os.path.join(roots[0], path)
        if not inside(path):
            if tool not in EDIT_TOOLS and inside(path, read_roots):
                return True, ""
            return False, f"{path} is outside the repos in scope"
        if read_only and tool in EDIT_TOOLS:
            return False, "this step is read-only"
        return True, ""
    if tool == "Bash":
        cmd = tool_input.get("command", "")
        for segment in re.split(r"&&|\|\||;|\||\n", cmd):
            try:
                words = shlex.split(segment)
            except ValueError:
                words = segment.split()
            if not words:
                continue
            if words[0] in ("git", "glab"):
                return False, "call git through `rtk git` (read-only commands only); glab is not available to this step"
            if words[0] == "rtk" and len(words) > 1:
                if words[1] == "glab":
                    return False, "GitLab actions are done by the workflow, not the coding agent"
                if words[1] == "git" and (len(words) < 3 or words[2] not in READ_ONLY_GIT or (words[2] == "branch" and len(words) > 3)):
                    return False, "only read-only `rtk git` commands are allowed; the workflow commits and pushes"
            for w in words:
                if os.path.isabs(w) and not inside(w) and not inside(w, read_roots) and not w.startswith(("/tmp", "/dev/null", "/usr", "/bin", "/opt", "/etc", "/proc")):
                    return False, f"{w} is outside the repos in scope"
        return True, ""
    if tool.startswith("mcp__"):
        if "confluence" in tool.lower() and is_write_tool(tool):
            return False, "Confluence is strictly read-only"
        if MCP_WRITE.search(tool.split("__")[-1]) and "browser" not in tool.lower():
            return False, "MCP write tools are not available to the coding agent"
        return True, ""
    return True, ""


def describe_tool(tool: str, tool_input: dict, roots: list[str]) -> str:
    """A one-line, path-relative description of a tool call for the live activity log."""
    def rel(p: str) -> str:
        for r in roots:
            if p.startswith(r + os.sep):
                return p[len(r) + 1:]
        return p
    if tool in FILE_TOOLS:
        target = tool_input.get(FILE_TOOLS[tool]) or tool_input.get("pattern") or ""
        extra = f" {tool_input['pattern']}" if tool in ("Glob", "Grep") and tool_input.get("pattern") and target != tool_input.get("pattern") else ""
        return f"{rel(str(target))}{extra}"[:300]
    if tool == "Bash":
        return str(tool_input.get("command", ""))[:300]
    return json.dumps(tool_input, default=str)[:200]


@dataclass
class CodingResult:
    ok: bool
    summary: str = ""
    files_changed: list[str] = field(default_factory=list)
    out_of_scope_needs: list[dict] = field(default_factory=list)  # [{repo, reason}]
    findings: dict = field(default_factory=dict)  # explore/verify payload
    raw: str = ""


class CodingAgent(Protocol):
    """`step` picks the model and the skills (routing.STEPS); `escalate` uses the step's stronger model."""
    def implement(self, repo: str, path: str, instructions: str, step: str = "implement", escalate: bool = False) -> CodingResult: ...
    def explore(self, repo: str, path: str, question: str, step: str = "discover_repos") -> CodingResult: ...
    def test_case(self, repos: dict[str, str], instructions: str, step: str = "ticket_review.e2e") -> dict: ...
    def verify(self, repos: dict[str, str], instructions: str, step: str = "integration_check") -> CodingResult: ...


IMPLEMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean", "description": "true if the tasks are implemented in this repo"},
        "summary": {"type": "string"},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "out_of_scope_needs": {"type": "array", "items": {"type": "object", "properties": {
            "repo": {"type": "string"}, "reason": {"type": "string"}}, "required": ["repo", "reason"]}},
    },
    "required": ["ok", "summary", "files_changed", "out_of_scope_needs"],
}
EXPLORE_SCHEMA = {
    "type": "object",
    "properties": {
        "confirmed": {"type": "boolean"}, "relevant_files": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
        "out_of_scope_needs": IMPLEMENT_SCHEMA["properties"]["out_of_scope_needs"],
    },
    "required": ["confirmed", "relevant_files", "notes", "out_of_scope_needs"],
}
VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "results": {"type": "array", "items": {"type": "object", "properties": {
            "criterion": {"type": "string"}, "passed": {"type": "boolean"}, "evidence": {"type": "string"},
            "suspected_repos": {"type": "array", "items": {"type": "string"}}},
            "required": ["criterion", "passed", "evidence", "suspected_repos"]}},
    },
    "required": ["passed", "results"],
}

E2E_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["passed", "failed", "needs_human"]},
        "summary": {"type": "string", "description": "What you did and saw, in two or three sentences."},
        "steps": {"type": "array", "items": {"type": "object", "properties": {
            "step": {"type": "string"}, "ok": {"type": "boolean"}, "note": {"type": "string"}},
            "required": ["step", "ok", "note"]}},
        "screenshots": {"type": "array", "items": {"type": "string"}, "description": "File names of the screenshots you saved."},
        "needs_human": {"type": "string", "description": "When status is needs_human: exactly what the developer must do or give you."},
    },
    "required": ["status", "summary", "steps", "screenshots", "needs_human"],
}

RULES = (
    "Rules you must follow:\n"
    "- Work only inside the repository paths you were given. Never read or edit other repositories.\n"
    "- Do not commit, push, create branches or call glab. Use only read-only `rtk git` commands (status, diff, log, show).\n"
    "- Every git command must be prefixed with rtk.\n"
    "- Confluence is read-only. Never write to Jira or Confluence.\n"
    "- Never touch CI/CD configuration or pipelines unless the task explicitly lists that file.\n"
    "- If the task needs a change in a repository you were not given, do not make it: report it in out_of_scope_needs.\n"
)


class ClaudeCodeAgent:
    def __init__(self, scope: ScopeGuard, routing: Routing | None = None, mcp_servers: dict | None = None, max_turns: int = 200):
        self.scope, self.routing, self.mcp_servers, self.max_turns = scope, routing or Routing(), mcp_servers or {}, max_turns

    def implement(self, repo: str, path: str, instructions: str, step: str = "implement", escalate: bool = False) -> CodingResult:
        out = self._run([path], f"{RULES}\nRepository: {repo} ({path})\n\n{instructions}", IMPLEMENT_SCHEMA, read_only=False,
                        step=step, escalate=escalate, repo=repo)
        return CodingResult(ok=bool(out.get("ok")), summary=out.get("summary", ""), files_changed=out.get("files_changed", []),
                            out_of_scope_needs=out.get("out_of_scope_needs", []), raw=json.dumps(out))

    def explore(self, repo: str, path: str, question: str, step: str = "discover_repos") -> CodingResult:
        out = self._run([path], f"{RULES}\nThis step is READ-ONLY: do not edit files.\nRepository: {repo} ({path})\n\n{question}", EXPLORE_SCHEMA,
                        read_only=True, step=step, repo=repo)
        return CodingResult(ok=bool(out.get("confirmed")), summary=out.get("notes", ""), files_changed=out.get("relevant_files", []),
                            out_of_scope_needs=out.get("out_of_scope_needs", []), findings=out, raw=json.dumps(out))

    def test_case(self, repos: dict[str, str], instructions: str, step: str = "ticket_review.e2e") -> dict:
        """One E2E test case in the browser through the Playwright MCP. Read-only for source files."""
        listing = "\n".join(f"- {n}: {p}" for n, p in repos.items())
        return self._run(list(repos.values()), f"{RULES}\nThis step tests the running app in the browser and must NOT edit "
                         f"source files.\nRepositories (for reading code only):\n{listing}\n\n{instructions}", E2E_SCHEMA,
                         read_only=True, step=step)

    def verify(self, repos: dict[str, str], instructions: str, step: str = "integration_check") -> CodingResult:
        listing = "\n".join(f"- {n}: {p}" for n, p in repos.items())
        out = self._run(list(repos.values()), f"{RULES}\nThis step verifies behaviour and must NOT edit source files.\nRepositories:\n{listing}\n\n{instructions}", VERIFY_SCHEMA, read_only=True, step=step)
        return CodingResult(ok=bool(out.get("passed")), findings=out, raw=json.dumps(out))

    def _run(self, roots: list[str], prompt: str, schema: dict, read_only: bool, step: str, escalate: bool = False,
             repo: str = "") -> dict:
        from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, HookMatcher, ResultMessage, ToolUseBlock, query

        roots = [os.path.realpath(r) for r in roots]
        for r in roots:
            self.scope.require_path(r)
        model = self.routing.model(step, escalate=escalate)
        skill_roots = [os.path.realpath(d) for d in self.routing.registry.dirs]
        prompt += skills_hint(self.routing.skills(step, repo_path=roots[0]))

        ctx = telemetry.current()  # the run and node this agent works for (activity and usage are recorded there)

        async def pre_tool_use(hook_input, tool_use_id, context):
            tool, tool_input = hook_input["tool_name"], hook_input.get("tool_input") or {}
            allowed, reason = tool_policy(tool, tool_input, roots, read_only, skill_roots)
            if allowed:
                return {}
            telemetry.record_activity({"tool": tool, "detail": describe_tool(tool, tool_input, roots), "denied": True,
                                       "reason": reason}, ctx=ctx, repo=repo)
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny", "permissionDecisionReason": reason}}

        options = ClaudeAgentOptions(
            cwd=roots[0], add_dirs=roots[1:], model=model, effort=self.routing.effort(model), max_turns=self.max_turns,
            permission_mode="acceptEdits", setting_sources=["user", "project"], mcp_servers=self.mcp_servers,
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool_use])]},
            output_format={"type": "json_schema", "schema": schema},
        )

        async def go() -> dict:
            final, started = None, time.time()
            async for msg in query(prompt=prompt, options=options):
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, ToolUseBlock):
                            telemetry.record_activity({"tool": block.name, "detail": describe_tool(block.name, block.input or {}, roots)},
                                                      ctx=ctx, repo=repo)
                if isinstance(msg, ResultMessage):
                    final = msg
            if final is not None:
                telemetry.record_usage(step, model, telemetry.usage_dict(final.usage), source="claude_code",
                                       cost_usd=final.total_cost_usd, duration_s=time.time() - started, ctx=ctx, repo=repo)
            if final is None or final.is_error:
                raise RuntimeError(f"Claude Code failed: {getattr(final, 'errors', None) or getattr(final, 'result', None)}")
            if isinstance(final.structured_output, dict):
                return final.structured_output
            return json.loads(final.result or "{}")

        return asyncio.run(go())
