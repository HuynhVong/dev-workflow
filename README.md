# Dev Workflows (LangGraph + Claude)

A full-stack developer's day-to-day team workflows, each as a small LangGraph graph.

| Workflow | Graph | What it does |
|---|---|---|
| Ticket to plan | `ticket_to_plan` | Analyzes a ticket, **pauses to ask you** any blocking questions, drafts an ordered plan (steps, API/DB changes, tests, risks), then a tech-lead critique loop revises it (max 2 rounds). |
| PR review | `pr_review` | Triages the diff, runs reviewers in parallel per lens (correctness, tests, plus security/performance/frontend when relevant), merges findings into one verdict. |
| Standup | `standup` | Reads your `git log` across repos plus free-text notes and writes Yesterday / Today / Blockers. |

## Layout

```
src/dev_workflows/
  config.py            model + effort (env: DEVFLOW_MODEL, DEVFLOW_EFFORT)
  llm.py               one Claude call helper (structured output via Pydantic); swap-able for tests
  cli.py               `devflow plan | review | standup`
  workflows/           one file per graph, each exposes build_graph() and `graph`
tests/                 offline tests with a fake LLM (no API key needed)
langgraph.json         lets `langgraph dev` / LangGraph Studio load all graphs
```

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # add ANTHROPIC_API_KEY
pytest -q
```

## Usage

```bash
devflow plan --ticket examples/ticket.md --context docs/stack.md
devflow review --title "Add order export" --diff <(git diff main...HEAD)
devflow standup --repo ~/code/api --repo ~/code/web --author "$(git config user.email)"
langgraph dev          # visual graph + step-through in LangGraph Studio
```

## Adding a workflow

1. Copy a file in `workflows/`, define Pydantic output models and a `State` TypedDict.
2. Nodes call `get_llm().structured(system, prompt, Schema)`.
3. Register it in `langgraph.json` and add a subcommand in `cli.py`.
4. Add a test using `tests/fake_llm.py`.

Default model is `claude-opus-5-5` at `medium` effort, with server-side refusal fallback enabled.
