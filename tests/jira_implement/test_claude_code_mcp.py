import json
import subprocess
from types import SimpleNamespace

import pytest

from dev_workflows import doctor
from dev_workflows.jira_implement.claude_code_mcp import (CONFLUENCE_EQUIVALENTS, JIRA_EQUIVALENTS, ClaudeCodeMcp,
                                                          claude_code_servers, suggest, tool_result)
from dev_workflows.jira_implement.confluence import ConfluenceReader, ConfluenceWriteBlocked
from dev_workflows.jira_implement.jira import JiraGateway
from dev_workflows.jira_implement.mcp_client import TransientToolError
from dev_workflows.jira_implement.mcp_config import confluence_mcp_for, has_playwright, jira_mcp_for, playwright_servers
from dev_workflows.jira_implement.workspace import (DEFAULT_CONFLUENCE_READ_TOOLS, DEFAULT_JIRA_TOOLS, load_workspace,
                                                    set_claude_code_mcp)

OFFICIAL = ["getAccessibleAtlassianResources", "getJiraIssue", "addCommentToJiraIssue", "searchJiraIssuesUsingJql",
            "getConfluencePage", "createConfluencePage", "updateConfluencePage"]
P = "mcp__claude_ai_Atlassian__"


def init(status="connected", tools=OFFICIAL, name="claude.ai Atlassian"):
    prefix = "mcp__" + name.replace(".", "_").replace(" ", "_") + "__"
    return {"type": "system", "subtype": "init", "mcp_servers": [{"name": name, "status": status}, {"name": "pw", "status": "connected"}],
            "tools": ["Read"] + [prefix + t for t in tools] + ["mcp__pw__browser_navigate"]}


def stream(*calls):
    """A stream-json transcript in which each (tool, result) pair was called and answered."""
    lines = []
    for i, (tool, result, *err) in enumerate(calls):
        lines.append({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": f"t{i}", "name": tool, "input": {}}]}})
        lines.append({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{i}", "is_error": bool(err),
                                                                "content": [{"type": "text", "text": result}]}]}})
    lines.append({"type": "result", "subtype": "success", "result": "DONE"})
    return "\n".join(json.dumps(x) for x in lines)


class Runs:
    def __init__(self, *outputs):
        self.outputs, self.calls = list(outputs), []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 0, stdout=self.outputs.pop(0), stderr="")


def jira_mcp(runs, **kw):
    return ClaudeCodeMcp("claude.ai Atlassian", DEFAULT_JIRA_TOOLS, JIRA_EQUIVALENTS, run=runs, init=lambda: init(**kw))


def test_servers_and_suggestions_come_from_the_init_event():
    servers = claude_code_servers(init())
    assert servers["claude.ai Atlassian"] == {"status": "connected", "tools": OFFICIAL}
    assert suggest(servers) == {"jira": "claude.ai Atlassian", "confluence": "claude.ai Atlassian", "playwright": "pw", "mysql": ""}
    assert suggest(claude_code_servers(init(status="needs-auth")))["jira"] == "claude.ai Atlassian"


def test_tool_names_map_to_the_official_connector():
    mcp = jira_mcp(Runs())
    assert mcp.resolve("get_issue") == "getJiraIssue" and mcp.resolve("search") == "searchJiraIssuesUsingJql"
    assert mcp.resolve("edit_comment") is None
    tools = set(mcp.list_tools())
    assert {"jira_get_issue", "jira_add_comment", "jira_search", "jira_download_attachments"} <= tools
    assert "jira_edit_comment" not in tools and "jira_update_issue" not in tools


def test_call_allows_only_that_tool_and_reads_its_raw_result_then_reuses_the_cloud_id():
    issue = {"key": "AQS-1", "fields": {"status": {"name": "In Progress"}, "assignee": {"emailAddress": "me@acme.io"}}}
    runs = Runs(stream((P + "getAccessibleAtlassianResources", '[{"id": "c1"}]'), (P + "getJiraIssue", json.dumps(issue))),
                stream((P + "getJiraIssue", json.dumps(issue))))
    gw = JiraGateway(jira_mcp(runs), DEFAULT_JIRA_TOOLS, ("To Do", "In Progress"))
    assert gw.status_and_assignee("AQS-1") == ("In Progress", "me@acme.io")
    cmd, kw = runs.calls[0]
    allowed = cmd[cmd.index("--allowedTools") + 1:cmd.index("--no-session-persistence")]
    assert allowed == [P + "getJiraIssue", P + "getAccessibleAtlassianResources"] and cmd[cmd.index("--tools") + 1] == ""
    assert '"issue_key": "AQS-1"' in kw["input"] and "ANTHROPIC_API_KEY" not in kw["env"]
    assert cmd[cmd.index("--max-turns") + 1] == "2"  # the cloudId lookup, then the call; the result is never sent back to the model
    gw.status_and_assignee("AQS-1")
    cmd, kw = runs.calls[1]
    assert cmd[cmd.index("--max-turns") + 1] == "1"
    assert cmd[cmd.index("--allowedTools") + 1:cmd.index("--no-session-persistence")] == [P + "getJiraIssue"]
    assert 'Already known from getAccessibleAtlassianResources: [{"id": "c1"}]' in kw["input"]


