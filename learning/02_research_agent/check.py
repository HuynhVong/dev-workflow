"""Provided: do not edit. out = {"answer": str, "citations": [file names], "steps": int}."""
ABSTAIN = ("don't know", "do not know", "not found", "no information", "cannot find", "can't find", "not mentioned", "no mention", "not in the")
MAX_STEPS = 8


def check(out, expect):
    ans = str(out.get("answer", "")).lower().replace(",", "").replace("×", "x")
    cites = {c.lower() for c in out.get("citations", [])}
    problems = []
    if expect.get("abstain"):
        if not any(a in ans for a in ABSTAIN):
            problems.append("should say it cannot find this in the docs")
        if cites:
            problems.append(f"cited {sorted(cites)} for an unanswerable question")
    else:
        problems += [f"missing {s!r}" for s in expect.get("contains", []) if s.lower() not in ans]
        problems += [f"did not cite {c}" for c in expect.get("cites", []) if c.lower() not in cites]
    if out.get("steps", 0) > MAX_STEPS:
        problems.append(f"{out['steps']} steps (max {MAX_STEPS})")
    return (0.0 if problems else 1.0), "; ".join(problems) or "ok"
