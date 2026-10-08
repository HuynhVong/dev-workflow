"""Provided: do not edit. out = the Invoice (or a dict) your solve() returns."""


def check(out, expect):
    got = out.model_dump() if hasattr(out, "model_dump") else dict(out)
    wrong = []
    for k, want in expect.items():
        v = got.get(k)
        ok = (v is None) if want is None else abs(float(v) - want) < 0.01 if k == "total" and v is not None else v == want
        if not ok:
            wrong.append(f"{k}: got {v!r}, want {want!r}")
    return (1.0, "all fields right") if not wrong else (1 - len(wrong) / len(expect), "; ".join(wrong))