def test_wrapped_results_errors_retries_and_skips():
    runs = Runs(stream((P + "getJiraIssue", json.dumps({"result": '{"key": "A-1"}'}))))
    assert jira_mcp(runs).call("jira_get_issue", {"issue_key": "A-1"}) == {"key": "A-1"}
    with pytest.raises(RuntimeError, match="no permission"):
        jira_mcp(Runs(stream((P + "addCommentToJiraIssue", "no permission", True)))).call("jira_add_comment", {})
    runs = Runs(stream(), stream((P + "getJiraIssue", "{}")))  # not called the first time (server still connecting)
    assert jira_mcp(runs).call("jira_get_issue", {}) == {} and len(runs.calls) == 2
    with pytest.raises(TransientToolError, match="was not called"):
        jira_mcp(Runs(stream(), stream())).call("jira_get_issue", {})
    assert "skipped" in jira_mcp(Runs()).call("jira_download_attachments", {"issue_key": "A-1"})
    with pytest.raises(RuntimeError, match="unknown tool"):
        jira_mcp(Runs()).call("jira_edit_comment", {})


def test_a_server_that_needs_sign_in_or_does_not_exist_says_what_to_do():
    with pytest.raises(RuntimeError, match="needs-auth.*/mcp"):
        jira_mcp(Runs(), status="needs-auth").list_tools()
    with pytest.raises(RuntimeError, match="no MCP server named 'nope'"):
        ClaudeCodeMcp("nope", DEFAULT_JIRA_TOOLS, JIRA_EQUIVALENTS, init=init).list_tools()


def test_confluence_stays_read_only_through_claude_code():
    runs = Runs(stream((P + "getConfluencePage", '{"id": "42"}')))
    mcp = ClaudeCodeMcp("claude.ai Atlassian", DEFAULT_CONFLUENCE_READ_TOOLS, CONFLUENCE_EQUIVALENTS, read_only=True, run=runs, init=init)
    assert "createConfluencePage" not in mcp._server_tools() and "updateConfluencePage" not in mcp._server_tools()
    reader = ConfluenceReader(mcp, DEFAULT_CONFLUENCE_READ_TOOLS)
    assert reader.get_page("42") == {"id": "42"}
    cmd = runs.calls[0][0]
    allowed = cmd[cmd.index("--allowedTools") + 1:cmd.index("--no-session-persistence")]
    assert not any("create" in a.lower() or "update" in a.lower() for a in allowed)
    hidden = cmd[cmd.index("--disallowedTools") + 1:]
    assert P + "createConfluencePage" in hidden and P + "updateConfluencePage" in hidden
    with pytest.raises(ConfluenceWriteBlocked):
        ConfluenceReader(mcp, {"get_page": "createConfluencePage"})
    with pytest.raises(RuntimeError):
        mcp.call("confluence_create_page", {})


def test_tool_result_ignores_other_tools():
    out = stream(("mcp__x__other", "nope"), ("mcp__x__getJiraIssue", "yes"))
    assert tool_result(out, "mcp__x__getJiraIssue") == ("yes", False) and tool_result(out, "mcp__x__missing") is None


def write_ws(tmp_path, extra=""):
    p = tmp_path / "workspace.yaml"
    p.write_text("# keep me\nrepos: {}\nmcp_servers:\n  atlassian: {command: uvx, args: [mcp-atlassian]}\n" + extra)
    return p


def test_setup_choice_is_saved_in_place_and_blank_falls_back(tmp_path):
    p = write_ws(tmp_path)
    ws = load_workspace(p)
    mcp, source = jira_mcp_for(ws, files=[])
    assert type(mcp).__name__ == "StdioOrHttpMcp" and source == "workspace.yaml"
    assert type(confluence_mcp_for(ws, mcp)).__name__ == "StdioOrHttpMcp"

    set_claude_code_mcp(p, {"jira": "claude.ai Atlassian", "confluence": "", "playwright": "pw"})
    text = p.read_text()
    assert text.startswith("# keep me") and "claude_code_mcp:" in text
    ws = load_workspace(p)
    assert ws.claude_code_mcp == {"jira": "claude.ai Atlassian", "playwright": "pw"}
    mcp, source = jira_mcp_for(ws, files=[])
    assert isinstance(mcp, ClaudeCodeMcp) and source == "Claude Code login: claude.ai Atlassian"
    # Confluence left blank: the workspace's own server, never the Claude Code Jira connector by accident
    assert type(confluence_mcp_for(ws, mcp, files=[])).__name__ == "StdioOrHttpMcp"
    set_claude_code_mcp(p, {"jira": "x", "confluence": "x", "playwright": ""})
    assert load_workspace(p).claude_code_mcp == {"jira": "x", "confluence": "x"} and p.read_text().count("claude_code_mcp:") == 1


