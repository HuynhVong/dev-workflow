"""Per-step models, fixed medium effort, and global Claude Code skills picked at run time."""
import ast
import json
from pathlib import Path

import anthropic
import httpx2
from pydantic import BaseModel

from dev_workflows.config import Settings
from dev_workflows.jira_implement.coding_agent import tool_policy
from dev_workflows.llm import ClaudeLLM
from dev_workflows.routing import MODELS, STEPS, Routing, SkillRegistry, repo_stack, setup_report

SRC = Path(__file__).resolve().parents[1] / "src" / "dev_workflows"


def skill(root: Path, name: str, description: str, body: str = "Do it well.") -> Path:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n{body}\n")
    return d


def test_default_models_follow_the_table():
    r = Routing(registry=SkillRegistry([]))
    expect = {"gather_context": "haiku", "analyze_requirements": "sonnet", "change_impact": "sonnet", "discover_repos": "haiku",
              "plan_implementation": "opus", "implement": "sonnet", "targeted_fix": "sonnet", "analyze_feedback_and_route": "sonnet",
              "integration_check": "sonnet", "repo_review": "sonnet", "contract_review": "opus", "summary": "haiku"}
    for step, tier in expect.items():
        assert r.model(step) == MODELS[tier], step
    assert r.model("targeted_fix", escalate=True) == MODELS["opus"]


def test_effort_is_always_medium():
    assert Settings().effort == "medium"
    r = Routing(registry=SkillRegistry([]))
    assert {r.effort(r.model(s)) for s in STEPS} <= {"medium", None}
    assert r.effort(MODELS["sonnet"]) == r.effort(MODELS["opus"]) == "medium"
    assert r.effort(MODELS["haiku"]) is None  # Haiku 4.5 has no effort parameter


def test_every_ai_call_names_a_routed_step():
    """No LLM or Claude Code call can fall back to an unrouted default model."""
    bad = []
    for f in SRC.rglob("*.py"):
        for n in ast.walk(ast.parse(f.read_text())):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in ("structured", "implement", "explore", "verify"):
                if f.name == "llm.py" or (isinstance(n.func.value, ast.Name) and n.func.value.id == "self"):
                    continue
                step = next((k.value for k in n.keywords if k.arg == "step"), None)
                if not (isinstance(step, ast.Constant) and step.value in STEPS) and not (n.func.attr == "implement" and isinstance(step, ast.IfExp)):
                    bad.append(f"{f.name}:{n.lineno} {n.func.attr}")
    assert not bad, bad


def test_registry_finds_global_and_plugin_skills_and_picks_by_stack(tmp_path):
    skill(tmp_path / "skills", "planning", "Plan multi-repo work")
    skill(tmp_path / "plugins" / "acme" / "skills", "spring-boot-coding", "Coding conventions for Java Spring services")
    skill(tmp_path / "skills", "react-coding", "React + TypeScript component conventions")
    reg = SkillRegistry([tmp_path / "skills", tmp_path / "plugins"])
    assert set(reg.skills) == {"planning", "spring-boot-coding", "react-coding"}
    repo = tmp_path / "api"
    repo.mkdir()
    (repo / "pom.xml").write_text("<project/>")
    assert "java" in repo_stack(str(repo))
    r = Routing(registry=reg, domain_skills=("insurance-domain",))
    assert [s.name for s in r.skills("implement", repo_path=str(repo))] == ["spring-boot-coding"]
    assert [s.name for s in r.skills("plan_implementation")] == ["planning"]  # missing domain skill is skipped
    assert r.missing()["plan_implementation"] == ["insurance-domain"]


def test_workspace_overrides_models_and_skills(tmp_path):
    skill(tmp_path, "team-planning", "Our planning template")
    r = Routing.from_config({"models": {"opus": "claude-opus-5"}, "steps": {"plan_implementation": {"skills": ["team-planning"]},
                                                                              "summary": {"model": "sonnet"}}},
                            registry=SkillRegistry([tmp_path]))
    assert r.model("plan_implementation") == "claude-opus-5" and r.model("summary") == MODELS["sonnet"]
    assert [s.name for s in r.skills("plan_implementation")] == ["team-planning"]


def test_api_call_uses_the_step_model_medium_effort_and_skill_text(tmp_path):
    skill(tmp_path, "planning", "Plan work", body="Always plan in dependency waves.")
    sent = []

    def handler(req):
        sent.append(json.loads(req.content))
        return httpx2.Response(200, json={"id": "m", "type": "message", "role": "assistant", "model": "x", "stop_reason": "end_turn",
                                          "stop_sequence": None, "content": [{"type": "text", "text": '{"a": 1}'}],
                                          "usage": {"input_tokens": 1, "output_tokens": 1}})

    class Out(BaseModel):
        a: int

    client = anthropic.Anthropic(api_key="k", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)))
    llm = ClaudeLLM(client=client, routing=Routing(registry=SkillRegistry([tmp_path])))
    llm.structured("sys", "hi", Out, step="plan_implementation")
    llm.structured("sys", "hi", Out, step="summary")
    assert sent[0]["model"] == MODELS["opus"] and sent[0]["output_config"]["effort"] == "medium"
    assert "Always plan in dependency waves." in sent[0]["system"]
    assert sent[1]["model"] == MODELS["haiku"] and "effort" not in sent[1].get("output_config", {})


def test_coding_agent_may_read_skills_but_never_edit_them(tmp_path):
    d = skill(tmp_path, "react-coding", "React")
    roots, skills = ["/w/web"], [str(tmp_path)]
    assert tool_policy("Read", {"file_path": str(d / "SKILL.md")}, roots, read_roots=skills)[0]
    assert not tool_policy("Edit", {"file_path": str(d / "SKILL.md")}, roots, read_roots=skills)[0]
    assert not tool_policy("Read", {"file_path": str(d / "SKILL.md")}, roots)[0]


def test_setup_report_lists_what_to_install(tmp_path):
    skill(tmp_path, "planning", "Plan work")
    report, ok = setup_report(Routing(registry=SkillRegistry([tmp_path])))
    assert not ok and "✓ planning" in report and "  - code-review" in report
