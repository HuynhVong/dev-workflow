"""Exercise 07 - orchestrator + workers. Goal: split a question into sub-questions, answer them in parallel with worker agents,
and merge - then PROVE it is better than one agent, or throw it away.

Run:  python -m learning.run 07          (needs ANTHROPIC_API_KEY)

Steps:
  1. Baseline: `single_agent` = exercise 02's loop on the whole question (reference.py there). Run the three cases, note
     correctness, model calls and characters sent (learning.llm.Counted wraps any model and counts both).
  2. Orchestrator: one call that returns a plan (a list of independent sub-questions, structured output). Workers: one agent run
     per sub-question, in threads. Merger: one call that composes the final answer from the workers' answers ONLY.
  3. Compare in a table: accuracy, calls, chars, wall-clock seconds. Multi-agent costs more tokens (each worker repeats the system
     prompt and tool specs), so it must buy you something: parallel speed, isolated context, or accuracy on bigger tasks.
  4. Failure handling: what if a worker fails or returns nothing? The merger must say what is missing, not invent it.
Rules of thumb you should be able to defend afterwards: start with one agent; add workers only for independent subtasks that are
big enough to pay for their overhead; give workers narrow tools; never let two workers write to the same place.
In this repo: jira_implement/graph.py runs repos as waves of parallel workers (Send), with a DAG deciding what can run together.
"""
from concurrent.futures import ThreadPoolExecutor  # noqa: F401  (workers run in threads)
from pathlib import Path

from learning.llm import Counted, anthropic_model, structured  # noqa: F401
from learning.run import load

HERE = Path(__file__).parent
T = load(HERE.parent / "02_research_agent" / "tools.py", "research_tools")
ref = load(HERE.parent / "02_research_agent" / "reference.py", "research_reference")


def single_agent(question: str, model) -> dict:
    """Baseline: one agent with all tools (this works as given; run it first)."""
    return ref.run_agent(question, model, T.FUNCS, T.SPECS)


def orchestrate(question: str, model) -> dict:
    raise NotImplementedError("TODO: plan -> workers in parallel -> merge (see the steps above)")


def solve(question: str) -> dict:
    model = Counted(anthropic_model())
    out = orchestrate(question, model)  # swap for single_agent(question, model) to measure the baseline
    return {"answer": out["answer"], "calls": model.calls, "chars": model.chars}
