"""Repo dependency DAG: validation, waves (parallel only within a wave), merge order, dependents."""


class DagError(ValueError):
    pass


def validate(nodes: list[str], edges: list[tuple[str, str]], scope: set[str]) -> list[str]:
    errors = [f"repo '{n}' is not in scope" for n in nodes if n not in scope]
    errors += [f"edge {u} -> {d} references a repo not in the plan" for u, d in edges if u not in nodes or d not in nodes]
    errors += [f"self-dependency on {u}" for u, d in edges if u == d]
    if not errors:
        try:
            waves(nodes, edges)
        except DagError as e:
            errors.append(str(e))
    return errors


def waves(nodes: list[str], edges: list[tuple[str, str]]) -> list[list[str]]:
    indeg = {n: 0 for n in nodes}
    for _, d in edges:
        indeg[d] += 1
    out: list[list[str]] = []
    remaining = dict(indeg)
    while remaining:
        wave = sorted(n for n, k in remaining.items() if k == 0)
        if not wave:
            raise DagError(f"dependency cycle among {sorted(remaining)}")
        out.append(wave)
        for n in wave:
            del remaining[n]
            for u, d in edges:
                if u == n and d in remaining:
                    remaining[d] -= 1
    return out


def upstream_of(repo: str, edges: list[tuple[str, str]]) -> list[str]:
    return [u for u, d in edges if d == repo]


def downstream_closure(repo: str, edges: list[tuple[str, str]]) -> list[str]:
    seen, stack = [], [repo]
    while stack:
        cur = stack.pop()
        for u, d in edges:
            if u == cur and d not in seen:
                seen.append(d)
                stack.append(d)
    return seen
