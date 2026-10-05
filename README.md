# Dev Workflows (LangGraph + Claude)

A full-stack developer's day-to-day team workflows, each as a small LangGraph graph.

| Workflow | Graph | What it does |
|---|---|---|
| Ticket to plan | `ticket_to_plan` | Analyzes a ticket, **pauses to ask you** any blocking questions, drafts an ordered plan (steps, API/DB changes, tests, risks), then a tech-lead critique loop revises it (max 2 rounds). |
| PR review | `pr_review` | Triages the diff, runs reviewers in parallel per lens (correctness, tests, plus security/performance/frontend when relevant), merges findings into one verdict. |
| Standup | `standup` | Reads your `git log` across repos plus free-text notes and writes Yesterday / Today / Blockers. |
| Jira ticket implement | `jira_implement/graph.py` | One Jira key to draft GitLab MRs across several repos: reads the ticket and its Confluence pages, plans a dependency DAG, has Claude Code implement repo by repo in waves, runs your checks, stops for your manual test and approval, then pushes, opens draft MRs and moves the ticket to Code Review. |
| Address review | `jira_implement/address_review.py` | Reads the unresolved threads on the ticket's draft MRs, lets you triage them, fixes the code in the affected repos only, re-tests, pushes, and replies in each thread (never resolves it). |

## Layout

```
src/dev_workflows/
  config.py            max tokens; effort is fixed at medium for every model
  routing.py           model per step (Haiku / Sonnet / Opus) and the global Claude Code skills each step uses
  llm.py               one Claude call helper (structured output via Pydantic); swap-able for tests
  cli.py               `devflow plan | review | standup | implement | address-review | answer | resume | ...`
  workflows/           one file per graph, each exposes build_graph() and `graph`
  jira_implement/      the two multi-repo workflows and their guardrails:
    graph.py             jira ticket implement (+ shared GraphKit, preflight, feedback routing)
    address_review.py    address-review companion graph
    vcs.py               the only place git/glab run: always `rtk git|glab`, deny-list, scope check
    scope.py             frozen repo allow-list (--repos)
    coding_agent.py      Claude Code (Agent SDK) launcher + per-tool policy hook
    confluence.py        read-only Confluence reader (write tools are never exposed)
    jira.py              ticket fetch + the idempotent Jira writes (status forward-only, one comment)
    ledger.py            run registry, audit log, write-ahead side-effect ledger (SQLite)
    runner.py            start / answer / resume / abort / status for the CLI
docs/                  jira-ticket-implement-design.md (the approved design)
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

## Jira ticket implement and address-review

First run `devflow setup`: it shows the model and effort of every step and which global Claude Code skills
(`~/.claude/skills/<name>/SKILL.md`, or skills from installed plugins) are installed or still missing. Install the
skills for your role, or point steps at the skills you already have under `ai.steps` in `workspace.yaml`. A missing
skill never stops a run; the step runs without it and preflight lists it as a warning.

Prerequisites on your machine: Jira MCP (read + write), Confluence MCP (read is enough; it is never written to),
Playwright MCP for UI repos, `git` and `glab` installed and authenticated, `rtk` installed (every git/glab call goes
through it), the Claude Code CLI with your coding skills, and `ANTHROPIC_API_KEY`. Copy `workspace.example.yaml` to
`workspace.yaml` and list your repos. The ticket should already be assigned to you and In Progress.

```bash
devflow implement AQS-5512 --repos api-service,web-portal     # prompts at each checkpoint
devflow implement AQS-5512 --no-input                         # stops at checkpoints; answer later:
devflow answer AQS-5512-20261005-101500 --choice approve
devflow status AQS-5512-20261005-101500                       # lifecycle, next step, repos, pending checkpoint
devflow resume AQS-5512-20261005-101500                       # after a failure or a killed process
devflow abort  AQS-5512-20261005-101500 --note "wrong ticket" # terminal; `resume --reopen` continues it explicitly
devflow show   AQS-5512-20261005-101500                       # audit log + side-effect ledger

devflow address-review AQS-5512                               # after reviewers comment on the draft MRs
devflow answer <run_id> --choice edit --fix 1,3 --answer-only 2 --skip 4
```

What the workflows never do: edit Confluence, send mail, read or trigger CI/CD pipelines, force-push, rebase,
delete branches, merge, approve or un-draft MRs, resolve review threads, or touch a repo outside `--repos` without
your approval. Every checkpoint (plan, manual test, push, ...) offers abort. Each repo gets 3 automated fix attempts
per run in total; after that the run waits for you.

## Adding a workflow

1. Copy a file in `workflows/`, define Pydantic output models and a `State` TypedDict.
2. Nodes call `get_llm().structured(system, prompt, Schema)`.
3. Register it in `langgraph.json` and add a subcommand in `cli.py`.
4. Add a test using `tests/fake_llm.py`.

Every AI step picks its own model (see `routing.py` and the table in `docs/jira-ticket-implement-design.md`); every model runs at `medium` effort, with server-side refusal fallback enabled.
