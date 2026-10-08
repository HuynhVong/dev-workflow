"""Exercise 05 - memory across sessions. Goal: decide what goes into the prompt, within a budget.

Run:  python -m learning.run 05

You build `MemoryStore.context_for(question, budget_chars)`: the text that would be placed in the prompt. The checker looks at that
text directly (no answer generation needed), so you can iterate fast and cheaply.

Learn:
  1. Memory is a retrieval problem under a token budget: what to keep, how to find it, what to drop.
  2. Facts change. "I moved to Da Nang" must replace "I live in Hanoi". Try: have the LLM extract facts as (subject, value) once
     per session (structured output!), store them keyed, and let a newer value overwrite the older one.
  3. Forgetting on request is a feature with real consequences (privacy): test it.
  4. Do not stuff irrelevant memory into the prompt - it costs tokens and distracts the model.
reference.py is a word-overlap baseline: read it, see which cases it fails, and beat it.
In this repo: LangGraph checkpoints (runner.py) persist a RUN's state; that is not conversational memory. Compare the two.
"""
from pathlib import Path

from learning.run import load

Baseline = load(Path(__file__).parent / "reference.py", "memory_reference").MemoryStore


class MemoryStore(Baseline):  # TODO: replace the baseline pieces one by one (extraction, conflict handling, ranking)
    pass


def solve(case: dict) -> str:
    store = MemoryStore()
    for session in case["sessions"]:
        store.add_session(session)
    return store.context_for(case["question"], case["budget_chars"])
