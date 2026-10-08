"""Provided: do not edit. out = list of findings: [{"file": str, "line": int|None, "title": str, "detail": str}].
A planted bug counts as found when a finding in that file mentions one of its keywords. Extra findings beyond one
are false positives and fail the case: a reviewer that cries wolf is ignored."""


def check(out, expect):
    findings = [f if isinstance(f, dict) else f.model_dump() for f in out]
    bugs, used, missed = expect["bugs"], set(), []
    for bug in bugs:
        hit = next((i for i, f in enumerate(findings) if i not in used and f.get("file", "").endswith(bug["file"])
                    and any(k in f"{f.get('title', '')} {f.get('detail', '')}".lower() for k in bug["keywords"])), None)
        if hit is None:
            missed.append(bug["file"] + ":" + bug["keywords"][0])
        else:
            used.add(hit)
    extra = len(findings) - len(used)
    problems = [f"missed {m}" for m in missed] + ([f"{extra} extra finding(s)"] if extra > (1 if bugs else 0) else [])
    return (0.0 if problems else 1.0), "; ".join(problems) or f"found {len(used)}/{len(bugs)}, {extra} extra"
