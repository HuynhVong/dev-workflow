"""Exercise 01 - structured extraction. Goal: text in, a validated Invoice out, right on messy inputs.

Run:  python -m learning.run 01

Learn (in this order):
  1. A Pydantic schema IS the prompt: field names and descriptions tell the model what you want. Write them well.
  2. Let the library validate; on failure send the error back and retry once (see how src/dev_workflows/llm.py raises).
  3. Hard cases are in cases.jsonl: decimal commas, issue date vs due date, subtotal vs total, dd/mm dates, missing fields.
     Where the model must NOT guess (a missing date), make the field Optional and say so in its description.
  4. When a case fails, fix the schema descriptions or the system prompt, then rerun. Do not special-case the input.
"""
from pydantic import BaseModel, Field

from learning.llm import structured


class Invoice(BaseModel):
    # TODO 1: describe every field so the model cannot misread it (ISO date, the TOTAL not the subtotal, 3-letter currency...)
    vendor: str
    invoice_number: str
    date: str | None = Field(default=None)
    total: float
    currency: str


SYSTEM = "TODO 2: write the system prompt (short, specific; say what to do with missing or ambiguous values)."


def solve(text: str) -> Invoice:
    raise NotImplementedError("call structured(SYSTEM, text, Invoice) and return the result (then add a retry on failure)")
