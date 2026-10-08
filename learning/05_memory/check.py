"""Provided: do not edit. out = the memory context string your store would put in the prompt for the question."""


def check(out, expect):
    ctx, problems = str(out), []
    problems += [f"missing {s!r}" for s in expect.get("contains", []) if s.lower() not in ctx.lower()]
    problems += [f"still contains {s!r}" for s in expect.get("absent", []) if s.lower() in ctx.lower()]
    if "max_chars" in expect and len(ctx) > expect["max_chars"]:
        problems.append(f"{len(ctx)} chars, over the {expect['max_chars']} budget")
    return (0.0 if problems else 1.0), "; ".join(problems) or f"ok ({len(ctx)} chars)"
