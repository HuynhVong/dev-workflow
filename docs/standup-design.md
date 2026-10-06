# Standup / work report workflow: design for approval

Status: APPROVED by Huynh on 2026-10-06 and built (`src/dev_workflows/jira_implement/standup.py`). Asked by Huynh, 2026-10-05.
Revised 2026-10-06: "Reviewed" is now found by the In Review transition + assignee, not by searching comments (Huynh).

It replaces today's `standup` (git log + notes → Yesterday / Today / Blockers) with a work report driven by Jira:
you pick a date range and paste your report template, devflow finds the tickets you worked on through the Jira MCP,
and Claude fills your template from those facts.

## What changes for you

| | Today (`standup`) | Proposed (`standup`) |
|---|---|---|
| Input | repos, `since`, git author, free notes | **from date**, **to date**, **report template** |
| Source | `rtk git log` across repos | Jira through the Jira MCP: JQL search, issue changelog, comments |
| What counts as work | every commit | only (1) tickets **you moved Approved → In Progress while assigned to you**, and (2) tickets **moved to In Review while assigned to you** (you are the reviewer) |
| Output | fixed Yesterday / Today / Blockers | your template, filled; plus the list of tickets it used |

## Inputs

```bash
devflow standup --from 2026-10-01 --to 2026-10-03 --template ~/reports/weekly.md [--no-input]
```

UI: New run → Standup → three fields:

| Field | Type | Default |
|---|---|---|
| From date | date picker (new form type `date`) | previous working day |
| To date | date picker | today |
| Report template | textarea (Markdown) | the template you used last time, else the built-in one below |

Both dates are inclusive, whole days, in your local timezone (taken from your Jira profile, else this machine).
"Myself" is the Jira account behind the Jira MCP connection (`currentUser()` / `jira_get_user_profile`), so there is
nothing to type for it. The last template you submitted is remembered in `.devflow/standup-template.md`.

Built-in template (used when you leave it empty):

```markdown
## Work report {{from}} → {{to}}

### Started (Approved → In Progress)
- [KEY] summary: one line on what it is

### Reviewed
- [KEY] summary: what I checked and the outcome

### Notes
```

Your template is free Markdown. Claude keeps its headings, order and wording and fills it with the facts only;
`{{from}}`, `{{to}}` and `{{me}}` are replaced by code before Claude sees it.

## Flow

```
 from, to, template
        │
 ┌──────▼────────────┐
 │ 0 resolve_me      │   Jira MCP: who am I (accountId, display name, timezone)
 └──────┬────────────┘
 ┌──────▼────────────┐
 │ 1 search_jira     │   two JQL searches → candidate tickets
 └──────┬────────────┘
 ┌──────▼────────────┐
 │ 2 verify_activity │   per ticket: changelog + comments → keep only real matches, with dates
 └──────┬────────────┘
 ┌──────▼────────────┐   ⏸ you: ticket list in two groups, untick any to leave out → continue / abort
 │   confirm_tickets │
 └──────┬────────────┘
 ┌──────▼────────────┐
 │ 3 digest_tickets  │   one line per ticket: what it is, what you did on it
 └──────┬────────────┘
 ┌──────▼────────────┐
 │ 4 fill_template   │   your template + facts → report
 └──────┬────────────┘
 ┌──────▼────────────┐   ⏸ you: read the report → accept / regenerate with a note / edit / abort
 │   review_report   │
 └──────┬────────────┘
        ▼ done: report on screen (Copy button) and in .devflow/runs/<run>/report.md
```

⏸ = checkpoint, same as the other workflows (CLI prompt or UI Approve panel, survives restarts, Abort always offered).
`--no-input` skips both checkpoints: every verified ticket is used and the first report is accepted.

## Steps in detail

| # | Step | What it does | Model | Skill |
|---|---|---|---|---|
| 0 | `resolve_me` | Calls `jira_get_user_profile` for the connected account: accountId, display name, email, timezone. Fails early with a clear message if the Jira MCP isn't reachable. | none | none |
| 1 | `search_jira` | Runs the two JQL queries below through `jira_search`, paging until done. Results are only candidates. | none | none |
| 2 | `verify_activity` | Fetches each candidate's changelog and comments and keeps the ones that really match the rules below, with the exact timestamp of each matching event. Deterministic code, no AI, so the ticket list is never guessed. | none | none |
| 2b | `confirm_tickets` ⏸ | Shows the two groups (key, summary, status now, when it matched, link). Untick to leave a ticket out. Continue, or Abort. | none | none |
| 3 | `digest_tickets` | Per kept ticket, one line from its summary, description and (for reviews) your comment text: what the ticket is and what you did. Batched in one call. | Haiku | `standup` |
| 4 | `fill_template` | Fills your template from the digest and facts. Rules: keep your structure, use only the listed tickets, never invent work, leave a section empty ("None") rather than guess. | Sonnet | `standup`, `work-report` |
| 4b | `review_report` ⏸ | Shows the report. Accept saves it. Regenerate redoes step 4 with your note. Edit lets you change the text before saving. | none | none |

Models follow your table: no AI for fetching and filtering, Haiku for the per-ticket summary, Sonnet for writing the
report. All at medium effort (Haiku has no effort setting). New routing entries: `standup.digest` (haiku) and
`standup.report` (sonnet), overridable under `ai.steps` in workspace.yaml like every other step.

## Which tickets count

