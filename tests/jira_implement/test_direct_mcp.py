"""Direct + fallback: a Claude Code MCP server with a local config is started by devflow itself, its own tool and
argument names are matched, and whatever can't be matched goes through Claude Code."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from dev_workflows import doctor
from dev_workflows.jira_implement.claude_code_mcp import CONFLUENCE_EQUIVALENTS, JIRA_EQUIVALENTS, ClaudeCodeMcp
from dev_workflows.jira_implement.direct_mcp import DirectMcp, map_args
from dev_workflows.jira_implement.jira import JiraGateway
from dev_workflows.jira_implement.mcp_config import confluence_mcp_for, jira_mcp_for
from dev_workflows.jira_implement.workspace import DEFAULT_CONFLUENCE_READ_TOOLS, DEFAULT_JIRA_TOOLS, load_workspace

FAKE = str(Path(__file__).parent / "fixtures" / "fake_pal_jira.py")


def test_arguments_map_to_the_tools_own_names():
    schema = {"properties": {"issueKey": {"type": "string"}, "fields": {"type": "array"}}, "required": ["issueKey"]}
    assert map_args({"issue_key": "A-1", "fields": "summary,status", "comment_limit": 10}, schema) == \
        ({"issueKey": "A-1", "fields": ["summary", "status"]}, ["comment_limit"])
    schema = {"properties": {"issueIdOrKey": {}, "body": {}}, "required": ["issueIdOrKey", "body"]}
    assert map_args({"issue_key": "A-1", "comment": "hi"}, schema) == ({"issueIdOrKey": "A-1", "body": "hi"}, [])
    # an exact name wins over a synonym: commentId takes comment_id, so `id` is not misused
    schema = {"properties": {"id": {}, "commentId": {}, "body": {}}, "required": ["commentId", "body"]}
    assert map_args({"issue_key": "A-1", "comment_id": "9", "comment": "x"}, schema)[0] == {"commentId": "9", "body": "x"}
    assert map_args({"transition_id": "31"}, {"properties": {"transitionId": {"type": "integer"}}}) == ({"transitionId": 31}, [])
    assert map_args({"comment": "x"}, {"properties": {"commentRef": {}, "newText": {}}, "required": ["commentRef"]}) is None


class Fallback:
    def __init__(self):
        self.calls = []

    def call(self, tool, args):
        self.calls.append(tool)
        return {"via": "claude_code", "tool": tool}

    def list_tools(self):
        return ["jira_get_issue"]


@pytest.fixture
def pal(tmp_path, monkeypatch):
    log, state = tmp_path / "log", tmp_path / "state.json"
    log.write_text("")
    state.write_text("{}")
    monkeypatch.setenv("FAKE_PAL_LOG", str(log))
    monkeypatch.setenv("FAKE_PAL_STATE", str(state))
    fb = Fallback()
    mcp = DirectMcp("pal-jira", {"command": sys.executable, "args": [FAKE]}, DEFAULT_JIRA_TOOLS, JIRA_EQUIVALENTS,
                    fallback=lambda: fb, timeout_s=10)
    yield SimpleNamespace(mcp=mcp, fb=fb, calls=lambda: [line.split() for line in log.read_text().splitlines()],
                          die=lambda n: state.write_text(json.dumps({"die": n})))
    mcp.close()


def test_calls_go_direct_on_one_open_session(pal):
    jira = JiraGateway(pal.mcp, DEFAULT_JIRA_TOOLS, ("To Do", "In Progress"))
    assert pal.mcp.call("jira_get_issue", {"issue_key": "AQS-1", "fields": "summary"})["fields"]["asked"] == ["summary"]
    assert jira.search("project = AQS", limit=5)[0]["key"] == "AQS-1"
    assert pal.mcp.call("jira_add_comment", {"issue_key": "AQS-1", "comment": "hello"})["body"] == "hello"
    assert pal.mcp.call("jira_transition_issue", {"issue_key": "AQS-1", "transition_id": "31"})["to"] == 31
    calls = pal.calls()
    assert [c[1] for c in calls] == ["get_issue", "search_issues", "add_comment", "transition"]
    assert len({c[0] for c in calls}) == 1 and pal.fb.calls == []  # one server process, no Claude Code


def test_what_cant_be_matched_goes_through_claude_code(pal):
    # update_comment needs commentRef, which devflow has no value for
    out = pal.mcp.call("jira_edit_comment", {"issue_key": "AQS-1", "comment_id": "1", "comment": "x"})
    assert out == {"via": "claude_code", "tool": "jira_edit_comment"}
    assert pal.mcp.route("edit_comment")[0] == "claude_code"
    assert pal.mcp.call("jira_get_transitions", {"issue_key": "AQS-1"})["via"] == "claude_code"  # no such tool
    assert "update_comment" not in [c[1] for c in pal.calls()]


def test_rejected_arguments_fall_back_once_and_are_remembered(pal):
    out = pal.mcp.call("jira_transition_issue", {"issue_key": "AQS-1", "transition_id": "done"})  # not an int
    assert out["via"] == "claude_code" and "rejected the arguments" in pal.mcp.route("transition_issue")[1]
    pal.mcp.call("jira_transition_issue", {"issue_key": "AQS-1", "transition_id": "31"})
    assert pal.fb.calls == ["jira_transition_issue", "jira_transition_issue"]


def test_a_dead_server_is_restarted_for_reads(pal):
    pal.mcp.call("jira_get_issue", {"issue_key": "AQS-1"})
    pal.die(1)
    assert pal.mcp.call("jira_get_issue", {"issue_key": "AQS-2"})["key"] == "AQS-2"
    pids = [c[0] for c in pal.calls()]
    assert len(pids) == 3 and pids[0] == pids[1] != pids[2] and pal.fb.calls == []


def test_a_server_that_wont_start_sends_everything_to_claude_code():
    fb = Fallback()
    mcp = DirectMcp("pal-jira", {"command": "/no/such/pal"}, DEFAULT_JIRA_TOOLS, JIRA_EQUIVALENTS, fallback=lambda: fb)
    assert mcp.call("jira_get_issue", {"issue_key": "A-1"})["via"] == "claude_code"
    assert mcp.list_tools() == ["jira_get_issue"] and "could not start" in mcp.route("get_issue")[1]
    mcp.close()


def test_confluence_stays_read_only(pal):
    conf = DirectMcp("pal-confluence", {"command": sys.executable, "args": [FAKE]}, DEFAULT_CONFLUENCE_READ_TOOLS,
                     CONFLUENCE_EQUIVALENTS, read_only=True, fallback=lambda: pal.fb, timeout_s=10)
    try:
        assert "create_page" not in conf.schemas()
        assert conf.call("confluence_get_page", {"page_id": "42"})["title"] == "Spec"
        assert conf.route("get_page") == ("direct", "get_page")
    finally:
        conf.close()


def test_doctor_shows_each_route(pal):
    checks = doctor.direct_route_checks(pal.mcp, "Jira MCP")
    assert checks[0].status == "warn" and "get_issue -> get_issue" in checks[0].detail
    assert "edit_comment" in checks[0].detail.split("Through Claude Code")[1]
    assert doctor.direct_route_checks(object(), "Jira MCP") == []


def write_ws(tmp_path, extra=""):
    p = tmp_path / "workspace.yaml"
    p.write_text("repos: {}\nclaude_code_mcp: {jira: pal-jira, confluence: pal-confluence}\n" + extra)
    return load_workspace(p)


def test_claude_code_servers_with_a_local_config_go_direct(tmp_path):
    cc = tmp_path / ".claude.json"
    cc.write_text(json.dumps({"mcpServers": {"pal-jira": {"command": "pal", "args": ["jira"]},
                                             "pal-confluence": {"command": "pal", "args": ["confluence"]}}}))
    mcp, source = jira_mcp_for(write_ws(tmp_path), files=[cc])
    assert isinstance(mcp, DirectMcp) and source == "pal-jira: direct, Claude Code login as fallback"
    conf = confluence_mcp_for(write_ws(tmp_path), mcp, files=[cc])
    assert isinstance(conf, DirectMcp) and conf.read_only
    assert isinstance(mcp.fallback(), ClaudeCodeMcp) and conf.fallback().read_only
    # no local config (an account connector), or turned off: the Claude Code login as before
    assert isinstance(jira_mcp_for(write_ws(tmp_path), files=[])[0], ClaudeCodeMcp)
    assert isinstance(jira_mcp_for(write_ws(tmp_path, "mcp_direct: false\n"), files=[cc])[0], ClaudeCodeMcp)
