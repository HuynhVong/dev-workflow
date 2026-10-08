# 03 - Code review / codebase agent

**Goal:** review diffs with 100% recall on the planted bugs and no padding on the clean one. Done when
`python -m learning.run 03` passes 7/7. Then add 5 real diffs from your own history to `cases.jsonl` (label the true bugs).

**Read first:** SWE-bench (how coding agents are measured), the Aider repo (repo maps and edit formats), Claude Code docs.
**In this repo:** `workflows/pr_review.py` (triage, parallel lenses, verdict), `jira_implement/vcs.py` (read-only git for agents),
`coding_agent.py` (what a coding agent may and may not touch).

**Stretch:** let the agent call `read_file`/`grep` over a checkout; give each finding a confidence and drop low ones; compare
precision/recall of single-pass vs lenses on 20 cases.
