# Ticket review workflow: design for approval

Status: APPROVED by Huynh on 2026-10-05 and built (`src/dev_workflows/jira_implement/ticket_review.py`). Replaces the user-facing "PR review" (diff in, Markdown out) with a review driven by a
Jira ticket and the commits that implement it. Asked by Huynh, 2026-10-05.

## What changes for you

| | Today (`pr_review`) | Proposed (`ticket_review`) |
|---|---|---|
| Input | title, pasted diff, optional description | Jira ticket key + commit SHA(s) per changed repo |
| Knows the requirement | no | yes, reads the ticket (and linked Confluence, read-only) |
| Tests | none | test plan you approve, then Playwright E2E with screenshots |
| Output | Markdown on screen | Jira comment + screenshot attachments, posted only after you approve |

## Inputs

```bash
devflow review AQS-5512 --commit web-portal=3f9a1c2 --commit api-service=88be0d4,a17c3e9
```

UI: New run → Ticket review → ticket key, then one row per repo (repo picked from workspace.yaml, one or more SHAs).
Repos come from workspace.yaml, so the workflow knows each repo's path, base branch and `run` command.

## Flow

```
 ticket key + SHAs per repo
           │
 ┌─────────▼──────────┐   ⏸ you: refresh + checkout the branch that has the commits, start the app
 │ 0 checkout_gate    │◄──┐ devflow then verifies each repo (read-only git); not OK → asks again
 └─────────┬──────────┘───┘
 ┌─────────▼──────────┐
 │ 1 understand_ticket│   Jira (read) + Confluence (read-only) → requirement + acceptance criteria
 └─────────┬──────────┘
 ┌─────────▼──────────┐
 │ 2 code_review      │   existing lens review per repo on the listed commits + "meets the ticket?" lens
 └─────────┬──────────┘
 ┌─────────▼──────────┐
 │ 3 test_plan        │   test cases mapped to acceptance criteria, each auto or needs-you
 └─────────┬──────────┘
 ┌─────────▼──────────┐   ⏸ you: tick the cases to run, edit, add notes → approve / regenerate / abort
 │   approve_test_plan│
 └─────────┬──────────┘
 ┌─────────▼──────────┐   Playwright MCP runs each case, screenshot per proof point
 │ 4 e2e_test         │◄─┐⏸ a case needs you (OTP, captcha, real login…): you do it in the
 └─────────┬──────────┘──┘  open browser, click Continue / Skip case
 ┌─────────▼──────────┐   ⏸ you: see pass/fail + screenshots → accept / retest selected / abort
 │   review_results   │
 └─────────┬──────────┘
 ┌─────────▼──────────┐
 │ 5 draft_comment    │   ticket summary, commits, code verdict, test table, screenshots
 └─────────┬──────────┘
 ┌─────────▼──────────┐   ⏸ you: preview exactly what goes to Jira → approve / edit / regenerate / abort
 │   approve_comment  │
 └─────────┬──────────┘
 ┌─────────▼──────────┐
 │ 6 post_to_jira     │   upload screenshots, add one comment (idempotent, never posted twice)
 └─────────┬──────────┘
           ▼ done
```

⏸ = checkpoint. The run waits there (CLI prompt or UI Approve panel), survives restarts, and Abort is always offered.

## Steps in detail

| # | Step | What it does | Model | Skill |
|---|---|---|---|---|
| 0 | `checkout_gate` ⏸ | Shows per repo what to run in your own clone: `rtk git fetch origin`, `rtk git checkout <branch>`, `rtk git pull`. It suggests the branch from `rtk git branch -r --contains <sha>`. Also asks the app URL (default from workspace.yaml). After you confirm, it checks each repo: every SHA is in HEAD (`rtk git merge-base --is-ancestor`), the tree is clean, and HEAD isn't behind its remote. On failure it says which check failed and asks again. devflow never checks out, pulls or switches your clone itself. | none | none |
| 1 | `understand_ticket` | Fetches the ticket (summary, description, acceptance criteria, attachments, images) and reads linked Confluence pages (read-only). Writes the requirement in plain words, a numbered acceptance-criteria list, and anything unclear. | Haiku (fetch), Sonnet (analysis) | `confluence-read`, `requirements-analysis` |
| 2 | `code_review` | Diff = the listed commits only (`rtk git show <sha>` per SHA). Runs the current triage → parallel lenses → verdict engine per repo, plus a new **requirement** lens: is each acceptance criterion implemented, and is anything unrelated changed? Findings are kept for the comment. It does not stop the run. | Haiku triage, Sonnet lenses | `code-review` |
| 3 | `test_plan` | E2E test cases, each with an id, the AC it proves, steps, expected result, `auto` or `needs-you`, and the screenshot(s) that prove it. | Opus (planning) | `playwright`, domain skills |
| 3b | `approve_test_plan` ⏸ | A checkbox list. You untick, edit or add cases and leave a note. Approve runs the ticked cases. Regenerate redoes step 3 with your note. | none | none |
| 4 | `e2e_test` | A Claude Code session with only the Playwright MCP runs one case at a time against the app URL. It can't edit code. It saves screenshots to `.devflow/runs/<run>/evidence/<case>-<n>.png`. When a case is `needs-you`, or the agent hits a step it can't do, the run pauses with "do X in the open browser", then Continue or Skip. The browser keeps its session between cases. | Sonnet | `playwright` |
| 4b | `review_results` ⏸ | Pass/fail per case with its screenshots. Accept goes on to the comment. Retest re-runs ticked cases, for example after you fixed something. Abort stops. A failure is reported in the comment, never hidden. | none | none |
| 5 | `draft_comment` | Writes the comment: ticket summary, commits reviewed per repo, code review verdict plus blocker/major findings, a test table (case, AC, result) and the screenshots inline. | Haiku | `implementation-summary` |
| 5b | `approve_comment` ⏸ | Shows the exact comment with images. Approve posts it. Edit lets you change the text. Regenerate takes a note. Abort posts nothing. | none | none |
| 6 | `post_to_jira` | Uploads the screenshots as ticket attachments and adds one comment that embeds them. A hidden marker plus the side-effect ledger means a resume or retry updates the same comment, never a second one. Status isn't changed. | none | none |

