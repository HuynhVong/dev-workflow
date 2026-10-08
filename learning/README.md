# learning/ - study path for LLM agents

Seven hands-on exercises, each with starter code (TODOs), a small test set, a checker you do not edit, and where
possible a reference baseline to compare with after you try. Everything is measured: you always know if a change helped.

```bash
pip install -e .                 # from the repo root, once (or use the repo's existing environment)
python -m learning.run 01        # run exercise 01 against its cases and print a pass/fail table
python -m pytest tests/test_learning.py   # checks the checkers, tools and baselines (no API calls)
```

Models: exercises 01, 03, 04 use `learning.llm.structured`, which works with `ANTHROPIC_API_KEY` or with your Claude Pro/Max
login (`claude` CLI). Exercises 02 and 07 are tool loops and need `ANTHROPIC_API_KEY`; you can build and test the loop
offline first with `learning.llm.ScriptedModel`. Budget: a full pass over all cases costs cents on Haiku/Sonnet.

## Order

| # | Exercise | Skill you practise | Done when |
|---|---|---|---|
| 0 | read first | the agent loop, tool use, MCP, structured output | you can explain "workflow vs agent" |
| 01 | structured extraction | schema as prompt, validation, retries, "don't guess" | 8/8 |
| 02 | research agent | tool loop, errors as messages, citations, abstaining | 5/5 within 8 steps |
| 03 | code review | precision vs recall, "empty is fine", lenses | 7/7 |
| 04 | skill library | progressive disclosure, routing with a catalog | 10/10 |
| 05 | memory | retrieval under a budget, superseding facts, forgetting | 5/5 (baseline 4/5) |
| 06 | data quality | profile -> propose rules -> run in code, human approval | exact on all rules |
| 07 | multi-agent | orchestrator + workers, and proving it beats one agent | a measured table + conclusion |

Do 01 and 02 first, in order. Then 03 and 04 in either order, 05 and 06 in either order, 07 last.

## Habits (the real curriculum)

1. **Evals first.** Add 5 cases of your own to every exercise before you "improve" anything.
2. **Read the trace.** When a case fails, read every message and tool call before touching the prompt.
3. **Cost is a feature.** Log calls and characters (see `Counted`); ask what each step buys you.
4. **Start with one prompt, then one agent, then more.** Each extra moving part must win a measurement.

## Free resources (search by name)

- Anthropic: *Building effective agents*; *How we built our multi-agent research system*; the Cookbook and Courses repos on GitHub;
  Claude docs on tool use, structured outputs, prompt caching and Agent Skills.
- Model Context Protocol docs (modelcontextprotocol.io).
- Hugging Face Agents Course; Microsoft *AI Agents for Beginners*; LangChain Academy *Intro to LangGraph*;
  DeepLearning.AI short courses on agents and multi-agent systems.
- Pydantic and Instructor docs; Great Expectations docs; Letta (MemGPT) docs; Mem0 repo; SWE-bench; the Aider repo.
- Blogs: Lilian Weng *LLM Powered Autonomous Agents*; Hamel Husain on evals; Eugene Yan on LLM patterns.

## This repo as a worked example

`src/dev_workflows/llm.py` (structured output) - `workflows/pr_review.py` (review lenses) - `routing.py` (skills) -
`jira_implement/graph.py` (parallel workers over a dependency graph) - `jira_implement/coding_agent.py` (tool policy) -
`jira_implement/mcp_client.py` (tools over MCP). Each exercise's README says which to read and when.