Range: `start = from 00:00`, `end = to 23:59:59`, in your timezone. Statuses come from workspace.yaml so they match your
board: `standup.started_from: Approved`, `standup.started_to: In Progress`, `standup.review_status: In Review`.

**1. Started by you (Approved → In Progress, assignee is you)**

JQL candidates:
```
status CHANGED FROM "Approved" TO "In Progress" DURING ("<from>", "<to + 1 day>")
  AND assignee WAS currentUser()
```
Kept when the changelog has a status change Approved → In Progress inside the range **and** the ticket was assigned to
you at that moment (assignee replayed from the changelog, so a ticket reassigned to someone else afterwards still
counts, and one moved while it belonged to someone else doesn't). Who clicked the transition doesn't matter. If it
moved more than once in the range, it's listed once with the first time.

**2. Reviewed by you (moved to In Review, assignee is you)**

JQL candidates:
```
status CHANGED TO "In Review" DURING ("<from>", "<to + 1 day>")
  AND assignee WAS currentUser()
```
Kept when the changelog has a status change into In Review inside the range **and** the ticket was assigned to you at
that moment (same changelog replay as group 1; an assignee set in the same move, e.g. on the transition screen, counts).
Who clicked the move doesn't matter. Moved into In Review more than once in the range: listed once, first time.

Your comments on these tickets inside the range are read too, but only as material for the report line ("what I
checked and the outcome"). They are not required: a ticket with no comment from you still counts.

A ticket you started yourself (Approved → In Progress while assigned to you, any date) that you then moved to In Review
while still holding it is your own ticket going to review, not a review you did, so it is left out of group 2.

About "profile log": Jira's per-user activity stream isn't exposed by the Jira MCP (sooperset or Atlassian's own), so
devflow rebuilds the same picture from JQL + each ticket's changelog and comments. That's also more exact, because it
has timestamps and the assignee at the time.

## Rules it keeps

- **Read-only.** It never writes to Jira: no comment, transition or worklog. Confluence and git aren't used.
- It doesn't touch CI/CD, repos or MRs. No mail is sent or drafted (say if you want an Outlook draft of the report).
- The ticket list is decided by code, not by a model; Claude only words what code found.

## Reuse and new pieces

Reuse: the Jira MCP resolution (workspace.yaml, else the server connected to Claude Code), `GraphKit.checkpoint`, the
run manager/UI, telemetry and token tracking, routing for models and global skills.

New:
- `JiraGateway.search(jql)`, `changelog(key)` (via `jira_get_issue` with `expand=changelog`, or
  `jira_batch_get_changelogs` when available) and `comments(key)`; new `jira_tools` keys `search`, `get_changelog`.
- The graph moves to `src/dev_workflows/jira_implement/standup.py` (it needs the gateway and checkpoints); the old
  git-based `workflows/standup.py` is removed, along with its `--repo/--since/--author/--notes` flags.
- UI form type `date`, a ticket checklist in the approval panel (the ticket review's checklist editor), and a Copy button
  on the report.
- `devflow doctor` / Setup: adds the `search` tool to the Jira read checks.
- Offline tests with a fake Jira MCP: transitions in and out of range, reassigned tickets, timezone edges at midnight,
  your own ticket moved to In Review, reassignment around the In Review move, comments by others, paging, empty range.

## Needs a live check (can't be tested in the cloud)

- Your exact status names (is it "Approved" and "In Progress" on every project you work on?).
- Whether your Jira MCP returns the changelog through `jira_get_issue` (`expand=changelog`) on your Jira (Cloud vs Server).
- That your team assigns the reviewer when a ticket enters In Review (in the same move, or right before it).

## Defaults picked (say if you want otherwise)

1. "Myself" = the account connected to the Jira MCP. No field to type it.
2. "Assignee is myself" = assigned to you **at the time of the transition**, not necessarily now.
3. "Reviewed" = moved to In Review while assigned to you. Your comments only add detail to the report; they are not
   required. Your own ticket that you move to In Review isn't counted as a review.
4. Git commits are no longer read. (Alternative: keep an optional "include my commits" toggle.)
5. The report is shown and saved locally only; nothing is posted or mailed.

## As built (differences from the proposal)

- **Who "you" are.** With `jira_user` in workspace.yaml, devflow reads that profile (account id, name, e-mail,
  timezone). Without it, it takes the assignee of your most recently updated ticket (`assignee = currentUser()`).
- **JQL is only a pre-filter.** The searches use a window one day wider on each side, so timezone differences between
  Jira and you never drop a ticket. The exact range, in your timezone, is then applied to each ticket's history.
- **History source.** `jira_get_issue` with `expand=changelog` first. If your server returns no changelog there,
  `jira_batch_get_changelogs` is used (map another tool under `jira_tools.batch_changelogs`).
- **Settings.** `standup: {started_from, started_to, review_status, timezone}` in workspace.yaml, all optional.
- **Runs.** The standup is a new kind of devflow run: it needs Jira and has checkpoints but no ticket key. Run ids look
  like `standup-20261006-002717-5f2f`, and the report is saved to `.devflow/<run>/report.md`.
- **Setup.** `devflow setup` / `devflow doctor` now show "Jira MCP can search (JQL)". It is a warning, because only
  the standup needs it; the standup's own preflight fails without it.
- **Old standup removed.** The git-log standup and its `--repo/--since/--author/--notes` flags are gone.
- Screenshots: `docs/ui-built/standup-*.png` in the project folder.