## Rules it keeps

- Every git call is `rtk git …` through `Vcs`, and only read-only commands are used (`fetch` is suggested to you, not run). No glab, no MR, no push, no commit.
- Confluence is read-only. Jira writes are limited to the one comment and its attachments, and only after your approval at 5b.
- It doesn't touch CI/CD.
- It reviews your existing checkout and creates no worktree. One review per ticket at a time.

## Reuse

`JiraGateway` (fetch, upsert comment), `GraphKit.checkpoint` (pauses, abort, UI approval panel), the pr_review lens
engine (unchanged, still used by jira-ticket-implement's `repo_review`), `ClaudeCodeAgent.verify` with the Playwright
MCP, the ledger, telemetry, and the run manager/UI.

New pieces: attachment upload in `JiraGateway`, the checkout verification in `Vcs`, a checklist editor and screenshot
gallery in the UI approval panel, and the CLI `devflow review <TICKET> --commit repo=sha[,sha]`.

## Jira MCP at first-time setup (Huynh, 2026-10-05)

`devflow setup` (and the Setup screen in the UI) makes a Jira MCP connected to Claude Code a **required** item.

- **Where it looks:** the MCP servers you added to Claude Code (`claude mcp add …`, read from `~/.claude.json`, `~/.claude/settings.json` and `.mcp.json`). You configure Jira once, in Claude Code. If the server named by `jira_server` isn't under `mcp_servers` in workspace.yaml, devflow uses the Claude Code entry. If no Jira MCP is found, setup fails with the `claude mcp add` command to run.
- **What it checks:**
  1. The server starts and answers.
  2. The read tools exist: get issue, download attachments.
  3. The write tools exist: add comment, edit comment, upload attachment. They're missing when the server runs in read-only mode.
  4. Reading a ticket works: setup asks for any ticket key you can see.
  
  It can't prove you're allowed to comment on a given project without posting a comment, so the real permission test happens at the first post. See the fallback below.
- **The result** is one of three:
  - ✓ read + write: the full review is available.
  - ⚠ read only: the review runs, but Jira posting switches to the fallback from the start, and setup says so.
  - ✗ missing or unreachable: setup fails, and the ticket review (and jira-ticket-implement) won't start.

**Fallback when Jira posting is not possible** (no write tools, or Jira refuses the comment or upload, e.g. 401/403 "no permission"):
the approved comment is saved to `.devflow/runs/<run>/jira-comment.md` with the screenshots beside it in `evidence/`, the
run ends as COMPLETED with a clear "not posted to Jira: <reason>" line, and the UI shows a Copy comment button plus the
folder to drag the screenshots from, so you can post it by hand. Nothing is retried in a loop. If only the upload is
refused, the comment is still posted with a list of the screenshot names and the files stay local.

## Needs a live check (can't be tested in the cloud)

- Attachment upload tool name/args in your Jira MCP (sooperset `jira_update_issue` takes `attachments`). If it's missing, the comment lists the screenshots and they stay local. Inline images render on Jira Cloud via `!file.png|thumbnail!`.
- A headed Playwright MCP browser that keeps its session between cases (persistent profile).

## Defaults picked (say if you want otherwise)

1. "PR review" in the CLI and UI becomes this ticket review. The old diff-only review is kept only as the internal engine.
2. You start the app yourself at the checkout gate. devflow does not run `run` commands for this review.
3. Code review findings never block testing. They go into the comment.

## As built (differences from the proposal)

- **Steps that need you.** The test browser closes after each case, because each case is one Claude Code session.
  You therefore give what the case needs (an OTP, a test account) in the note and it re-runs the case with it. A
  login you do yourself carries over, since the browser profile is kept in `<state_dir>/playwright-profile`.
- **Fetching the ticket** is plain code, not a Haiku call. Understanding it runs on Sonnet.
- **Screenshots.** They are attached to the ticket, and the comment's test table names them. They are not embedded
  inline yet: how inline images render through your Jira MCP is part of the live check.
- **Jira MCP source.** workspace.yaml wins. Otherwise devflow uses the server connected to Claude Code.
  `devflow doctor` and the UI's Connections page now show the read, comment and attachment checks too.
- **Code review models.** The ticket review runs the pr_review engine under its own `pr_review.*` steps, so
  `ai.steps` overrides in workspace.yaml apply to it.

