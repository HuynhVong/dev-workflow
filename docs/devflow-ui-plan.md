# Devflow UI: plan

Status: approved 2026-10-05 and built in phases 0 to 4 (phase 5 is optional polish).
Date: 2026-10-05. Repo: https://github.com/HuynhVong/dev-workflow (main, commit 7e98bba).

## 1. Goal

One local app that wraps every devflow workflow so a developer can:

0. Monitor **every** graph workflow in the repo, including ones added later, with no UI code needed for a new
   workflow (section 3.1).
1. Set up the project on first run, with every personal connection checked (MCP servers, CLIs, auth, skills, repos).
2. Start any workflow from a form instead of the CLI.
3. Watch a run live: the current node, every finished node with its result, the repos and waves, the decisions made.
4. Answer human checkpoints (approve plan, manual test, approve push, triage review threads...) right in the UI.
5. See the tokens (and cost) each node, model and run used.
6. Manage runs: resume, abort, reopen, and review the audit log and side-effect ledger.
7. Run several tickets at the same time (usually 3), each in its own git worktree (section 5.1).

The UI never adds powers the workflows do not have. It drives the same runner the CLI uses, so every existing
guardrail stays where it is: rtk for every git and glab call, Confluence read-only, no CI/CD, draft MRs only,
`--repos` as an allow-list, abort at every checkpoint, idempotent side effects.

## 2. What exists today (and what the UI needs from it)

| Already there | Where | What the UI needs |
|---|---|---|
| Run registry, lifecycle, audit log, side-effect ledger in SQLite | `jira_implement/ledger.py` (`devflow_runs`, `devflow_audit`, `devflow_effects`) | Read as is. Add an events table and a usage table (section 5). |
| LangGraph checkpoints in the same SQLite file | `runner.py` `SqliteSaver` | Read state for the run detail screen. |
| Two-node human checkpoints with `{name, payload, options}` | `graph.py` `GraphKit.checkpoint` | Payload is already structured, so each checkpoint can get a proper form. |
| Heartbeat per node | `GraphKit.node` | Already gives "current node"; the UI also needs node start/end events with output. |
| Run control: start, answer, resume, abort, status, show | `runner.py` `Session` | Wrap it in a background run manager so a run keeps going while the browser is closed. |
| Preflight checks | `graph.py` `check_environment`, `routing.py` `setup_report` | Refactor into one structured "doctor" that returns a list of checks for the setup screen. |
| Per-step model routing and global skills | `routing.py` `STEPS`, `SkillRegistry` | Show per node; edit overrides in Settings. |
| Claude calls | `llm.py` `ClaudeLLM.structured`, `coding_agent.py` (Agent SDK `query`) | Token usage is **not recorded today**. Capture `response.usage` and the Agent SDK `ResultMessage` usage/cost. |
| Simple workflows (ticket_to_plan, pr_review, standup) | `workflows/` | They run in memory without the Store. Bring them under the same runner so they show in the UI. |

## 3. Shape of the app

**Recommended: a local web app started with `devflow ui`.**

- A small Python server (FastAPI) runs in the same package, imports the runner directly, and serves a prebuilt
  frontend. `devflow ui` opens `http://127.0.0.1:<port>` in the browser.
- It runs on the developer's machine because the workflows need their MCP config, git clones, rtk, glab auth and the
  Claude Code CLI. Nothing is hosted.
- Runs execute in a background run manager inside that server, so closing the tab does not stop a run, and the CLI
  can still answer or inspect the same run (both share the SQLite file; the existing heartbeat stops two drivers).

Why not the alternatives:

- **LangGraph Studio** (`langgraph dev`) stays useful for debugging graphs, but it knows nothing about the ledger,
  repos, waves, setup checks, or what each checkpoint payload means, and it cannot run the guarded runner.
- **Desktop app (Tauri/Electron)** gives a dock icon and native notifications but adds a second build pipeline. The
  same frontend can be wrapped later with no rework, so it is a phase 5 option, not a starting point.

```
 Browser (React)  ──REST──►  devflow ui server (FastAPI, 127.0.0.1)
        ▲                      │  RunManager (thread per active run)
        └────────SSE───────────┤  Doctor (setup checks)
                               │  Session / graphs / Vcs / MCP clients   (unchanged guardrails)
                               ▼
                        runs.sqlite: checkpoints + runs + audit + effects + events + usage
```

### 3.1 Every workflow added to the repo shows up automatically

Huynh's requirement (2026-10-05): the UI monitors all graph workflows added to the repo, not a fixed list.

- **Discovery.** On start (and on a Refresh button) the server builds the workflow list from two sources:
  1. every graph in `langgraph.json` (today ticket_to_plan, pr_review, standup), and
  2. graphs that need runtime dependencies and so cannot sit in `langgraph.json` as a plain object (today Jira ticket
     implement and address-review), registered with one line: a `register_workflow(id, factory)` call or a
     `devflow.workflows` entry point in `pyproject.toml`.
  Adding a workflow to the repo means adding it to one of those two places, which the README's "Adding a workflow"
  steps will say. Extra repos with their own `langgraph.json` can be listed under `ui.graph_sources` in
  `workspace.yaml` if a team keeps workflows elsewhere.
