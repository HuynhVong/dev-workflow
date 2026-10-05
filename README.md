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
    worktrees.py         one git worktree per run per repo (parallel tickets)
  registry.py          every workflow the UI and CLI can run, found by discovery
  doctor.py            setup checks (`devflow doctor`), shared by preflight and the UI
  telemetry.py         token usage and coding-agent activity, recorded per run and node
  ui/                  `devflow ui`: FastAPI server, background run manager, built frontend in ui/static
frontend/              the web app source (React, TypeScript, Vite, Tailwind, React Flow); `npm run build`
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

## The web app (`devflow ui`)

```bash
devflow ui                      # prints a one-time link and opens it: http://127.0.0.1:8765/?token=...
devflow doctor [--json]         # the same connection checks from the terminal
```

A local app over the same runner the CLI uses, so every guardrail is unchanged and CLI and UI work on the same
runs. It binds to 127.0.0.1 only, the token in the link becomes a cookie, and secrets in workspace.yaml are never
sent to the browser. Design: `docs/devflow-ui-plan.md`.

- **Setup**: opens when there is no workspace.yaml. Edit repos and MCP servers, see every connection check, and the
  model and skills per step.
- **Overview**: runs waiting on you first, then running, with live updates. Start any discovered workflow from
  **New run**; ticket workflows run the preflight checks inline. Up to `max_parallel_runs` run at once; more queue.
- **Run detail**: steps or the real graph, the approval panel for each checkpoint, node output, diff, input and the
  live Claude Code log, plus Activity, Repos, Decisions, Side effects, Audit log and Tokens tabs. Abort, Resume and
  Reopen work as in the CLI.
- **Tokens**: per day, workflow, model and step, with estimated cost from the `prices` table in workspace.yaml.
- **Worktrees** and **Settings** (workspace, connections, models and skills, prices, notifications).

Any graph added to the repo shows up without UI code: its graph, a start form generated from its input schema, and
its `interrupt()` payloads as a generic answer form. A `DEVFLOW_UI` dict in the module adds titles and custom forms.

The built frontend is committed under `src/dev_workflows/ui/static`, so using the app needs no Node. To change it:

```bash
cd frontend && npm install
npm run dev                     # against a running `devflow ui` on port 8765
npm run build                   # writes src/dev_workflows/ui/static
npm run e2e                     # Playwright tests against the offline demo server
python tests/ui/demo_server.py --token demo   # try the UI with fake Claude, Jira and GitLab, no accounts
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

### Parallel runs (git worktrees)

`repos[].path` in `workspace.yaml` is your own clone: devflow only fetches from it and adds worktrees, it never edits
or switches it, so it can have work in progress. Every ticket works in its own worktree,
`<worktree_root>/<TICKET>/<repo>` (default `~/devflow-worktrees`), cut from a freshly fetched `develop`. So you can run
`devflow implement AQS-5512` and `devflow implement AQS-5513` at the same time. Rules:

- One unfinished run per ticket. A ticket branch can only be checked out in one place, so preflight stops if your
  own clone has it checked out.
- `setup_commands` (e.g. `npm ci`) and `copy_files` (e.g. `.env.local`) per repo prepare a fresh worktree once.
- Check commands get `DEVFLOW_WORKTREE_<REPO>` pointing at the run's sibling worktrees. A repo with
  `parallel_checks: false` (fixed ports, a shared local database) runs its checks one ticket at a time.
- Worktrees are never removed automatically: `devflow worktrees` lists them, `devflow worktree-clean AQS-5512`
  removes a finished ticket's clean worktrees (never forced; the branch stays).

What the workflows never do: edit Confluence, send mail, read or trigger CI/CD pipelines, force-push, rebase,
delete branches, merge, approve or un-draft MRs, resolve review threads, or touch a repo outside `--repos` without
your approval. Every checkpoint (plan, manual test, push, ...) offers abort. Each repo gets 3 automated fix attempts
per run in total; after that the run waits for you.

## Adding a workflow

1. Copy a file in `workflows/`, define Pydantic output models and a `State` TypedDict.
2. Nodes call `get_llm().structured(system, prompt, Schema)`.
3. Register it in `langgraph.json` (or call `register_workflow(id, factory)` / add a `devflow.workflows` entry point when
   it needs runtime dependencies) and add a subcommand in `cli.py`. The UI picks it up on start or on Settings,
   App, Re-scan workflows.
4. Optionally add a `DEVFLOW_UI` dict to the module: title, description, node labels, step order, a start form.
5. Add a test using `tests/fake_llm.py`.

Every AI step picks its own model (see `routing.py` and the table in `docs/jira-ticket-implement-design.md`); every model runs at `medium` effort, with server-side refusal fallback enabled.
