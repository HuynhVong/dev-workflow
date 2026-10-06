"""Freely Implement (no Jira) and the mockups/notes a developer attaches to a run."""
import base64
from dataclasses import replace

import pytest

from dev_workflows.jira_implement import task_inputs
from dev_workflows.jira_implement.free_implement import WORKFLOW as FREE
from dev_workflows.jira_implement.graph import WORKFLOW as JIRA
from dev_workflows.jira_implement.models import FeedbackAnalysis, FeedbackItem
from dev_workflows.jira_implement.runner import RunConflict

from .harness import FakeCoder, FakeLLM, glab_db, make_env, sh, wt
from .test_runner import session

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 64).decode()
VALUES = {"description": "Export orders as CSV\nWith the active filters.", "branch": "feature/export-orders", "ref": "AQS-9",
          "repos": ["api"], "note": "The button must be blue"}


def image(tmp_path, name="mock.png"):
    p = tmp_path / name
    p.write_bytes(base64.b64decode(PNG))
    return str(p)


def pending(s, run_id):
    return s.status(run_id)["pending_checkpoint"]


def test_free_run_goes_plan_code_review_manual_test_push_without_touching_jira(tmp_path):
    ws, env = make_env(tmp_path, repos=("api",))
    ws.repos["api"] = replace(ws.repos["api"], has_ui=True)
    ws.mcp_servers["playwright"] = {"command": "npx", "args": ["@playwright/mcp@latest"]}  # a UI repo needs one at preflight
    fb = FeedbackAnalysis(items=[FeedbackItem(source="developer", category="review_blocker", repos=["api"], cause="label",
                                              confidence="high", fix_instructions="rename the button", contract_changed=False)])
    llm, coder = FakeLLM(["api"], [], overrides={FeedbackAnalysis: [fb]}), FakeCoder()
    s, out, mcp = session(tmp_path, ws, env, llm, coder)
    run_id, status = s.start_run(FREE, {**VALUES, "images": [image(tmp_path)]})
    assert status == "WAITING_HUMAN" and pending(s, run_id) == "approve_plan"
    assert run_id.startswith("feature-export-orders-free-")
    assert llm.steps[:4] == ["mockup_brief", "analyze_requirements", "change_impact", "plan_implementation"]  # no Jira/Confluence step
    assert len(llm.images[0]) == 1
    assert "<developer_notes" in llm.calls[1][1] and "<design_brief" in llm.calls[1][1]  # the analysis sees notes and brief
    s.answer(run_id, {"choice": "approve"})
    # the branch is the developer's own, cut from develop, in a worktree keyed by its slug
    path = wt(ws, "api", key="feature-export-orders")
    assert sh("git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD") == "feature/export-orders"
    # review first, then the developer approves the code
    assert pending(s, run_id) == "approve_code"
    impl = [c[2] for c in coder.calls if c[0] == "implement"][0]
    assert "<mockup_files>" in impl and "The button must be blue" in impl and "Orders table" in impl  # UI repo: brief + mockup files
    s.answer(run_id, {"choice": "changes", "note": "rename the button to Export CSV"})
    assert pending(s, run_id) == "approve_code"  # fixed, reviewed again, asks again
    assert [st for st, _, _ in coder.steps] == ["implement", "targeted_fix"]
    s.answer(run_id, {"choice": "approve"})
    assert pending(s, run_id) == "manual_test"
    payload = s._pending(s.graph(run_id).get_state(s.cfg(run_id)))["payload"]
    assert payload["test_cases"]["cases"][0]["title"] == "Export respects filters" and payload["mockups"]
    assert any("matches the mockup" in c or "mockup" in c for c in payload["checklist"])
    assert llm.steps.count("manual_test_cases") == 1
    s.answer(run_id, {"choice": "ok"})
    assert pending(s, run_id) == "approve_push"
    assert s.answer(run_id, {"choice": "approve"}) == "COMPLETED"
    mr = glab_db(tmp_path)["api"][0]
    assert mr["source"] == "feature/export-orders" and mr["draft"] and "Task: Export orders as CSV (AQS-9)" in mr["description"]
    subject = sh("git", "--git-dir", str(tmp_path / "origin/api.git"), "log", "-1", "--format=%s", "feature/export-orders")
    assert subject == "AQS-9: Export orders as CSV"
    assert mcp.calls == []  # not one Jira or Confluence call


