# Dev Workflows (LangGraph + Claude)

A full-stack developer's day-to-day team workflows, each as a LangGraph graph, for multi-repo work on GitLab with
Jira and Confluence. Run them from the `devflow` CLI or the local web app (`devflow ui`); both drive the same runs.

| Workflow | Command | What it does |
|---|---|---|
| Ticket to plan | `devflow plan` | Analyzes a ticket, **pauses to ask you** any blocking questions, drafts an ordered plan (steps, API/DB changes, tests, risks), then a tech-lead critique loop revises it (max 2 rounds). |
| Jira ticket implement | `devflow implement` | One Jira key to draft GitLab MRs across several repos: reads the ticket and its Confluence pages, plans a dependency DAG, has Claude Code implement repo by repo in waves, runs your checks, stops for your manual test and approval, then pushes, opens draft MRs and moves the ticket to Code Review. |
| Address review | `devflow address-review` | Reads the unresolved threads on the ticket's draft MRs, lets you triage them, fixes the code in the affected repos only, re-tests, pushes, and replies in each thread (never resolves it). |
| Ticket review | `devflow review` | A Jira ticket plus the commits that implement it: you check out the branch, it reviews the commits against the ticket, writes a test plan you approve, runs it with Playwright (pausing when a step needs you), and posts one comment with the proof screenshots on the ticket after you approve it. Replaces the old diff-only PR review. |
| Standup | `devflow standup` | Your work report for a date range: the Jira tickets you moved Approved → In Progress and the ones moved to In Review while assigned to you (reviews), found from each ticket's history and written into your own report template. Read-only. |

## Prerequisites

