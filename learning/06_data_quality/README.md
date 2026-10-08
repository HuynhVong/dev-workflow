# 06 - Data / ETL quality agent

**Goal:** an agent that finds bad rows by proposing rules from a data profile, then running them in code. Done when
`python -m learning.run 06` passes (exact on all five injected problems, no clean row flagged). `python make_data.py` regenerates the data.

**Read first:** Great Expectations docs (expectations = reviewable rules); dbt tests; Pandera; the "profiling then rules" idea in any data-quality talk.
**In this repo:** `jira_implement/ledger.py` (an idempotent side-effect ledger: the same idea you want before an agent writes fixes back).

**Stretch:** let the agent propose a FIX per rule (never apply it without approval); add a second table and a cross-table rule
(orders.customer_id must exist in customers.id); report the token cost of rows-through-the-model vs profile-and-rules.
