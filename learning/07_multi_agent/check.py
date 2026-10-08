"""Provided: do not edit. out = {"answer": str, "calls": int, "chars": int}. Correctness only; the cost goes in the table."""


def check(out, expect):
    ans = str(out.get("answer", "")).lower().replace(",", "")
    missing = [s for s in expect["contains"] if s.lower() not in ans]
    cost = f"{out.get('calls', '?')} model calls, {out.get('chars', '?')} chars sent"
    return (0.0, f"missing {missing}; {cost}") if missing else (1.0, cost)
