"""A tiny eval harness. An exercise = cases (JSONL: id, input, expect) + your solve(input) + a check(out, expect).

    python -m learning.run 01            # run exercise 01 against its cases and print a table

`check` returns (score between 0 and 1, a short reason). A case passes at 1.0. Evaluating on every change is the habit
this folder is for: an agent you cannot measure is a demo, not a system."""
import json
import time
from pathlib import Path


def load_cases(path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def evaluate(solve, cases: list[dict], check, show: bool = True) -> dict:
    rows = []
    for c in cases:
        started, detail = time.time(), ""
        try:
            score, detail = check(solve(c["input"]), c["expect"])
        except NotImplementedError as e:
            print(f"\nNot implemented yet: {e}\nOpen the exercise's starter.py and fill in the TODOs.")
            return {"passed": 0, "total": len(cases), "rate": 0.0, "rows": [], "not_implemented": True}
        except Exception as e:  # noqa: BLE001 - a crash is a failed case, not the end of the run
            score, detail = 0.0, f"{type(e).__name__}: {e}"
        rows.append({"id": c["id"], "score": score, "detail": detail, "s": round(time.time() - started, 1)})
    passed = sum(1 for r in rows if r["score"] >= 1)
    result = {"passed": passed, "total": len(rows), "rate": passed / max(len(rows), 1), "rows": rows}
    if show:
        print(f"\n{'case':<24}{'score':>7}  {'sec':>5}  detail")
        for r in rows:
            print(f"{r['id']:<24}{r['score']:>7.2f}  {r['s']:>5}  {r['detail'][:90]}")
        print(f"\npassed {passed}/{len(rows)} ({result['rate']:.0%})")
    return result
