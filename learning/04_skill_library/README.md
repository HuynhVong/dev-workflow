# 04 - Domain assistant with a skill library

**Goal:** route a question to the right skill (or none) using only the catalog. Done when `python -m learning.run 04` passes 10/10,
and you can show that adding a 4th skill barely changes the prompt size.

**Read first:** Claude docs on Agent Skills (SKILL.md format, progressive disclosure); MCP prompts and resources.
**In this repo:** `routing.py` (skills per workflow step, `skills_system_block` with a character budget), `docs/jira-ticket-implement-design.md`.

**Stretch:** let the model pick up to 2 skills; add a skill that contains a script and let the agent run it; handle two skills
that overlap (what should the router do, and how would an eval catch a regression?).