def test_playwright_from_claude_code(tmp_path):
    cc = tmp_path / ".claude.json"
    cc.write_text(json.dumps({"mcpServers": {"pw": {"command": "npx", "args": ["@playwright/mcp@latest"]}}}))
    ws = load_workspace(write_ws(tmp_path, "claude_code_mcp: {playwright: pw}\n"))
    assert has_playwright(ws)
    assert playwright_servers(ws, files=[cc]) == {"pw": {"command": "npx", "args": ["@playwright/mcp@latest"]}}
    assert playwright_servers(ws, files=[]) == {}  # account-level server: the agent inherits it from Claude Code


def test_doctor_reports_each_chosen_server():
    ws = SimpleNamespace(claude_code_mcp={"jira": "claude.ai Atlassian", "confluence": "gone", "playwright": "pw"})
    servers = claude_code_servers(init(status="needs-auth"))
    checks = {c.id: c for c in doctor.claude_code_mcp_checks(ws, servers)}
    assert checks["cc.jira"].status == "fail" and "/mcp" in checks["cc.jira"].fix
    assert checks["cc.confluence"].status == "fail" and "no MCP server named" in checks["cc.confluence"].detail
    assert checks["cc.playwright"].status == "ok"
    assert doctor.claude_code_mcp_checks(SimpleNamespace(claude_code_mcp={})) == []


def test_a_server_with_its_own_names_still_resolves_or_doctor_names_the_candidates():
    own = ["get_issue", "get_transitions", "transition", "add_comment", "update_comment", "search_issues", "get_user_profile"]
    mcp = ClaudeCodeMcp("pal-jira", DEFAULT_JIRA_TOOLS, JIRA_EQUIVALENTS, init=lambda: init(tools=own, name="pal-jira"))
    assert mcp.resolve("transition_issue") == "transition" and mcp.resolve("edit_comment") == "update_comment"
    assert mcp.resolve("add_comment") == "add_comment" and mcp.resolve("get_transitions") == "get_transitions"
    assert JiraGateway(mcp, DEFAULT_JIRA_TOOLS, ("To Do",)).missing_tools() == []

    # two candidates for edit_comment: doctor lists them instead of guessing
    two = ["get_issue", "get_transitions", "add_comment", "edit_comment_body", "update_comment"]
    mcp = ClaudeCodeMcp("pal-jira", DEFAULT_JIRA_TOOLS, JIRA_EQUIVALENTS, init=lambda: init(tools=two, name="pal-jira"))
    ws = SimpleNamespace(jira_tools=DEFAULT_JIRA_TOOLS, confluence_server="x")
    check = doctor.mcp_checks(ws, JiraGateway(mcp, DEFAULT_JIRA_TOOLS, ("To Do",)), None, need_confluence=False)[0]
    assert check.status == "fail"
    assert "edit_comment: <one of: add_comment, edit_comment_body, update_comment>" in check.fix
    assert "transition_issue: <one of: get_transitions>" in check.fix


def test_call_sends_only_the_tools_it_needs_and_records_its_tokens(monkeypatch):
    recorded = []
    monkeypatch.setattr("dev_workflows.telemetry.record_usage", lambda *a, **kw: recorded.append((a, kw)))
    out = stream((P + "getAccessibleAtlassianResources", '[{"id": "c1"}]'), (P + "getJiraIssue", '{"key": "AQS-1"}'))
    out = out.rsplit("\n", 1)[0] + "\n" + json.dumps({"type": "result", "subtype": "success", "result": "DONE",
                                                      "usage": {"input_tokens": 900, "output_tokens": 20},
                                                      "modelUsage": {"claude-haiku-4-5-20251001": {}}, "total_cost_usd": 0.001})
    runs = Runs(out)
    jira_mcp(runs).call("jira_get_issue", {"issue_key": "AQS-1"})
    cmd = runs.calls[0][0]
    hidden = cmd[cmd.index("--disallowedTools") + 1:]
    # the other server is hidden whole, and this server's tools the call doesn't need one by one
    assert "mcp__pw" in hidden and P + "searchJiraIssuesUsingJql" in hidden and P + "addCommentToJiraIssue" in hidden
    assert P + "getJiraIssue" not in hidden and P + "getAccessibleAtlassianResources" not in hidden
    assert "--disable-slash-commands" in cmd and len(cmd[cmd.index("--system-prompt") + 1]) < 200
    (step, model, usage), kw = recorded[0]
    assert step == "mcp.get_issue" and model == "claude-haiku-4-5-20251001" and usage["input_tokens"] == 900
    assert kw["source"] == "claude_code_mcp" and kw["cost_usd"] == 0.001
