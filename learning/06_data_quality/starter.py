"""Exercise 06 - a data-quality agent. Goal: find injected bad rows in data.csv, exactly, without reading every row through an LLM.

Run:  python -m learning.run 06

Learn:
  1. Cost: a model reading every row does not scale (200 rows today, 20 million tomorrow) and is not exact. Instead:
       profile the data (column types, null counts, min/max, patterns) -> show the LLM the PROFILE and ~20 sample rows ->
       have it propose rules as structured data -> run the rules with plain code over every row -> report.
  2. A rule is data you can review: {"name", "column", "check": "regex|range|not_null|unique", "arg"}. A human approves it
     before it runs on production data. Build that approval step even if it is just a print.
  3. Precision matters: the checker fails if a clean row is flagged.
  4. Keep the rule names the checker expects (duplicate_id, negative_age, bad_email, missing_name, future_signup).
reference.py is the hand-written baseline (it scores 1/1); your agent must reach the same result from the profile alone.
"""
import csv
from pathlib import Path

from pydantic import BaseModel

from learning.llm import structured  # noqa: F401  (you will use it to propose rules)

HERE = Path(__file__).parent


class Rule(BaseModel):
    name: str
    column: str
    kind: str  # "regex" | "min" | "max" | "not_null" | "unique"
    arg: str | None = None


class Rules(BaseModel):
    rules: list[Rule]


def profile(path: Path) -> dict:
    """TODO 1: per column: name, null count, distinct count, min/max for numbers/dates, 3 sample values."""
    raise NotImplementedError("write profile(); keep it small - it is what you send to the model")


def run_rule(rule: Rule, rows: list[dict]) -> list[int]:
    """TODO 3: apply one rule to every row in plain code; return the ids of the rows that break it."""
    raise NotImplementedError("write run_rule()")


def solve(csv_name: str) -> dict[str, list[int]]:
    path = HERE / csv_name
    rows = list(csv.DictReader(open(path, newline="")))
    prof = profile(path)  # noqa: F841
    # TODO 2: rules = structured(<system>, <profile + sample rows>, Rules).rules ; print them for approval ; then
    return {r.name: run_rule(r, rows) for r in []}
