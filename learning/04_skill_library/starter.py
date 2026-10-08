"""Exercise 04 - a domain assistant with a skill library. Goal: pick the right skill (or none) while showing the model only
the catalog, and load a skill's full text only after it is chosen.

Run:  python -m learning.run 04

Learn:
  1. Progressive disclosure: the catalog (name + one line) is always in context; a skill body is loaded on demand. Measure the
     difference: len(catalog) vs the sum of all bodies grows with every skill you add.
  2. The routing prompt must allow "none" - the chit-chat cases fail if every question gets a skill.
  3. reference.py is a keyword router: it scores 8/10 and fails the two paraphrased cases. Your LLM router should not.
  4. Then write your own skill (a new folder with SKILL.md) for a domain you know, plus 6 routing cases for it.
In this repo: src/dev_workflows/routing.py (SkillRegistry, a step -> skills table, a budget on skill text) - read it after.
"""
from pathlib import Path

from pydantic import BaseModel

from learning.llm import structured
from learning.run import load

Lib = load(Path(__file__).parent / "reference.py", "skill_reference").SkillLibrary
LIB = Lib(Path(__file__).parent / "skills")


class Route(BaseModel):
    skill: str | None


def solve(question: str) -> str | None:
    raise NotImplementedError("TODO: structured(<system: choose from the catalog or null>, question + LIB.catalog(), Route).skill")