def test_free_run_validates_what_the_developer_gives(tmp_path):
    ws, env = make_env(tmp_path, repos=("api", "web"))
    s, _, _ = session(tmp_path, ws, env, FakeLLM(["api"], []), FakeCoder())
    bad = [({**VALUES, "branch": "develop"}, "long-lived"), ({**VALUES, "branch": "has space"}, "not a valid"),
           ({**VALUES, "repos": []}, "at least one repo"), ({**VALUES, "repos": ["nope"]}, "not in workspace.yaml"),
           ({**VALUES, "description": " "}, "describe what to implement"), ({**VALUES, "images": ["/no/such.png"]}, "image not found")]
    for values, text in bad:
        with pytest.raises(ValueError, match=text):
            s.create(FREE, values)
    run_id, status = s.start_run(FREE, VALUES)
    with pytest.raises(RunConflict):  # one active run per branch
        s.create(FREE, VALUES)
    ws2 = replace(ws, branch_prefix="feature/")
    s2, _, _ = session(tmp_path, ws2, env, FakeLLM(["api"], []), FakeCoder())
    with pytest.raises(ValueError, match="start with 'feature/'"):
        s2.create(FREE, {**VALUES, "branch": "fix/x"})


def test_free_run_without_images_or_notes_makes_no_vision_call(tmp_path):
    ws, env = make_env(tmp_path, repos=("api",))
    llm = FakeLLM(["api"], [])
    s, _, _ = session(tmp_path, ws, env, llm, FakeCoder())
    s.start_run(FREE, {k: v for k, v in VALUES.items() if k not in ("note", "ref")})
    assert "mockup_brief" not in llm.steps and llm.steps[0] == "analyze_requirements"


def test_jira_run_takes_images_and_notes_and_reads_them_once(tmp_path):
    ws, env = make_env(tmp_path, repos=("api",))
    llm = FakeLLM(["api"], [])
    s, _, _ = session(tmp_path, ws, env, llm, FakeCoder())
    run_id, status = s.start_run(JIRA, {"ticket": "AQS-1", "repos": ["api"], "images": [image(tmp_path)], "note": "Match the mockup"})
    assert status == "WAITING_HUMAN"
    assert llm.steps[:3] == ["mockup_brief", "gather_context", "analyze_requirements"] and llm.steps.count("mockup_brief") == 1
    assert len(llm.images[0]) == 1
    analysis_prompt = llm.calls[llm.steps.index("analyze_requirements")][1]
    assert "Match the mockup" in analysis_prompt and "Orders table" in analysis_prompt
    inputs = s.store.inputs(run_id)
    assert inputs["dev_notes"] == "Match the mockup" and inputs["dev_images"][0].endswith("01-mock.png")


def test_upload_adopt_limits_and_cleanup(tmp_path):
    ws, _ = make_env(tmp_path, repos=("api",))
    up = task_inputs.save_upload(ws, "../../etc/shot one.PNG", "data:image/png;base64," + PNG)
    assert "/" not in up["name"] and up["name"].lower().endswith(".png")
    paths = task_inputs.adopt_images(ws, "run-1", [up["id"]])
    assert paths[0].endswith("01-" + up["name"]) and "run-1/inputs" in paths[0]
    assert not (task_inputs.uploads_dir(ws) / up["id"]).exists()  # staging cleaned
    with pytest.raises(task_inputs.InputError, match="only"):
        task_inputs.save_upload(ws, "notes.txt", PNG)
    with pytest.raises(task_inputs.InputError, match="limit"):
        task_inputs.save_upload(ws, "big.png", base64.b64encode(b"x" * (task_inputs.MAX_BYTES + 1)).decode())
    with pytest.raises(task_inputs.InputError, match="at most"):
        task_inputs.adopt_images(ws, "run-2", [image(tmp_path)] * (task_inputs.MAX_IMAGES + 1))
    assert task_inputs.slug("Feature/Add_Export v2") == "feature-add_export-v2" and task_inputs.title_of("\n## Add export\nx") == "Add export"
