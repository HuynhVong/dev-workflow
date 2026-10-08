# 07 - Multi-agent collaboration (orchestrator + workers)

**Goal:** build an orchestrator that is measurably better than one agent on these cases (or learn, with numbers, that it is not).
Done when you have a table of single-agent vs multi-agent: accuracy, calls, characters, seconds - and a written conclusion.

**Read first:** Anthropic's "How we built our multi-agent research system"; "Building effective agents" (orchestrator-workers pattern);
LangGraph docs on `Send` / subgraphs; CrewAI and AutoGen docs for other framings.
**In this repo:** `jira_implement/graph.py` (waves, `Send`, a budget on automated fix attempts), `dag.py`, `worktrees.py`
(isolation so parallel workers do not collide).

**Stretch:** add a verifier worker that checks the merged answer against the sources; add a budget (max calls) and a graceful
degraded answer when it is hit.
