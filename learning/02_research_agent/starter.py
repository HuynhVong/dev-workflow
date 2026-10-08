"""Exercise 02 - a multi-tool research agent. Goal: answer from the docs with tools, cite sources, and say "I can't find it".

Run:  python -m learning.run 02          (needs ANTHROPIC_API_KEY for the real model)
Offline practice: build your loop first against learning.llm.ScriptedModel (see tests/test_learning.py for an example script).

Learn:
  1. The loop: call the model with the messages + tool specs; if it asks for tools, run them, append the results, repeat;
     if it answers in text, stop. A step limit is part of the design, not an afterthought.
  2. A tool error is a message for the model ("error: ..."), never an exception that kills the run.
  3. Citations: the checker wants the file names you actually used. Decide how the model reports them and parse it.
  4. Abstaining is a feature: the "unanswerable" case fails if the agent invents an answer.
  5. Read the full trace (messages) of every failing case before changing anything.
After you pass, read reference.py and compare designs.
"""
from learning.llm import anthropic_model
from pathlib import Path

from learning.run import load

T = load(Path(__file__).parent / "tools.py", "research_tools")  # T.FUNCS (name -> function), T.SPECS (JSON-schema specs)

SYSTEM = "TODO 1: system prompt - role, 'use the tools, never answer from memory', how to report sources, when to abstain."
MAX_STEPS = 8


def run_agent(question: str, model, funcs: dict, specs: list, max_steps: int = MAX_STEPS) -> dict:
    """Return {"answer": str, "citations": [file names], "steps": int}."""
    raise NotImplementedError("TODO 2: write the loop (messages format is described in learning/llm.py)")


def solve(question: str) -> dict:
    return run_agent(question, anthropic_model(), T.FUNCS, T.SPECS)
