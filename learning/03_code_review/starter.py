"""Exercise 03 - a code review agent. Goal: find planted bugs in a diff without inventing others.

Run:  python -m learning.run 03

Learn:
  1. Output = structured findings (file, line, severity, title, detail). Vague output cannot be checked or acted on.
  2. "An empty list is a fine answer" must be IN the prompt, or the model pads. The clean case tests exactly that.
  3. Try one pass vs one pass per lens (correctness, security, ...). Compare pass rate and cost: src/dev_workflows/workflows/pr_review.py
     fans a diff out to lenses and merges a verdict. Is the extra cost worth it on these cases? Measure, don't guess.
  4. Going further than a diff: give the agent read-only tools (read_file, grep) so it can check how a function is used.
"""
from pydantic import BaseModel

from learning.llm import structured


class Finding(BaseModel):
    file: str
    line: int | None = None
    title: str
    detail: str


class Review(BaseModel):
    findings: list[Finding]


SYSTEM = "TODO 1: reviewer role, what counts as a finding, 'report only what you can point to in the diff', empty list is fine."


def solve(diff: str) -> list[Finding]:
    raise NotImplementedError("TODO 2: structured(SYSTEM, <diff in tags + instruction>, Review).findings")
