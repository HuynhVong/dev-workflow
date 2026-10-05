import ast
import subprocess
from pathlib import Path

import pytest

from dev_workflows.jira_implement.coding_agent import tool_policy
from dev_workflows.jira_implement.confluence import ConfluenceReader, ConfluenceWriteBlocked, is_write_tool
from dev_workflows.jira_implement.dag import DagError, downstream_closure, validate, waves
from dev_workflows.jira_implement.scope import ScopeGuard, ScopeViolation
from dev_workflows.jira_implement.vcs import Vcs, VcsPolicyError, check_policy

SRC = Path(__file__).resolve().parents[2] / "src" / "dev_workflows"


def recorder():
    calls = []

    def run(cmd, cwd):
        calls.append((cmd, cwd))
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return calls, run


def test_every_vcs_call_is_prefixed_with_rtk(tmp_path):
    calls, run = recorder()
    vcs = Vcs(ScopeGuard.create({"api": str(tmp_path)}), prefix="rtk", runner=run)
    vcs.git("api", "status")
    vcs.glab("api", "mr", "list")
    assert [c[0][:2] for c in calls] == [["rtk", "git"], ["rtk", "glab"]]


@pytest.mark.parametrize("tool,args", [
    ("git", ["push", "--force"]), ("git", ["push", "-f", "origin", "x"]), ("git", ["push", "origin", "+x"]),
    ("git", ["push", "--delete", "origin", "x"]), ("git", ["branch", "-D", "x"]), ("git", ["reset", "--hard"]),
    ("git", ["rebase", "develop"]), ("glab", ["mr", "merge", "1"]), ("glab", ["mr", "approve", "1"]),
    ("glab", ["mr", "update", "1", "--ready"]), ("glab", ["ci", "status"]), ("glab", ["pipeline", "list"]),
    ("glab", ["api", "projects"]), ("bash", ["-c", "x"]),
    # glab api is open only for MR discussion read / reply / edit-own-reply with a body field
    ("glab", ["api", "projects/:id/pipelines"]),
    ("glab", ["api", "--method", "PUT", "projects/:id/merge_requests/1/discussions/ab12", "-f", "resolved=true"]),
    ("glab", ["api", "--method", "POST", "projects/:id/merge_requests/1/discussions/ab12/notes", "-f", "body=x", "-f", "resolved=true"]),
    ("glab", ["api", "--method", "POST", "projects/:id/merge_requests/1/discussions/ab12/notes", "-F", "body=@/etc/passwd"]),
    ("glab", ["api", "--method", "DELETE", "projects/:id/merge_requests/1/discussions/ab12/notes/5"]),
    ("glab", ["api", "--method", "PUT", "projects/:id/merge_requests/1", "-f", "body=x"]),
])
def test_forbidden_vcs_commands(tool, args):
    with pytest.raises(VcsPolicyError):
        check_policy(tool, args)


@pytest.mark.parametrize("args", [
    ["api", "projects/:id/merge_requests/3/discussions?per_page=100"],
    ["api", "--method", "POST", "projects/:id/merge_requests/3/discussions/ab12/notes", "-f", "body=Fixed"],
    ["api", "--method", "PUT", "projects/:id/merge_requests/3/discussions/ab12/notes/55", "-f", "body=Fixed"],
])
def test_mr_discussion_calls_are_allowed(args):
    check_policy("glab", args)


def test_vcs_refuses_out_of_scope_repo(tmp_path):
    calls, run = recorder()
    vcs = Vcs(ScopeGuard.create({"api": str(tmp_path)}), runner=run)
    with pytest.raises(ScopeViolation):
        vcs.git("billing", "status")
    assert not calls


