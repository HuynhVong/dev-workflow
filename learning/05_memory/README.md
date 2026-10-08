# 05 - Conversational agent with memory

**Goal:** a memory store that returns the right facts for a question within a character budget, supersedes old facts, and
forgets on request. Done when `python -m learning.run 05` passes 5/5 (the baseline passes 4/5).

**Read first:** Letta (MemGPT) docs and the paper idea (memory tiers); Mem0 repo; LangGraph docs on short-term vs long-term memory;
Claude docs on context windows and prompt caching.
**In this repo:** `ui/` and `runner.py` show persistent run state (SQLite checkpoints) - useful contrast with conversational memory.

**Stretch:** add summarisation of old sessions; measure what happens to accuracy as you halve the budget; add a rule that sensitive
facts (health, salary) are only used when the question needs them.