- **Monitoring works for any graph with zero UI code.** The run screen is generic: the graph comes from
  `graph.get_graph()`, node start/finish and outputs come from `graph.stream()`, checkpoints from `interrupt()`
  payloads, tokens from the LLM wrapper. A brand-new workflow gets the graph view, timeline, node outputs (as a JSON
  tree), token counts, and approve/answer buttons on the first day.
- **Start form is generated** from the graph's input schema (`graph.get_input_jsonschema()`), so a new workflow can
  be started from the UI without writing a form. Required fields, enums and lists become inputs.
- **Optional polish, per workflow.** A workflow can ship a small metadata block (title, description, icon, nicer
  labels for nodes, a custom start form, a custom checkpoint form, output renderers for its node types). Without it
  the generic views are used. Today's five workflows get this polish in phases 2 to 4.
- **Interrupts in any shape.** devflow checkpoints send `{name, payload, options}`. A plain `interrupt(...)` from
  another graph (like ticket_to_plan's questions today) is shown as its payload plus a free-text or JSON answer box,
  so it can still be answered in the UI.
- **Runs from anywhere are visible.** Every run goes through the shared runner and SQLite file, whether started from
  the UI or the CLI, so the UI lists all of them.

### Visual direction

Huynh's reference for the look (2026-10-05): https://workflow-light-ui.lovable.app ("Flowstate, Workflow
control"). Reviewed 2026-10-05 by rendering it in Chromium at desktop (1440px) and phone (390px) widths and reading
its stylesheet. It is a one-page Lovable prototype of a LangGraph run monitor with demo data. The screens in section
4 follow it. Screenshots taken from it are in `ui-reference/` next to this file.

**Overall feel.** Despite "light" in its address, the site is **dark only** (`color-scheme: dark`, no light
tokens). Calm, dense, developer-tool look: near-black blue-grey surfaces, thin 1px borders instead of shadows, one
blue accent, and status colours used only as small soft-tinted pills and icon squares. No gradients, no
illustrations.

**Layout.**

```
┌──────────────┬─────────────────────────────────────────────────────────────────────────────────┐
│ ▣ devflow.   │ Workspace › Overview                       ● All connections OK   🔔•   (HV)   │  top bar
├──────────────┼─────────────────────────────────────────────────────────────────────────────────┤
│ WORKSPACE    │ ● LIVE WORKSPACE  3 of 3 slots busy                                [+ New run]  │  eyebrow + action
│ ▦ Overview   │ Workflow overview                                                               │  H1
│ ⑂ All runs 5 │ Monitor your runs and keep work moving.                                         │
│ ⛉ Approvals 2│ ┌Running ⚡┐ ┌Needs you ⛉┐ ┌Completed ✓┐ ┌Tokens today ⑂┐                          │  4 stat cards
│              │ └ 03 ─────┘ └ 02 ───────┘ └ 01 ───────┘ └ 412k · $4.10 ┘                          │
│ YOUR         │ ┌ Runs ──────────────┐ ┌ AQS-5512 · Jira ticket implement     ● Needs approval ┐ │
│ WORKFLOWS    │ │ [Search runs…]     │ │ Fix order total rounding                              │ │  master / detail
│ ● Jira impl. │ │ All Active Approval│ │ ⏱ Started 12m ago · 3 repos                           │ │
│ ● Address rv │ │ Failed Completed   │ │ Overview  Activity  Repos  Decisions  …               │ │
│ ● Ticket→plan│ │▌AQS-5512   Approval│ │ Execution  Step 5 of 9 · Awaiting input     [Abort]   │ │
│ ● PR review  │ │ AQS-5498   Running │ │ ✓ preflight  ✓ gather_context  ✓ analyze  ✓ plan      │ │
│ ● Standup    │ │ AQS-5470 Completed │ │ ⑤ approve_plan  Waiting   ○ implement  ○ …            │ │
│ ──────────── │ └────────────────────┘ │ ┌ Approval required        HUMAN IN THE LOOP ┐       │ │
│ (HV) Huynh   │                        │ └ [✓ Approve & continue] [Revise] [Abort]   ┘       │ │
└──────────────┴────────────────────────┴───────────────────────────────────────────────────────┘
```

- **Left sidebar** (about 228px, darker than the page, divided from it by a 1px border): logo mark (a blue rounded
  square with a node icon) and the word mark with a blue full stop ("devflow."). Section labels in tiny uppercase
  with wide letter spacing ("WORKSPACE", "YOUR WORKFLOWS"). Nav items with an icon, label and a right-aligned count;
  the active item has a raised background. Approvals' count is an amber badge. "Your workflows" lists every
  discovered workflow (section 3.1) with a coloured dot; clicking one filters the runs. The user block sits at the
  bottom (initials avatar, name, role line, a "…" menu).
- **Top bar** (about 72px): breadcrumb on the left (muted parent › bold current page). On the right a status
  indicator with a green dot (the site's "System operational"; in devflow it is the doctor result, amber or red when
  a connection check fails, and opens Settings > Connections), a bell with an amber dot when something waits on
  you, and the avatar.
- **Page header**: an eyebrow line (blue dot + "LIVE WORKSPACE" in small blue uppercase, then a muted note), a large
  bold title, a muted one-line subtitle, and the primary action as a blue button on the right ("+ New run").
- **Content**: a row of four stat cards, then a master/detail split: a Runs card (about 40% width) and the selected
  run's detail card (about 60%). Selecting a run swaps the right card in place; the site has no separate run page.
  "All runs" and "Approvals" in the sidebar are the same page with a filter applied.
- **Phone width**: the sidebar collapses to a menu icon in the top bar, stat cards go 2 by 2, the New run button
  moves under the title, and the list and detail stack.

**Palette** (the site's tokens; oklch with an approximate hex, all hue 260 blue-grey unless noted). These become
the Tailwind/shadcn CSS variables as they are, so shadcn components pick them up with no restyling.

| Token | Value | ≈ Hex | Used for |
|---|---|---|---|
| `--sidebar` | oklch(12% .012 260) | #04060a | sidebar |
| `--background` | oklch(14.5% .012 260) | #070a0f | page |
| `--surface` | oklch(16.5% .012 260) | #0b0e14 | inner panels (the step list box) |
| `--card` | oklch(18% .014 260) | #0e1218 | cards |
| `--surface-raised` / `--popover` | oklch(20.5–21% .015 260) | #13171e | active nav item, menus, modal |
| `--muted` / `--secondary` | oklch(24–24.5% .012 260) | #1c1f25 | chips, selected filter, hover |
| `--accent` | oklch(27.5% .02 260) | #222832 | hover on raised items |
| `--track` | oklch(29% .012 260) | #282c31 | progress tracks, connector lines not yet reached |
| `--border` / `--input` | oklch(31–32% .012 260) | #2d3137 | every 1px border, inputs |
| `--muted-foreground` | oklch(66% .015 260) | #8d939c | secondary text, captions |
| `--foreground` | oklch(97% .006 260) | #f3f5f9 | primary text |
| `--primary` (+ `-soft` at 14%) | oklch(66% .19 256) | #3491ff | buttons, links, focus ring, active tab, selected row, **running** |
| `--success` (+ `-soft` at 12%) | oklch(75% .16 155) | #42cb80 | done steps, Completed, Approve button, OK checks |
| `--warning` (+ `-soft` at 12%) | oklch(81% .14 82) | #eeb747 | **waiting on you**, approval panel, warnings |
| `--destructive` | oklch(67% .19 25) | #f45a56 | failed, Abort, blocking check failures |

Status mapping in devflow: Running = primary, Needs you (`WAITING_HUMAN`) = warning, Completed = success, Failed =
destructive, Aborted / not reached / skipped = muted. A status is always a pill (soft-tinted background, a 6px dot
and the label in the full colour), never a solid block.

**Typography.** Inter (400, 500, 600, 700) from Google Fonts, bundled locally in our build since the app runs
offline. The UI system monospace stack for ids: run ids, ticket keys, branch names, node ids and token counts.
Sizes are mostly 12 to 14px (`text-xs` / `text-sm`); card titles 16px semibold; page title about 32px bold with
tight letter spacing; stat numbers about 28px bold and zero-padded to two digits ("04"). Eyebrows and section labels
are 11 to 12px uppercase with wide tracking (`tracking-wider` / `tracking-widest`).

**Shape and motion.** Cards and the modal have a 12px radius (`--radius-xl`), buttons, inputs and pills 6 to 8px.
1px borders everywhere, no drop shadows except the modal. Transitions are 150ms. The only animation is a slow pulse
on the current step and on live dots.

**Components to reuse from the site.**

- *Stat card*: muted label top left, a 32px rounded icon square in the status colour's soft tint top right, a big
  two-digit number, a muted caption ("Waiting on your decision").
- *Runs list card*: title + subtitle + count on the right, a search input with a magnifier, a filter icon then
  segmented filter chips (selected chip on `--muted`), and rows of: bold title, muted "workflow · RUN-ID" line (id
  in mono), status pill, relative time on the right, a chevron. The selected row gets a 2px blue left border and
  the `--primary-soft` background.
- *Run detail card*: a mono meta line (RUN-ID · workflow), the run title (20px bold), a muted description, a meta
  row (clock + "Started 2 min ago", initials avatar + who started it), the status pill top right, then underline
  tabs (active tab white with a 2px blue underline).
- *Execution stepper* ("Execution graph" on the site): a header with "Step 4 of 5 · Awaiting input" and an
  outlined action button on the right, then a bordered inner panel with a vertical list of steps. Each step has a
  32px rounded square marker joined by a vertical line: done = green check on green tint with a green line below,
  current = blue step number on blue tint with "In progress" in blue on the right, waiting = amber number with
  "Waiting" in amber, not reached = grey number with a plain border. Title in white, one muted line of detail.
- *Approval panel*: a card with an amber border and faint amber tint under the stepper; a shield icon in an amber
  square, bold "Approval required", "HUMAN IN THE LOOP" eyebrow in amber on the right, the question, an optional
  "Decision note" textarea, then a solid green "✓ Approve & continue" and an outlined "✕ Reject".
- *Activity timeline*: a vertical line with 32px circular markers holding a small pulse icon; the latest event is
  blue, earlier ones grey; each row is the event text plus a muted second line.
- *Modal*: dimmed page, a `--popover` card with a title, a muted subtitle, labelled inputs (focused input gets a
  blue ring), and Cancel (outlined) + primary (with a play icon) buttons bottom right.
- *Footer hint*: a small muted line with an info icon at the bottom of the page (devflow uses it for "Runs started
  from the CLI show here too").

**What devflow changes from the site.** The site's stepper is a straight line of five demo steps; devflow's graphs
branch, loop and fan out per repo, so the stepper is the default view and a React Flow graph in the same colours is
one toggle away (section 4.4). The site's Pause button has no runner equivalent, and the UI adds no powers, so that
slot holds Abort (and Resume for a failed run). Reject becomes the options the checkpoint actually offers. The demo
data banner and the Lovable badge are dropped.

### Frontend stack

React + TypeScript + Vite, Tailwind with shadcn/ui components, React Flow for the graph view, TanStack Query for
data, a Monaco or `react-diff-view` diff viewer, and Recharts for token charts. Dark theme first, matching the
reference (which is dark only); a light theme derived from the same tokens is a phase 5 option. The build
output ships inside the Python package, so users never run npm.

## 4. Screens

### 4.1 First run: Setup wizard

Opens automatically when no `workspace.yaml` exists or the last doctor run had a blocking failure. Can be reopened
any time from Settings > Connections.

1. **Workspace.** Create or pick `workspace.yaml`. Add repos with a folder picker (name, path, has UI, default
   branch `develop`), Jira user, state dir. Shows the YAML it will write before saving.
2. **Connections.** A grid of check cards in the stat-card style (icon square tinted success / warning /
   destructive, a status pill), each with the detail and a "how to fix" line and an outlined Re-check button:

   | Group | Checks |
   |---|---|
   | AI | `ANTHROPIC_API_KEY` set (value never shown), one tiny call per model tier reachable (optional button, costs a few tokens) |
   | CLIs | `claude` (Claude Code) installed and logged in, `rtk` installed, `rtk git --version`, `rtk glab auth status` |
   | Jira MCP | server reachable, every read **and** write tool the workflows need is present |
   | Confluence MCP | server reachable, read tools present, write tools listed as "blocked by devflow" (never called, not even to test) |
   | Playwright MCP | configured when any repo has `has_ui: true` |
   | Repos | is a git repo, clean working tree, `origin` reachable, `develop` exists |
   | Optional | other MCP servers from the user's Claude Code config, shown so they can be imported into `workspace.yaml` (read only, nothing is changed in Claude Code's config) |

3. **Skills and models.** One row per AI step: model tier (Haiku / Sonnet / Opus), effort shown as "medium"
   (locked), preferred skills with installed / missing, and a dropdown to map a step to a skill the developer
   already has. Missing skills are warnings, never blockers, as today.
4. **Done.** Summary of blocking vs warning items. "Start your first run" button.

All checks are read-only. Nothing is created in Jira, GitLab or Confluence during setup.

The wizard is a centred single column (about 720px) inside the normal shell, with a step indicator built from the
same square step markers as the execution stepper.

### 4.2 Home: Overview

The site's Overview page, with devflow content. Sidebar "Overview", "All runs" and "Approvals" are this page with a
filter applied; clicking a workflow under "Your workflows" filters by workflow.

- **Header**: eyebrow "● LIVE WORKSPACE" with a muted note of parallel slots in use ("2 of 3 slots busy", from
  `max_parallel_runs`), title "Workflow overview", and the blue **+ New run** button.
- **Stat cards**: Running (blue), Needs you (amber, "Waiting on your decision"), Completed today (green), Tokens
  today with estimated $ (neutral). A fifth Failed card (red) appears only while a resumable failed run exists.
  Clicking a card applies its filter.
- **Runs card** (left): search by ticket key or title, filter chips All / Active / Needs you / Failed / Completed,
  plus a workflow dropdown. Rows are sorted with runs waiting on you first, then running, failed, recent. Each row:
  ticket key and title, "workflow · run id" in mono, status pill, relative time. Rows also carry what the old lanes
  showed:
  - *Needs you*: the checkpoint in plain words ("Approve the plan", "Manual test", "Approve push") and how long it
    has waited.
  - *Running*: current node with a pulsing dot, small repo chips with per-repo status, elapsed time, tokens so far.
  - *Failed* (resumable): the node it failed in and the error line.
  - *Completed / aborted*: MR links from the ledger.
- **Run detail card** (right): the selected run, exactly as section 4.4 describes, so most work (including
  answering checkpoints) happens without leaving Home. Selection is kept in the URL (`/runs/<id>`), so a run can be
  linked and opened full width.
- Tab title and favicon badge show the number of runs waiting on you, mirrored by the amber Approvals count in the
  sidebar and the amber dot on the bell; optional browser notification when a run starts waiting or fails. The bell
  lists those events.
- Footer hint: "Runs started from the CLI show here too."

### 4.3 Start a run

A **"Start a new run" modal** opened from **+ New run** (or from a workflow in the sidebar, preselected), styled
like the site's: title, the subtitle "Launch another workflow alongside your active runs.", a Workflow select (one
entry per discovered workflow, with its coloured dot), then that workflow's fields, and Cancel + a blue
**▷ Check & start** button. Long forms scroll inside the modal. Workflows without a custom form get one generated
from their input schema. Today's five get these custom forms:

| Workflow | Form |
|---|---|
| Jira ticket implement | ticket key (Jira summary previewed once typed), repos multi-select from the workspace (empty = all) |
| Address review | ticket key; shows the draft MRs found and their unresolved thread count before starting |
| Ticket to plan | ticket key or pasted text / file, optional context doc |
| PR review | MR picker (or paste a diff), title |
| Standup | repos, author (defaults to `git config user.email`), notes |

Preflight runs inline before the run is created, using the same doctor checks scoped to the chosen repos; results
show as small status pills under the form, and a blocking failure keeps the start button disabled. The form also
shows which model each step will use. When all parallel slots are busy, the button reads "Queue run".

### 4.4 Run detail (the main screen)

The site's run detail card. It shows as the right half of Home, or full width at `/runs/<id>`, where the node
detail opens beside the stepper instead of below it.

```
┌ AQS-5512 · Jira ticket implement                                               ● Running ┐
│ Fix order total rounding                                                                 │
│ Round order totals per line, not per order, across api and portal.                       │
│ ⏱ Started 12m ago   (HV) Huynh   ⑂ 3 repos   182k tok · $1.94   [Copy CLI command]        │
├──────────────────────────────────────────────────────────────────────────────────────────┤
│ Overview   Activity   Repos   Decisions   Side effects   Audit log   Tokens               │
├──────────────────────────────────────────────────────────────────────────────────────────┤
│ Execution                                       [Steps | Graph]              [✕ Abort]   │
│ Step 6 of 9 · implement · api-service, wave 1                                            │
│ ┌──────────────────────────────────────────────────────────────────────────────────────┐ │
│ │ [✓] preflight            Workspace and repos checked                        0:04      │ │
│ │ [✓] gather_context       Jira, Confluence (read only), repos                0:41      │ │
│ │ [✓] analyze_requirements 6 requirements, 2 open questions                    1:12      │ │
│ │ [✓] plan_implementation  7 steps across 3 repos                              2:30      │ │
│ │ [✓] approve_plan         Approved by Huynh                                   —         │ │
│ │ [6] implement ×2         shared-lib ✓  api-service ◉ 1/3  web-portal ○   In progress │ │
│ │ [7] run_checks                                                                        │ │
│ │ [8] manual_test          Waits for you                                                │ │
│ │ [9] approve_push                                                                      │ │
│ └──────────────────────────────────────────────────────────────────────────────────────┘ │
│ Node: implement · api-service · attempt 1/3 · Sonnet 5.5 · medium · 64k / 9k tok         │
│ Output   Diff   Input   Logs                                                             │
│   Read  src/main/.../OrderService.java                                                   │
│   Edit  src/main/.../OrderService.java (+42 −3)                                          │
│   Denied  rtk git commit (the workflow commits, not the agent)                           │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

- **Header**: as the site's: mono meta line (ticket key · workflow), title from the Jira summary, a muted one-line
  description, a meta row (started, by whom, repo count, live tokens and $), the status pill top right. When the
  run waits on you the pill reads "Needs approval" in amber.
- **Steps view** (default, the site's execution stepper): one row per graph node in the order the graph runs
  them, with the site's square markers and colours: done (green check), running (blue number, pulsing, "In
  progress"), waiting for you (amber number, "Waiting"), failed (red, with the error line), skipped and not reached
  (grey). The header line reads "Step N of M · current node · repo, wave". Each row adds what the site leaves out:
  a short result ("Plan: 7 steps across 3 repos"), duration, and for nodes that run per repo, per wave or per fix
  loop a "×N" count and inline repo chips. The `_wait` half of each checkpoint is folded into one step so the list
  reads like the design doc. Workflows with no metadata use the node ids as titles.
- **Graph view** (toggle): the real LangGraph graph (from `graph.get_graph()`), laid out with React Flow on the
  `--surface` panel, nodes drawn as the same marker plus title cards and coloured by the same states. Edges actually
  taken are drawn in `--success`, the rest in `--track`. Nodes that ran several times show a count badge. Useful
  for branching and loops; the toggle is remembered per workflow.
- The outlined action on the right of the "Execution" header is **Abort** while running or waiting, **Resume** for
  a failed run, and absent once finished. There is no Pause (the runner has none).
- **Activity tab** (the site's activity timeline, replacing the separate Timeline view): every node execution and
  event in order, newest first, which handles loops and waves better than a static graph. Round markers, the
  latest in blue. Each row: node, repo, start, duration, model, tokens, short result ("Checks: 2 failed"), and
  checkpoint answers inline.
- **Repos panel**: one row per repo in scope with wave, status, branch, fix attempts used out of 3, MR link once
  created.
- **Node detail** (click any node or timeline row):
  - *Output*, rendered by type: requirements and plan as readable sections, DAG as a small graph, review findings
    as a table, check results as pass/fail with the log tail, summaries as text. Unknown shapes fall back to a JSON
    tree.
  - *Diff*: files the coding agent changed in that node, from the local clone (read through `rtk git diff`).
  - *Input*: the state the node received (collapsed by default).
  - *Logs*: live Claude Code activity for coding nodes (tool calls, edits, rtk commands, and tool calls the policy
    denied), plus model, skills used, tokens and duration.
- **Tabs** under the header, in the site's underline style: Overview (stepper or graph, approval panel, node
  detail), Activity, Repos, Decisions (every checkpoint answer with note and time), Side effects (the ledger: branch
  created, pushed, MR opened, Jira moved, with their status), Audit log, Tokens. In the half-width layout on Home
  the tabs after Activity sit behind a "More" menu.

### 4.5 Human checkpoints in the UI

When a run waits, the Overview tab shows the site's **approval panel** directly under the waiting step (amber
border and faint amber tint, shield icon, the checkpoint title in plain words, a "HUMAN IN THE LOOP" eyebrow, the
form, and a "Decision note (optional)" box that becomes the answer's note). The same panel opens from a Needs-you
row and from the Approvals filter. The approving option is the solid green button ("✓ Approve & continue"), Abort
is an outlined red button, every other option is an outlined neutral button. Each checkpoint gets its own form,
built from the payload it already sends:

| Checkpoint | Form |
|---|---|
| `clarify` | the open questions, an answer box for each; Answer / Proceed without answers / Abort |
| `approve_plan` | plan steps, DAG of repos and waves, validation errors, risk and warnings highlighted; Approve / Revise (note) / Abort |
| `branch_ownership` | branches that already exist and who owns them; the options the graph offers |
| `scope_request` | which repo outside `--repos` the agent wants and why; Approve / Deny / Abort |
| `budget_exhausted` | repo, attempts used (3 of 3), last failure; Fixed by hand (re-run checks) / Abort |
| `manual_test` | per repo: what changed, how to run it, what to test; OK / Feedback (text, optional screenshot paths) / Abort |
| `route_ask` | the feedback items the router could not place, with a repo picker per item |
| `approve_push` | exactly what will happen: per repo branch, commits, draft MR target `develop`, merge order, scope gaps, Jira transition; Approve / Abort |
| `push_blocked` | the push problems and the "never force-push" hint; Retry / Abort |
| address-review `triage` | table of review threads, a Fix / Answer only / Skip toggle per row, with the comment and file shown |
| `manual_retest` | as `manual_test` |
| anything new | generic renderer: payload as readable JSON plus one button per option |

Rules: buttons come from the payload's `options` only; the server re-validates the answer (the graph already does);
Abort always asks for confirmation and an optional note; answers are written to the audit log as today.

### 4.6 Tokens and cost

- Per run: total input, output, cache-read and cache-write tokens, and estimated cost. Stacked bars by node and by
  model; a table of every Claude call (node, repo, model, tokens, duration).
- Across runs: tokens per day and per workflow, the most expensive steps, cache hit rate.
- Cost uses a price table in `workspace.yaml` that the developer can edit; tokens are always shown even if no price
  is set.
- Live: the header counter updates as each call finishes.
- Laid out like Home: a row of stat cards (input, output, cache hit rate, estimated $) above the charts. Charts use
  the palette's blue for output, a lighter blue for input, and muted greys for cache tokens, with green and amber
  kept for status only.

### 4.7 Settings

- Workspace editor (form over `workspace.yaml`: repos, MCP servers, Jira user, state dir).
- Model per step overrides (tier picker; effort shown as medium and locked, per Huynh's rule).
- Skill mapping per step.
- Connections: re-run the doctor.
- Notifications, theme (dark only until a light theme is added in phase 5), port.
- Settings reuses the shell: a second-level list on the left of the content area and form cards on the right.

## 5. Backend changes needed

1. **Workflow registry by discovery** (section 3.1): reads `langgraph.json`, the `register_workflow` / entry-point
   registrations and any `ui.graph_sources`, so the server, CLI and UI always list the same workflows, however many
   are added. Every workflow, including the three simple ones, runs under the Store-backed runner (run id,
   lifecycle, audit, events, usage); `GraphKit`-style event hooks are applied by the runner from the outside, so a
   new graph does not have to use `GraphKit` to be monitored. `build_graph()` stays for LangGraph Studio.
2. **RunManager.** Starts and resumes runs on a background thread (one per active run), using `graph.stream()`
   instead of `invoke()` so node start/finish events come out as they happen. Answers and aborts go through the
   existing `Session.answer` / `Session.abort`. A process restart marks orphaned RUNNING runs stale, exactly like
   today's heartbeat logic, and the UI offers Resume.
3. **Events table** `devflow_events(run_id, seq, at, kind, node, repo, data)`: `node_started`, `node_finished`
   (with a trimmed output), `checkpoint_waiting`, `decision`, `agent_activity` (coding agent tool calls), `usage`,
   `status`. Written by `GraphKit.node`, `GraphKit.checkpoint` and the coding agent, so the CLI produces the same
   history as the UI. The UI replays it on page load and follows it live over SSE.
4. **Usage capture.** `ClaudeLLM.structured` records `response.usage` (input, output, cache creation, cache read)
   with run id, node, repo, step and model; the coding agent records the Agent SDK `ResultMessage` usage and cost.
   The current run and node reach the LLM through a context variable set in `GraphKit.node`, so no node signature
   changes. Stored in `devflow_usage`.
5. **Doctor.** `check_environment` and `setup_report` become `doctor.run(scope) -> list[Check]` with
   `{id, group, label, status: ok|warn|fail, blocking, detail, fix}`. Preflight raises on blocking failures as now;
   `devflow doctor --json` prints the same list.
6. **HTTP API** (all local):

   | Method | Path | Purpose |
   |---|---|---|
   | GET | `/api/doctor` / POST `/api/doctor/run` | setup checks |
   | GET/PUT | `/api/workspace` | read / save `workspace.yaml` (validated) |
   | GET | `/api/workflows` | discovered workflows with generated form schemas and graph structure; POST `/api/workflows/refresh` re-scans |
   | GET | `/api/runs`, `/api/runs/{id}` | list, detail (run + state + repos + pending checkpoint) |
   | POST | `/api/runs` | start (runs preflight first) |
   | POST | `/api/runs/{id}/answer`, `/resume`, `/abort` | run control, same rules as the CLI |
   | GET | `/api/runs/{id}/events?after=seq` | history; `/api/runs/{id}/stream` is the SSE feed |
   | GET | `/api/runs/{id}/nodes/{exec}` | one node execution: output, input, logs, usage |
   | GET | `/api/runs/{id}/diff?repo=` | local diff through `rtk git` |
   | GET | `/api/usage?from=&to=` | token and cost aggregates |

7. **Local security.** Bind to 127.0.0.1 only, a random token in the launch URL stored as a cookie (like Jupyter),
   Host header check against DNS rebinding, no CORS. Secrets are never sent to the browser; the UI only sees "set"
   or "missing".

## 5.1 Parallel runs with git worktrees

Huynh's requirement (2026-10-05): implement several tickets at once, usually 3, with each run working in its own git
worktree.

**Today this is not safe.** Every run edits the one clone listed in `workspace.yaml`: branch setup runs
`rtk git checkout -b <TICKET>` in `repos[].path` (`graph.py:520-554`, `address_review.py:227`), and the coding agent,
checks and manual test all use that same path (`ws.repo(r).path`). Two tickets that touch the same repo would switch
each other's branch, or the second fails preflight because the tree is dirty. Tickets on completely different repos
would work in two terminals, but nothing guards against overlap.

**Proposed design**

- **One worktree per run per repo.** `repos[].path` stays your normal clone and becomes the source only; devflow
  never edits or switches it. Each run gets `<worktree_root>/<TICKET>/<repo>`, with `worktree_root` defaulting to
  `~/devflow-worktrees` (configurable in `workspace.yaml`).
- **Branch setup with worktrees.** `rtk git fetch origin`, then
  `rtk git worktree add -b AQS-5512 <worktree_root>/AQS-5512/api-service origin/develop`, so the branch is still cut
  from freshly fetched develop and named by the bare ticket key. If the branch already exists (a resumed run or
  address-review), `rtk git worktree add <path> AQS-5512` reuses it, and an existing worktree for that ticket is
  reused as is. Address-review works in the ticket's worktree.
- **The run remembers its paths.** The scope saved in the run state maps each repo to its worktree path. Every node
  (coding agent roots, check commands, manual test payload, diffs, push) reads paths from the run's scope instead of
  `ws.repo(r).path`. The coding agent's file sandbox therefore covers only that run's worktrees, so two parallel
  runs cannot touch each other's files.
- **Preflight changes.** The main clone no longer has to be clean (it is not edited). Preflight checks that the
  worktree path is free or belongs to this ticket, and that the branch is not checked out in another worktree.
- **Vcs policy.** Allow `rtk git worktree add` and `rtk git worktree list`. Removing a worktree is never automatic:
  the UI offers "Clean up worktree" for finished or aborted runs only when the worktree has no uncommitted changes,
  and it never deletes the branch.
- **Per-worktree setup.** A fresh worktree has no `node_modules`, build output or local env files. Each repo can
  list `setup_commands` (for example `npm ci`) and `copy_files` (for example `.env.local`) run once after the
  worktree is created.
- **Cross-repo links.** Check commands get `DEVFLOW_WORKTREE_<REPO>` environment variables pointing at the sibling
  worktrees of the same run, so a backend can build against that run's version of the shared lib.

**Running several at once**

- **Parallel runs.** The RunManager runs up to `max_parallel_runs` at once (default 3, configurable). More are
  queued as PENDING and start when a slot frees up. Runs waiting for you do not hold a slot.
- **Locks.** One active run per ticket. A repo+branch can only be in one worktree, which git itself enforces.
- **Shared resources.** SQLite runs in WAL mode so several runs write safely. Each run gets its own MCP client
  sessions. Check commands that use a fixed port or a shared local database can clash between runs; a repo marked
  `parallel_checks: false` runs its checks one run at a time through a repo lock.
- **Manual test.** Each ticket is tested from its own worktree. The manual test form shows the worktree path with
  "Open in VS Code" and "Copy path" buttons. If your app needs a fixed port, test one ticket at a time; the other
  runs simply wait at their checkpoint.
- **In the UI.** Home shows the parallel runs side by side with one shared Needs-you queue across tickets, so you
  can approve AQS-5512's plan while AQS-5513 is coding. A Worktrees page lists every worktree with its ticket, run
  status, branch, uncommitted changes and disk size. The tokens header also shows the total for all running runs.
- **CLI too.** `devflow implement AQS-5512` and `devflow implement AQS-5513` in two terminals use the same worktree
  logic and locks.

## 6. What the UI will never do

No merge, approve, un-draft, force-push, rebase or branch-delete buttons. No Confluence edits, not even from setup.
No pipeline or CI/CD views. No repo outside the run's scope without the `scope_request` checkpoint. No git or glab
call that is not routed through `Vcs` (and so through rtk). No action the CLI cannot also take.

## 7. Delivery phases

Each phase ends with a demo and tests, and each is proposed for approval before it is built.

| Phase | Scope | Done when |
|---|---|---|
| 0. Parallel runs with worktrees | worktree branch setup, run-scoped paths in every node, preflight and Vcs policy changes, `setup_commands` / `copy_files`, ticket and repo locks, `max_parallel_runs` queue, CLI support. Proposed as its own design for approval first, since it changes the implement and address-review workflows | three tickets touching the same repos run at once in the offline harness without touching each other or the main clone |
| 1. Backend foundation | discovery registry (any graph in the repo), RunManager on `stream()`, events and usage tables, usage capture, doctor, HTTP API, `devflow ui` serving a placeholder page | a run of any discovered graph, including a test-only graph added just for this, produces the full event and usage history; existing 79 tests still pass plus new ones |
| 2. Setup and runs | setup wizard, Home overview (stat cards, runs list with Needs-you first), Start run modal | a fresh machine goes from no `workspace.yaml` to a started run without the terminal |
| 3. Run detail and approvals | steps and graph views, activity tab, approval panel, repos panel, node detail, all checkpoint forms, abort/resume | a full Jira ticket implement run can be driven end to end from the browser |
| 4. Tokens, live agent log, settings | token views and charts, coding agent activity stream, diff viewer, settings editor, notifications | per-node token and cost numbers match the API usage records |
| 5. Polish (optional) | keyboard shortcuts, a light theme from the same tokens, desktop wrapper (Tauri), packaging the frontend build into the wheel | `pip install` then `devflow ui` works with no Node installed |

Testing: backend with the existing offline harness (fake LLM, fake rtk/glab/MCP) plus API tests; frontend with
Playwright end-to-end tests that drive a fake run through every checkpoint.

## 8. Decisions for Huynh

Defaults are marked; the plan above assumes them.

1. App form: **local web app via `devflow ui` (recommended)** or a desktop app from day one.
2. Frontend stack: **React + TypeScript + shadcn/ui + React Flow (recommended)** or something you prefer.
3. Cost: **show estimated $ next to tokens, with an editable price table (recommended)** or tokens only.
4. Decided by Huynh: the UI monitors every graph workflow in the repo, current and future (section 3.1).
5. **CLI and UI keep working side by side on the same runs (recommended)** or the UI becomes the only driver.
6. Next step after approval: a clickable design mockup of Home, Run detail and one checkpoint form before phase 1,
   or go straight to phase 1.
7. Parallel runs: **worktrees under `~/devflow-worktrees/<TICKET>/<repo>` (recommended)** or another location;
   **at most 3 runs at once (recommended)**; **phase 0 (worktrees) is built before the UI (recommended)**, since the
   UI's parallel view depends on it.
