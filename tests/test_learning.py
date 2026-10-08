"""Keeps the learning/ exercises honest: checkers accept the answer key and reject garbage, references and tools work offline."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from learning.evalkit import evaluate, load_cases  # noqa: E402
from learning.llm import Counted, ScriptedModel  # noqa: E402
from learning.run import exercise_dir, load  # noqa: E402

STEPS = ["01", "02", "03", "04", "05", "06", "07"]
GARBAGE = {"01": {}, "02": {"answer": "", "citations": [], "steps": 1}, "03": [{"file": "x.py", "title": "style", "detail": "nit"}] * 3,
           "04": "nonsense", "05": "", "06": {}, "07": {"answer": ""}}


def oracle(step, case):
    """The right output for a case, built from its answer key."""
    e = case["expect"]
    if step == "01":
        return e
    if step == "02":
        if e.get("abstain"):
            return {"answer": "I cannot find that in the docs.", "citations": [], "steps": 2}
        return {"answer": " ".join(e["contains"]), "citations": e["cites"], "steps": 3}
    if step == "03":
        return [{"file": b["file"], "line": 1, "title": b["keywords"][0], "detail": ""} for b in e["bugs"]]
    if step == "04":
        return e
    if step == "05":
        return " ".join(e.get("contains", []))
    if step == "06":
        return e
    return {"answer": " ".join(e["contains"]), "calls": 3, "chars": 100}


@pytest.mark.parametrize("step", STEPS)
def test_checker_accepts_the_answer_key_and_rejects_garbage(step):
    d = exercise_dir(step)
    check = load(d / "check.py", f"check_{step}").check
    cases = load_cases(d / "cases.jsonl")
    assert len(cases) >= 1
    for c in cases:
        score, detail = check(oracle(step, c), c["expect"])
        assert score == 1.0, f"{c['id']}: the answer key itself fails: {detail}"
    bad = [c for c in cases if check(GARBAGE[step], c["expect"])[0] < 1]
    assert bad, f"exercise {step}: garbage passes every case, the checker is too lenient"


def test_unimplemented_starters_report_it_instead_of_crashing():
    for step in STEPS:
        d = exercise_dir(step)
        r = evaluate(load(d / "starter.py", f"starter_{step}").solve, load_cases(d / "cases.jsonl")[:1], load(d / "check.py", f"c{step}").check, show=False)
        assert r.get("not_implemented") or step == "05", f"{step} should start unimplemented"


def test_tools_search_read_and_calculate_safely():
    T = load(exercise_dir("02") / "tools.py", "tools_t")
    assert "pricing.md" in T.search("Pro price per seat")
    assert T.calculator("3*20*12*0.85") == "612.0"
    assert T.calculator("__import__('os').system('x')").startswith("error")
    assert T.calculator("1/0").startswith("error") and T.read_file("../../etc/passwd").startswith("error")


def test_reference_agent_loop_uses_tools_survives_errors_and_stops():
    d = exercise_dir("02")
    T, ref = load(d / "tools.py", "tools_r"), load(d / "reference.py", "ref_r")
    script = [{"tool_calls": [{"id": "1", "name": "search", "args": {"query": "Pro price seat"}}]},
              {"tool_calls": [{"id": "2", "name": "calculator", "args": {"expression": "3 * 20 * 12 * 0.85 /"}}]},  # a bad call: error text goes back
              {"tool_calls": [{"id": "3", "name": "calculator", "args": {"expression": "3*20*12*0.85"}}]},
              {"text": "612 dollars per year.\nSources: pricing.md"}]
    model = ScriptedModel(script)
    out = ref.run_agent("3 Pro seats, annual?", model, T.FUNCS, T.SPECS)
    assert out == {"answer": "612 dollars per year.", "citations": ["pricing.md"], "steps": 4}
    assert model.calls[2]["messages"][-1]["content"].startswith("error")
    loop = ScriptedModel([{"tool_calls": [{"id": str(i), "name": "search", "args": {"query": "x"}}]} for i in range(20)])
    assert ref.run_agent("q", loop, T.FUNCS, T.SPECS, max_steps=3)["steps"] == 4  # gave up at the limit
    check = load(d / "check.py", "check_r").check
    assert check(out, {"contains": ["612"], "cites": ["pricing.md"]})[0] == 1.0


def test_skill_library_catalog_is_much_smaller_than_the_skills_and_the_keyword_router_has_a_gap():
    d = exercise_dir("04")
    ref = load(d / "reference.py", "skills_ref")
    lib = ref.SkillLibrary(d / "skills")
    assert set(lib.skills) == {"sql-help", "hr-policy", "unit-conversion"}
    assert len(lib.catalog()) < sum(len(s["body"]) + len(s["description"]) for s in lib.skills.values()) * 2
    assert "20 vacation days" in lib.load("hr-policy") and "20 vacation days" not in lib.catalog()
    r = evaluate(lib.route, load_cases(d / "cases.jsonl"), load(d / "check.py", "check_s").check, show=False)
    assert r["passed"] == 8 and r["total"] == 10  # fails exactly the two paraphrased cases


def test_memory_baseline_passes_four_of_five():
    d = exercise_dir("05")
    ref = load(d / "reference.py", "mem_ref")

    def solve(case):
        store = ref.MemoryStore()
        for s in case["sessions"]:
            store.add_session(s)
        return store.context_for(case["question"], case["budget_chars"])
    r = evaluate(solve, load_cases(d / "cases.jsonl"), load(d / "check.py", "check_m").check, show=False)
    assert [x["id"] for x in r["rows"] if x["score"] < 1] == ["newer-fact-wins"]


def test_data_quality_baseline_is_exact():
    d = exercise_dir("06")
    ref = load(d / "reference.py", "dq_ref")
    r = evaluate(lambda name: ref.find_issues(str(d / name)), load_cases(d / "cases.jsonl"), load(d / "check.py", "check_d").check, show=False)
    assert r["passed"] == 1, r["rows"]


def test_counted_model_counts_calls_and_chars():
    m = Counted(ScriptedModel([{"text": "a"}, {"text": "b"}]))
    m("sys", [{"role": "user", "content": "hello"}], [])
    m("sys", [{"role": "user", "content": "hi"}], [])
    assert (m.calls, m.chars) == (2, 3 + 5 + 3 + 2)
