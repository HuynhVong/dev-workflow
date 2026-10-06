import json
import subprocess

import pytest
from pydantic import BaseModel

from dev_workflows import doctor
from dev_workflows.config import Settings, llm_backend
from dev_workflows.llm import ClaudeCliLLM, ClaudeLLM, LLMRefusal, make_llm
from dev_workflows.routing import MODELS, Routing, SkillRegistry


class Out(BaseModel):
    a: int


def fake_run(payload: dict | str, calls: list):
    def run(cmd, **kw):
        calls.append((cmd, kw))
        out = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
    return run


OK = {"type": "result", "subtype": "success", "is_error": False, "stop_reason": "end_turn", "structured_output": {"a": 3},
      "usage": {"input_tokens": 5, "output_tokens": 2}, "modelUsage": {"claude-opus-5-5": {}}}


def test_auto_backend_uses_the_api_only_when_a_key_is_set():
    assert llm_backend(Settings(backend="auto"), env={"ANTHROPIC_API_KEY": "k"}) == "api"
    assert llm_backend(Settings(backend="auto"), env={}) == "claude-cli"
    assert llm_backend(Settings(backend="claude-cli"), env={"ANTHROPIC_API_KEY": "k"}) == "claude-cli"
    assert llm_backend(Settings(backend="api"), env={}) == "api"


def test_make_llm_picks_the_backend(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert isinstance(make_llm(cfg=Settings(backend="auto")), ClaudeCliLLM)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert isinstance(make_llm(cfg=Settings(backend="auto")), ClaudeLLM)


def test_cli_call_passes_schema_model_effort_skills_and_drops_the_api_key(tmp_path, monkeypatch):
    (tmp_path / "planning").mkdir()
    (tmp_path / "planning" / "SKILL.md").write_text("---\nname: planning\ndescription: Plan work\n---\nAlways plan in dependency waves.\n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret")
    calls = []
    llm = ClaudeCliLLM(routing=Routing(registry=SkillRegistry([tmp_path])), run=fake_run(OK, calls))
    assert llm.structured("sys", "the prompt", Out, step="plan_implementation") == Out(a=3)
    cmd, kw = calls[0]
    arg = lambda flag: cmd[cmd.index(flag) + 1]  # noqa: E731
    assert cmd[:2] == ["claude", "-p"] and json.loads(arg("--json-schema"))["properties"]["a"]["type"] == "integer"
    assert arg("--model") == MODELS["opus"] and arg("--effort") == "medium" and arg("--tools") == ""
    assert "Always plan in dependency waves." in arg("--system-prompt")
    assert kw["input"] == "the prompt" and "ANTHROPIC_API_KEY" not in kw["env"]

    llm.structured("sys", "hi", Out, step="summary")
    assert "--effort" not in calls[1][0] and calls[1][0][calls[1][0].index("--model") + 1] == MODELS["haiku"]


def test_cli_images_are_read_with_the_read_tool(tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(b"png")
    calls = []
    ClaudeCliLLM(run=fake_run(OK, calls)).structured("sys", "look", Out, images=[str(img), str(tmp_path / "notes.txt")])
    cmd, kw = calls[0]
    assert cmd[cmd.index("--tools") + 1] == "Read" and str(img) in kw["input"] and "notes.txt" not in kw["input"]
    assert cmd[cmd.index("--allowedTools") + 1] == "Read" and cmd[cmd.index("--add-dir") + 1] == str(tmp_path.resolve())


@pytest.mark.parametrize("payload, error", [
    ({**OK, "is_error": True, "subtype": "error_during_execution", "result": "usage limit reached"}, "usage limit reached"),
    ({**OK, "structured_output": None}, "No structured output"),
    ("Not logged in. Run claude to log in.", "Not logged in"),
])
def test_cli_failures_are_reported(payload, error):
    with pytest.raises(RuntimeError, match=error):
        ClaudeCliLLM(run=fake_run(payload, [])).structured("sys", "hi", Out)


def test_cli_refusal_is_an_llm_refusal():
    with pytest.raises(LLMRefusal):
        ClaudeCliLLM(run=fake_run({**OK, "stop_reason": "refusal", "structured_output": None}, [])).structured("s", "p", Out)


def test_doctor_accepts_the_claude_code_login_without_a_key(monkeypatch):
    monkeypatch.setattr("dev_workflows.config.settings", Settings(backend="auto"))
    [c] = doctor.ai_checks(env={}, which=lambda b: "/usr/bin/claude")
    assert c.status == "ok" and "Claude Code" in c.label
    [c] = doctor.ai_checks(env={}, which=lambda b: None)
    assert c.status == "fail" and c.blocking
    [c] = doctor.ai_checks(env={"ANTHROPIC_API_KEY": "k"}, which=lambda b: None)
    assert c.status == "ok" and c.label == "ANTHROPIC_API_KEY"
