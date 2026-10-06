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
    assert suggest(servers) == {"jira": "claude.ai Atlassian", "confluence": "claude.ai Atlassian", "playwright": "pw"}
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
    gw.status_and_assignee("AQS-1")
    cmd, kw = runs.calls[1]
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
    assert not any("create" in a.lower() or "update" in a.lower() for a in runs.calls[0][0])
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
