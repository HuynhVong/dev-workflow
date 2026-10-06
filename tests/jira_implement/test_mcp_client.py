"""The MCP client against a real stdio server: errors arrive unwrapped, and the server sees the shell's environment."""
import sys
from pathlib import Path

import pytest

from dev_workflows.jira_implement.mcp_client import McpToolError, StdioOrHttpMcp, TransientToolError, root_cause
from dev_workflows.jira_implement.mcp_config import expand

SERVER = str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")


def server(*args, env=None):
    return StdioOrHttpMcp("jira", {"command": sys.executable, "args": [SERVER, *args], **({"env": env} if env else {})})


def test_call_returns_json():
    assert server().call("jira_get_issue", {"issue_key": "AQS-1"})["fields"]["status"]["name"] == "In Progress"


def test_tool_error_is_not_hidden_behind_a_task_group():
    with pytest.raises(McpToolError) as e:
        server().call("jira_get_issue", {"issue_key": "NOPE-1"})
    assert "TaskGroup" not in str(e.value) and str(e.value).startswith("jira.jira_get_issue failed:")


def test_rejected_arguments_name_the_problem():
    with pytest.raises(McpToolError, match="comment_limit"):
        server().call("jira_get_issue", {"issue_key": "AQS-1", "comment_limit": "lots"})


def test_server_crash_shows_its_stderr():
    with pytest.raises(RuntimeError) as e:
        server("crash").list_tools()
    assert "TaskGroup" not in str(e.value) and "JIRA_URL is not set" in str(e.value)


def test_missing_command_is_transient_and_named():
    with pytest.raises(TransientToolError, match="No such file"):
        StdioOrHttpMcp("jira", {"command": "/no/such/uvx"}).list_tools()


def test_server_inherits_the_environment_and_config_env_wins(monkeypatch):
    monkeypatch.setenv("JIRA_URL", "https://jira.example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "from-shell")
    s = server(env={"JIRA_API_TOKEN": "from-config"})
    assert s.call("env_var", {"name": "JIRA_URL"}) == "https://jira.example.com"
    assert s.call("env_var", {"name": "JIRA_API_TOKEN"}) == "from-config"


def test_root_cause_unwraps_nested_groups():
    err = ValueError("401 Unauthorized")
    assert root_cause(ExceptionGroup("outer", [ExceptionGroup("inner", [err])])) is err


def test_expand_claude_code_variables_at_any_depth(monkeypatch):
    monkeypatch.setenv("JIRA_TOKEN", "t0k")
    monkeypatch.delenv("UNSET_VAR", raising=False)
    cfg = {"env": {"JIRA_API_TOKEN": "${JIRA_TOKEN}", "X": "${UNSET_VAR:-fallback}", "Y": "${UNSET_VAR}"},
           "args": ["--token=${JIRA_TOKEN}"], "headers": {"Authorization": "Bearer ${JIRA_TOKEN}"}}
    assert expand(cfg) == {"env": {"JIRA_API_TOKEN": "t0k", "X": "fallback", "Y": "${UNSET_VAR}"},
                           "args": ["--token=t0k"], "headers": {"Authorization": "Bearer t0k"}}
