"""Text trims shared by the prompts that carry code: a diff that fits a budget without cutting a file in half."""
import re


def diff_digest(diff: str, n: int = 15000) -> str:
    """A diff that fits in `n` chars: whole when it does, else the list of changed files with their +/- line counts,
    then whole file sections in order while they fit, then the names of the files left out."""
    if len(diff) <= n:
        return diff
    sections = [s for s in re.split(r"(?m)^(?=diff --git )", diff) if s.strip()]
    def name(sec: str) -> str:
        m = re.match(r"diff --git a/(\S+)", sec)
        return m.group(1) if m else sec.splitlines()[0][:100]
    def counts(sec: str) -> str:
        lines = sec.splitlines()
        add = sum(1 for x in lines if x.startswith("+") and not x.startswith("+++"))
        rem = sum(1 for x in lines if x.startswith("-") and not x.startswith("---"))
        return f"+{add}/-{rem}"
    head = "Changed files:\n" + "\n".join(f"  {name(x)} {counts(x)}" for x in sections) + "\n\n"
    out, left, budget = [], [], n - len(head)
    for sec in sections:
        if len(sec) <= budget:
            out.append(sec)
            budget -= len(sec)
        else:
            left.append(name(sec))
    note = f"\n(diff of {', '.join(left)} left out to save space; read those files if they matter)\n" if left else ""
    return head + "".join(out) + note


def diffs_block(diffs: dict[str, str], n: int = 15000) -> str:
    """One <diff repo='...'> block per repo, each digested to `n` chars. Raw text, not JSON: putting a diff in a JSON
    string escapes every newline and quote, which costs tokens and makes the diff harder to read."""
    return "\n".join(f"<diff repo='{r}'>\n{diff_digest(d, n) or '(no changes)'}\n</diff>" for r, d in diffs.items())
