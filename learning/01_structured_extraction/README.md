# 01 - Structured-output extraction

**Goal:** turn messy text into a validated object. Done when `python -m learning.run 01` passes 8/8 on the cases, and you can
explain why each failure you fixed was a schema/prompt problem and not a model problem.

**Read first:** Claude docs on structured outputs / tool use; Pydantic and Instructor docs (retries on validation errors).
**In this repo:** `src/dev_workflows/llm.py` (`structured`, refusal and "no structured output" handling) and `jira_implement/models.py`.

**Stretch:** add 10 invoices of your own (different languages, scanned-OCR noise) to `cases.jsonl`; track the pass rate when you
switch model tiers (Haiku vs Sonnet) and write down the cost of each point of accuracy.
