"""Prompt trims that keep devflow's token use down: skills, Confluence pages, JSON, diffs, coding-agent MCP servers."""
from types import SimpleNamespace

from dev_workflows.jira_implement.confluence import compact_pages, html_to_text
from dev_workflows.jira_implement.graph import Deps, _j, diff_digest
from dev_workflows.routing import SKILL_BUDGET, Skill, skills_system_block


def test_skills_block_keeps_the_first_two_skills_within_the_budget(tmp_path):
    skills = []
    for name in ("planning", "playwright", "domain"):
        f = tmp_path / name / "SKILL.md"
        f.parent.mkdir()
        f.write_text(f"---\nname: {name}\n---\n{name} rule.\n" + "x" * 20000)
        skills.append(Skill(name, "", str(f)))
    block = skills_system_block(skills)
    assert "planning rule." in block and "playwright rule." in block and "domain" not in block
    assert len(block) < SKILL_BUDGET + 500
    assert skills_system_block([]) == ""


def test_confluence_pages_lose_markup_and_link_fields():
    page = {"id": "42", "title": "Rates", "_links": {"webui": "/x"}, "version": {"number": 7},
            "body": {"storage": {"value": "<h1>Rates</h1><p>Rate is <b>5%</b> &amp; fixed.</p>"
                                          "<table><tr><th>Plan</th><th>Rate</th></tr><tr><td>A</td><td>5</td></tr></table>"}}}
    out = compact_pages([{"id": "42", "page": page, "children": []}])
    assert out == [{"id": "42", "page": {"id": "42", "title": "Rates", "body": {"storage": {"value": out[0]["page"]["body"]["storage"]["value"]}}}}]
    text = out[0]["page"]["body"]["storage"]["value"]
    assert "Rate is 5% & fixed." in text and "Plan | Rate |" in text and "<" not in text
    assert html_to_text("plain text") == "plain text"


def test_prompt_json_is_compact():
    assert _j({"a": [1, 2], "b": "é"}) == '{"a":[1,2],"b":"é"}'


def test_diff_digest_lists_every_file_and_keeps_whole_files_that_fit():
    small = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n+one\n-two\n"
    big = "diff --git a/big.sql b/big.sql\n--- a/big.sql\n+++ b/big.sql\n" + "+row\n" * 5000
    assert diff_digest(small, 1000) == small
    out = diff_digest(small + big, 2000)
    assert len(out) < 2300 and "a.py +1/-1" in out and "big.sql +5000/-0" in out
    assert small in out and "+row\n+row" not in out and "big.sql left out" in out


def test_coding_agent_loads_only_its_own_mcp_servers_unless_playwright_is_account_level():
    deps = lambda cc: Deps(workspace=SimpleNamespace(claude_code_mcp=cc), store=None, llm=None, jira=None, confluence=None)
    assert deps({})._strict_mcp({}) is True
    assert deps({"playwright": "pw"})._strict_mcp({"pw": {"command": "npx"}}) is True
    assert deps({"playwright": "claude.ai Playwright"})._strict_mcp({}) is False
