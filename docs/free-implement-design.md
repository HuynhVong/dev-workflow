# Freely Implement and run mockups

## Why

Jira Implement could only take what the ticket says, and every run needed a Jira key. Two gaps: a developer wants to hand a
run a mockup and notes (so the UI comes out as expected), and wants to run the same plan, implement, review, test and push
flow for work that has no ticket.

## Mockups and notes (both workflows)

- Start form: `images` field (drag, browse, Ctrl+V paste; <= 6 images, <= 5 MB each) and `note`. The UI uploads each image
  to `POST /api/uploads` (base64 JSON, so no multipart dependency); the run copies them to `<state_dir>/<run_id>/inputs/`.
  CLI: `--image FILE` (repeatable), `--note`.
- `read_mockups` runs once after the ticket (or task) is loaded: one vision call (`mockup_brief`, Sonnet) turns the images,
  the ticket's own image attachments and the notes into a `DesignBrief` (screens, components, exact texts, styling,
  interactions, ambiguities). No images and no notes: no call.
- **Token rule:** later steps get the brief as text, never the pictures. Only a coding agent of a `has_ui` repo also gets the
  image paths (the run's `inputs/` and `attachments/` folders are added as read-only roots), so it can open the mockup.
- Developer notes are marked highest priority: where they disagree with the ticket text, they win and the conflict is raised.
- The approve-plan checkpoint shows the images and the brief; the manual-test checklist gets a "matches the mockup" line.

## Freely Implement (`free_implement`)

Same graph as `jira_ticket_implement` (`graph.build_graph(free=True)`), different ends:

| | Jira implement | Freely implement |
|---|---|---|
| Input | Jira key | description, images, notes, branch, optional reference, repos (required) |
| Front | fetch ticket, Confluence | `load_task` (the description stands in for the ticket) |
| Branch | the ticket key | the developer's name; worktrees keyed by its slug |
| Base | each repo's `base_branch` | always `develop` (workspace copy with `base_branch="develop"`) |
| Preflight | tools, Jira, Confluence, repos | tools, repos, worktrees (no Jira/Confluence) |
| After implement | integration, manual test, review, push approval | integration, review, **approve_code**, **manual_test_cases**, manual test, push approval |
| Tail | draft MRs, Jira update | draft MRs |

`gate()` decides the next step from versions of the code (`review_version`, `code_approved_version`, `manual_test_version`),
so any fix, from a review blocker, a requested change or manual-test feedback, sends the run back through review, code
approval and testing again; the push is never offered for code that was not reviewed, approved and tested.

New pieces: `task_inputs.py` (uploads, branch validation, slugs), `mockups.py` (brief), `free_implement.py` (UI config),
`approve_code` and `manual_test_cases` nodes, models `DesignBrief` and `ManualTest`, routing steps `mockup_brief` and
`manual_test_cases`, `branch_prefix` in workspace.yaml.

## Safety

Branch names must be valid git refs and cannot be `develop`, `main`, `master`, `release/*` or `hotfix/*` (optionally must
start with `branch_prefix`). One unfinished run per branch. An existing branch goes through the existing branch-ownership
checkpoint. Pushes are never forced, MRs are drafts, nothing is merged, nothing is written to Jira.
