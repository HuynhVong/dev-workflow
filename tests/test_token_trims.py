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


def test_diffs_block_is_raw_text_per_repo_and_digested():
    from dev_workflows.textutil import diffs_block
    small = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n+one \"quoted\"\n"
    big = "diff --git a/big.sql b/big.sql\n--- a/big.sql\n+++ b/big.sql\n" + "+row\n" * 5000
    out = diffs_block({"api": small, "web": big, "empty": ""}, 1500)
    assert "<diff repo='api'>" in out and '+one "quoted"' in out and "\\n" not in out  # not JSON-escaped
    assert "big.sql +5000/-0" in out and out.count("+row") == 0 and "<diff repo='empty'>\n(no changes)" in out


def test_pr_review_sends_the_diff_digested_to_triage_and_every_lens():
    from fake_llm import FakeLLM
    from dev_workflows.workflows import pr_review
    from dev_workflows.workflows.pr_review import LENS_DIFF, TRIAGE_DIFF

    files = "".join(f"diff --git a/f{i}.py b/f{i}.py\n--- a/f{i}.py\n+++ b/f{i}.py\n" + "+x = 1\n" * 1500 for i in range(12))
    seen = {}

    class Spy:
        def structured(self, system, prompt, schema, images=(), step=""):
            seen.setdefault(step, []).append(len(prompt))
            if schema is pr_review.Triage:
                return pr_review.Triage(summary="s", touches_frontend=False, risk="low", lenses=["correctness"])
            if schema is pr_review.LensReview:
                return pr_review.LensReview(findings=[])
            return pr_review.Verdict(decision="approve", summary="ok")

    pr_review.build_graph(Spy()).invoke({"title": "t", "description": "", "diff": files, "findings": []})
    assert len(files) > 100000
    assert max(seen["pr_review.triage"]) < TRIAGE_DIFF + 1500
    assert len(seen["pr_review.lens"]) == 2 and max(seen["pr_review.lens"]) < LENS_DIFF + 1500


def test_ticket_images_are_capped_and_oversized_ones_skipped(tmp_path):
    from dev_workflows.llm import MAX_IMAGES, usable_images
    paths = []
    for i in range(MAX_IMAGES + 4):
        p = tmp_path / f"s{i}.png"
        p.write_bytes(b"x")
        paths.append(str(p))
    big = tmp_path / "huge.png"
    big.write_bytes(b"x" * (5 * 1024 * 1024 + 1))
    (tmp_path / "notes.txt").write_text("x")
    got = usable_images([str(big), str(tmp_path / "notes.txt"), str(tmp_path / "missing.png"), *paths])
    assert got == paths[:MAX_IMAGES]
