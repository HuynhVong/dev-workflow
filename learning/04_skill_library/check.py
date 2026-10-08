"""Provided: do not edit. out = the name of the skill to load, or None."""


def check(out, expect):
    return (1.0, "ok") if out == expect else (0.0, f"routed to {out!r}, wanted {expect!r}")
