"""Provided: do not edit. out = {rule: [row ids]} for rules duplicate_id, negative_age, bad_email, missing_name, future_signup.
Passes only with every injected row found and no clean row flagged (a validator that cries wolf gets switched off)."""


def check(out, expect):
    problems = []
    for rule, want in expect.items():
        got = set(out.get(rule, []))
        if got != set(want):
            problems.append(f"{rule}: missed {sorted(set(want) - got)} flagged-clean {sorted(got - set(want))[:5]}")
    return (0.0 if problems else 1.0), "; ".join(problems) or "all five rules exact"