def test_no_module_runs_git_or_glab_outside_run_vcs():
    """Only vcs.py may start a process for git/glab; everything else goes through run_vcs (and so through rtk)."""
    spawners = {"run", "Popen", "call", "check_call", "check_output", "system", "popen", "create_subprocess_exec", "create_subprocess_shell"}

    def mentions_vcs(node) -> bool:
        for n in ast.walk(node):
            if isinstance(n, ast.Constant) and isinstance(n.value, str):
                v = n.value.strip()
                if v in ("git", "glab") or v.startswith(("git ", "glab ")):
                    return True
        return False

    offenders = []
    for py in SRC.rglob("*.py"):
        if py.name == "vcs.py":
            continue
        for n in ast.walk(ast.parse(py.read_text())):
            receiver = getattr(getattr(n, "func", None), "value", None)
            if isinstance(receiver, ast.Name) and receiver.id in ("vcs", "self"):
                continue  # Vcs.run(): the rtk-prefixed run_vcs entry point
            if isinstance(n, ast.Call) and getattr(n.func, "attr", getattr(n.func, "id", "")) in spawners:
                if any(mentions_vcs(a) for a in [*n.args, *[k.value for k in n.keywords]]):
                    offenders.append(f"{py.relative_to(SRC)}:{n.lineno}")
    assert not offenders, offenders


def test_scope_guard_is_frozen():
    guard = ScopeGuard.create({"api": "/w/api"})
    with pytest.raises(TypeError):
        guard.repos["web"] = "/w/web"  # type: ignore[index]
    assert guard.contains_path("/w/api/src/x.py") and not guard.contains_path("/w/api2/x")


@pytest.mark.parametrize("name", ["confluence_create_page", "confluence_update_page", "confluence_delete_page",
                                  "confluence_add_label", "confluence_add_comment", "mcp__atlassian__confluence_move_page"])
def test_confluence_write_tools_detected(name):
    assert is_write_tool(name)


@pytest.mark.parametrize("name", ["confluence_get_page", "confluence_get_page_children", "confluence_search", "confluence_get_attachments"])
def test_confluence_read_tools_allowed(name):
    assert not is_write_tool(name)


def test_confluence_reader_rejects_write_tool_in_allow_list():
    with pytest.raises(ConfluenceWriteBlocked):
        ConfluenceReader(mcp=None, read_tools={"get_page": "confluence_update_page"})  # type: ignore[arg-type]


def test_confluence_reader_exposes_no_write_method():
    public = {m for m in dir(ConfluenceReader) if not m.startswith("_")}
    assert public <= {"get_page", "get_children", "search", "missing_tools", "allowed_tools"}


@pytest.mark.parametrize("tool,inp,ok", [
    ("Bash", {"command": "git status"}, False),
    ("Bash", {"command": "npm test && git commit -m x"}, False),
    ("Bash", {"command": "rtk git status"}, True),
    ("Bash", {"command": "rtk git diff HEAD"}, True),
    ("Bash", {"command": "rtk git commit -m x"}, False),
    ("Bash", {"command": "rtk git push"}, False),
    ("Bash", {"command": "rtk glab mr list"}, False),
    ("Bash", {"command": "cat /w/billing/secrets.env"}, False),
    ("Bash", {"command": "npm run lint"}, True),
    ("Edit", {"file_path": "/w/api/src/a.ts"}, True),
    ("Edit", {"file_path": "/w/web/src/a.ts"}, False),
    ("Read", {"file_path": "/w/billing/a.ts"}, False),
    ("mcp__atlassian__confluence_update_page", {}, False),
    ("mcp__atlassian__jira_transition_issue", {}, False),
    ("mcp__atlassian__confluence_get_page", {}, True),
    ("mcp__playwright__browser_click", {}, True),
])
def test_coding_agent_tool_policy(tool, inp, ok):
    assert tool_policy(tool, inp, ["/w/api"])[0] is ok


def test_read_only_steps_cannot_edit():
    assert tool_policy("Edit", {"file_path": "/w/api/a"}, ["/w/api"], read_only=True)[0] is False
    assert tool_policy("Read", {"file_path": "/w/api/a"}, ["/w/api"], read_only=True)[0] is True


def test_dag_waves_and_validation():
    nodes = ["shared", "api", "web", "batch"]
    edges = [("shared", "api"), ("api", "web")]
    assert waves(nodes, edges) == [["batch", "shared"], ["api"], ["web"]]
    assert downstream_closure("shared", edges) == ["api", "web"]
    assert validate(["a", "b"], [("a", "b"), ("b", "a")], {"a", "b"})
    assert validate(["a", "x"], [], {"a"}) == ["repo 'x' is not in scope"]
    with pytest.raises(DagError):
        waves(["a", "b"], [("a", "b"), ("b", "a")])
