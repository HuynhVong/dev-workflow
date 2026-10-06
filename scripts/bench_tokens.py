"""Token benchmark for the Claude Code path: run it on `main` and on the token-saving branch and compare.

    python scripts/bench_tokens.py            # uses your `claude` login, a few cents of usage on Haiku

It runs the real devflow code on a tiny throwaway repo (fix a typo) and prints the input tokens (new + cache write +
cache read) and output tokens of each call: a coding-agent session, one structured AI step, and, with --mcp
<server> <tool> <json-args>, one Jira/Confluence-style call through your Claude Code connector.
Agent runs vary by a few turns, so the script repeats each case (--runs, default 3) and reports the median."""
import argparse
import json
import os
import statistics
import subprocess
import tempfile

from dev_workflows import telemetry
from dev_workflows.config import settings
from dev_workflows.jira_implement.coding_agent import ClaudeCodeAgent
from dev_workflows.jira_implement.models import Analysis
from dev_workflows.jira_implement.scope import ScopeGuard
from dev_workflows.llm import ClaudeCliLLM
from dev_workflows.routing import Routing, SkillRegistry, Step

calls: list[tuple[str, dict]] = []
telemetry.record_usage = lambda step, model, usage, **kw: calls.append((step, usage))  # capture instead of storing


def total_in(u: dict) -> int:
    return sum(u.get(k, 0) or 0 for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))


def routing() -> Routing:
    r = Routing(registry=SkillRegistry([tempfile.mkdtemp()]))
    r.steps["implement"] = Step("haiku")
    r.steps["bench.structured"] = Step("haiku")
    return r


def agent_case() -> int:
    d = os.path.realpath(tempfile.mkdtemp())
    open(d + "/app.js", "w").write("const label = 'Exprot';\nmodule.exports = {label};\n")
    subprocess.run("git init -q && git add -A && git -c user.email=a@b -c user.name=n commit -qm init", shell=True, cwd=d)
    calls.clear()
    ClaudeCodeAgent(ScopeGuard.create({"demo": d}), routing=routing()).implement(
        "demo", d, "In app.js the label says 'Exprot'; change it to 'Export'.", step="implement")
    return sum(total_in(u) + u.get("output_tokens", 0) for _, u in calls)


def structured_case() -> int:
    calls.clear()
    ClaudeCliLLM(settings, routing=routing()).structured(
        "You are a senior engineer.", "<ticket key='X-1'><title>Button typo</title><description>Export says 'Exprot'."
        "</description></ticket>\nAnalyze the requirement.", Analysis, step="bench.structured")
    return sum(total_in(u) + u.get("output_tokens", 0) for _, u in calls)


def mcp_case(server: str, tool: str, args: dict) -> int:
    from dev_workflows.jira_implement.claude_code_mcp import ClaudeCodeMcp
    calls.clear()
    ClaudeCodeMcp(server, {"op": tool}, {}).call(tool, args)
    return sum(total_in(u) + u.get("output_tokens", 0) for _, u in calls)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--mcp", nargs=3, metavar=("SERVER", "TOOL", "JSON_ARGS"), help="also time one connector call")
    a = ap.parse_args()
    cases = {"coding agent session (fix a typo)": agent_case, "structured AI step": structured_case}
    if a.mcp:
        cases[f"connector call {a.mcp[0]}.{a.mcp[1]}"] = lambda: mcp_case(a.mcp[0], a.mcp[1], json.loads(a.mcp[2]))
    print(f"{'case':<40} {'median tokens':>14}   runs")
    for name, fn in cases.items():
        runs = [fn() for _ in range(a.runs)]
        print(f"{name:<40} {int(statistics.median(runs)):>14}   {runs}")