- Python 3.11+, and either a Claude Code login (Claude Pro or Max plan, no API key) or an `ANTHROPIC_API_KEY`
  (see [AI backend](#ai-backend)).
- **Jira MCP** with read and write access, connected to Claude Code (`claude mcp add ...`) or listed in `workspace.yaml`.
- **Confluence MCP**. Read access is enough: every workflow treats Confluence as strictly read-only and never exposes
  its write tools.
- **Playwright MCP**, for repos with a UI and for the ticket review.
- `git` and/or `glab` installed and authenticated.
- `rtk` installed. Every git and glab command devflow runs carries the `rtk` prefix
  (`rtk git ...`, `rtk glab ...`); bare git/glab is never called.
- The Claude Code CLI, with the global role skills for your stack installed at first-time setup (see below).

## Setup

On Ubuntu or WSL, install Python and Claude Code first. Ubuntu ships only `python3`; `python-is-python3` adds
`python`. Ubuntu 22.04 has Python 3.10, so install 3.11+ there (for example from the deadsnakes PPA).

```bash
sudo apt update && sudo apt install -y python3-venv python3-pip python-is-python3
python3 --version                             # 3.11 or newer
curl -fsSL https://claude.ai/install.sh | bash   # Claude Code, if `claude` is not installed yet
claude                                        # log in once (Pro/Max account, or an API key)
```

Then, in the repo:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env                          # optional: ANTHROPIC_API_KEY, or leave it out to use your Claude login
cp workspace.example.yaml workspace.yaml      # list your repos, their checks and MCP servers
devflow setup --ticket AQS-5512               # first-time setup
devflow doctor                                # every connection check, any time (also in the UI)
pytest -q                                     # offline tests, no API key needed
```

devflow reads settings from the environment only; it does not load `.env` by itself. Either turn on
`python.terminal.useEnvFile` in VS Code (new terminals then load `.env`), or run `set -a && source .env && set +a` in
each terminal.

### Windows with WSL and VS Code

Clone the repo inside WSL (for example `~/workspace/dev-workflow`), install the **WSL** extension in Windows VS Code,
then run `code .` from the repo folder in the Ubuntu terminal. The bottom-left corner should show **WSL: Ubuntu**.

- `code` not found: reinstall VS Code on Windows with "Add to PATH" ticked, run `wsl --shutdown` in PowerShell, reopen
  Ubuntu.
- "Failed to connect to the remote extension host server (1006)": update VS Code, close it, run `wsl --shutdown`, then
  `rm -rf ~/.vscode-server` in Ubuntu and `code .` again. Check `df -h ~` too; a full disk causes the same error.
- `claude` not found after installing packages: add `~/.local/bin` to PATH
  (`echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc && source ~/.bashrc`) or rerun the Claude Code installer.

`devflow setup` finds the Jira MCP in `workspace.yaml` or in Claude Code's own MCP servers, checks that it answers,
can read tickets (and the one you name), and whether it can comment and attach files. It also shows the model of
every step and which global Claude Code skills (`~/.claude/skills/<name>/SKILL.md`, or skills from installed plugins)
are installed or still missing. Install the skills for your role, or point steps at skills you already have under
`ai.steps` in `workspace.yaml`. A missing skill never stops a run; the step runs without it and preflight warns.

### Models

Every AI step picks its own model at run time (defaults in `routing.py`, overridable under `ai:` in `workspace.yaml`):
Haiku for fetching, context and summaries, Sonnet for analysis, coding and review, Opus for planning and contract
review. Deterministic steps use no model. Every model runs at **medium** effort, fixed by design. The table is in
`docs/jira-ticket-implement-design.md`.

### AI backend

The AI steps run in one of two ways, picked by `DEVFLOW_LLM_BACKEND` (default `auto`):

- `claude-cli`: each step runs through headless Claude Code (`claude -p --json-schema`) on the account you are
  logged in to with `claude`, so a **Claude Pro or Max plan works with no API key**. Calls count against that plan's
  usage limits, and which models you get depends on the plan.
- `api`: each step calls the Anthropic API with `ANTHROPIC_API_KEY`, billed per token.
- `auto`: `api` when `ANTHROPIC_API_KEY` is set, otherwise `claude-cli`.

The coding steps always run through Claude Code. `devflow doctor` shows which backend is in use.

## Usage

### Ticket to plan

```bash
devflow plan --ticket examples/ticket.md --context my-stack-notes.md
langgraph dev          # this graph in LangGraph Studio
```

### Jira ticket implement and address-review

The ticket should already be assigned to you and In Progress.

```bash
devflow implement AQS-5512 --repos api-service,web-portal     # prompts at each checkpoint
devflow implement AQS-5512 --no-input                         # stops at checkpoints; answer later:
devflow answer AQS-5512-20261005-101500 --choice approve
devflow status AQS-5512-20261005-101500                       # lifecycle, next step, repos, pending checkpoint
devflow resume AQS-5512-20261005-101500                       # after a failure or a killed process
devflow abort  AQS-5512-20261005-101500 --note "wrong ticket" # terminal; `resume --reopen` continues it explicitly
devflow show   AQS-5512-20261005-101500                       # audit log + side-effect ledger
devflow runs                                                  # recent runs

devflow address-review AQS-5512                               # after reviewers comment on the draft MRs
devflow answer <run_id> --choice edit --fix 1,3 --answer-only 2 --skip 4
```

Branches are cut from a freshly pulled `develop` and named by the bare ticket key (`AQS-5512`). MRs are drafts
targeting `develop` and are never merged by devflow. `--repos` is a hard allow-list: touching any other repo pauses
for your approval. Each repo gets 3 automated fix attempts per run; after that the run waits for you.

What these workflows never do: edit Confluence, send mail, read, watch or trigger CI/CD pipelines, force-push,
rebase, delete branches, merge, approve or un-draft MRs, or resolve review threads. Every checkpoint (plan, manual
test, push, ...) offers abort.

#### Parallel runs (git worktrees)

`repos[].path` in `workspace.yaml` is your own clone: devflow only fetches from it and adds worktrees, it never edits
or switches it, so it can have work in progress. Every ticket works in its own worktree,
`<worktree_root>/<TICKET>/<repo>` (default `~/devflow-worktrees`), so `devflow implement AQS-5512` and
`devflow implement AQS-5513` can run at the same time (up to `max_parallel_runs`, default 3; more queue in the UI).

- One unfinished run per ticket. A ticket branch can only be checked out in one place, so preflight stops if your
  own clone has it checked out.
- `setup_commands` (e.g. `npm ci`) and `copy_files` (e.g. `.env.local`) per repo prepare a fresh worktree once.
- Check commands get `DEVFLOW_WORKTREE_<REPO>` pointing at the run's sibling worktrees. A repo with
  `parallel_checks: false` (fixed ports, a shared local database) runs its checks one ticket at a time.
- Worktrees are never removed automatically: `devflow worktrees` lists them, `devflow worktree-clean AQS-5512`
  removes a finished ticket's clean worktrees (never forced; the branch stays).

### Ticket review

```bash
devflow review AQS-5512 --commit web-portal=3f9a1c2 --commit api-service=88be0d4,a17c3e9 [--app-url http://localhost:5173]
```

1. **Check out** (you): it shows, per repo, the `rtk git fetch / checkout / pull` to run in your own clone and waits.
   Start the app, give its URL (default: `app_url` of the repo in workspace.yaml) and choose ready. It then checks,
   read-only, that every commit is in your checked-out branch, nothing tracked is uncommitted and the branch isn't
   behind its remote. devflow never checks out, pulls or commits for you.
2. **Understand the ticket**: the ticket, its attachments and linked Confluence pages (read-only).
3. **Code review**: a lens review of exactly those commits per repo, plus which acceptance criteria they meet.
4. **Test plan** (you approve): E2E cases mapped to the criteria. Untick, edit or add cases, or have it rewritten.
5. **E2E**: Claude Code with the Playwright MCP runs one case at a time and saves proof screenshots under
   `<state_dir>/<run>/evidence`. When a step needs you (an OTP, SSO login, a captcha) it pauses: give the value in the
   note or do it in the test browser (its profile is kept in `<state_dir>/playwright-profile`), then continue.
6. **Results** (you): pass/fail with screenshots; accept, or re-test the cases you tick.
7. **Jira comment** (you approve): the exact comment. Approve posts it and attaches the screenshots. Nothing reaches
   Jira before this.

It never commits, pushes, calls glab, touches CI/CD, changes the ticket status or writes to Confluence. If Jira
refuses the comment, the run still completes: the comment is saved to `<state_dir>/<run>/jira-comment.md` and the
screenshots to `<state_dir>/<run>/upload` for you to post by hand.

### Standup (work report)

```bash
devflow standup --from 2026-10-01 --to 2026-10-03 --template ~/reports/weekly.md
```

Dates are inclusive (default: previous working day to today). The template defaults to the last one you used, else a
built-in one. Which tickets count is decided from each ticket's Jira history, not by the model:

- **Worked on**: moved Approved → In Progress in the range while assigned to you.
- **Reviewed**: moved to In Review in the range while assigned to you (your own started tickets are excluded).

You confirm the ticket list, then review the written report. "You" is `jira_user` in `workspace.yaml`. It never
writes to Jira. Design: `docs/standup-design.md`.

## The web app (`devflow ui`)

```bash
devflow ui                      # prints a one-time link and opens it: http://127.0.0.1:8765/?token=...
```

A local app over the same runner the CLI uses, so every guardrail is unchanged and CLI and UI work on the same runs.
It binds to 127.0.0.1 only, the token in the link becomes a cookie, and secrets in workspace.yaml are never sent to
the browser. Design: `docs/devflow-ui-plan.md`.

- **Setup**: opens when there is no workspace.yaml. Edit repos and MCP servers, see every connection check, and the
  model and skills per step.
- **Overview**: runs waiting on you first, then running, with live updates. Start any workflow from **New run**;
  ticket workflows run the preflight checks inline.
- **Run detail**: steps or the real graph, the approval panel for each checkpoint (including the ticket review's
  test plan and results and the standup's ticket list and report), node output, diff, input and the live Claude Code
  log, plus Activity, Repos, Decisions, Side effects, Audit log and Tokens tabs. Abort, Resume and Reopen work as in
  the CLI.
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

## Layout

```
src/dev_workflows/
  cli.py               `devflow plan | implement | address-review | review | standup | setup | doctor | ui |
                        answer | resume | abort | status | show | runs | worktrees | worktree-clean`
  config.py            default model and max tokens; effort is fixed at medium
  routing.py           model per step (Haiku / Sonnet / Opus) and the global Claude Code skills each step uses
  llm.py               one Claude call helper (structured output via Pydantic); swap-able for tests
  registry.py          every workflow the UI and CLI can run, found by discovery
  doctor.py            setup checks (`devflow setup`, `devflow doctor`), shared by preflight and the UI
  telemetry.py         token usage and coding-agent activity, recorded per run and node
  workflows/
    ticket_to_plan.py    ticket to plan
    pr_review.py         the lens code review engine the ticket workflows reuse (no longer a standalone workflow)
  jira_implement/      the Jira workflows and their guardrails:
    graph.py             jira ticket implement (+ shared GraphKit, preflight, feedback routing)
    address_review.py    address-review companion graph
    ticket_review.py     ticket review (commits vs ticket, approved E2E proof, Jira comment)
    standup.py           standup / work report (date range + your template, tickets from Jira history)
    dag.py               repo dependency DAG and waves
    workspace.py         workspace.yaml loader
    mcp_client.py        MCP client; mcp_config.py finds the Jira MCP in workspace.yaml or Claude Code
    vcs.py               the only place git/glab run: always `rtk git|glab`, deny-list, scope check
    scope.py             frozen repo allow-list (--repos)
    coding_agent.py      Claude Code (Agent SDK) launcher + per-tool policy hook
    confluence.py        read-only Confluence reader (write tools are never exposed)
    jira.py              ticket fetch + the idempotent Jira writes (status forward-only, one comment, attachments)
    ledger.py            run registry, audit log, write-ahead side-effect ledger (SQLite)
    runner.py            start / answer / resume / abort / status for the CLI and UI
    worktrees.py         one git worktree per run per repo (parallel tickets)
  ui/                  `devflow ui`: FastAPI server, background run manager, built frontend in ui/static
frontend/              the web app source (React, TypeScript, Vite, Tailwind, React Flow)
docs/                  the approved designs: jira-ticket-implement, devflow-ui-plan, ticket-review, standup
tests/                 offline tests with a fake LLM, fake rtk/glab/MCP and real git repos; tests/ui has the API
                       tests and the demo server
workspace.example.yaml repos, checks, MCP servers, Jira tool names, models per step
langgraph.json         lets `langgraph dev` / LangGraph Studio load ticket_to_plan
```

## Adding a workflow

1. Copy a file in `workflows/` (or `jira_implement/` when it needs Jira, repos or checkpoints), define Pydantic
   output models and a `State` TypedDict.
2. Nodes call `get_llm().structured(system, prompt, Schema)`; add its steps to `routing.py` to pick a model.
3. Register it in `langgraph.json` (or call `register_workflow(id, factory)` / add a `devflow.workflows` entry point
   when it needs runtime dependencies) and add a subcommand in `cli.py`. The UI picks it up on start or on
   Settings, App, Re-scan workflows.
4. Optionally add a `DEVFLOW_UI` dict to the module: title, description, node labels, step order, a start form.
5. Add a test using `tests/fake_llm.py`.

## Status

Everything above is built and covered by offline tests (pytest plus Playwright e2e against the demo server). It has
not yet had a live run against real Jira, GitLab and Confluence, so MCP tool names, Jira status names, changelog
paging and `rtk` output may need adjusting on first use (tool names are overridable under `jira_tools`).

Not built yet: a Jira summary preview while typing the ticket key, an MR and thread preview in the address-review
start form, and phase 5 of the UI plan (light theme, desktop app).
