"""Freely Implement: the ticket implement workflow for a task the developer describes, without Jira.

Same engine as `jira_ticket_implement` (graph.build_graph(free=True)): worktrees for the repos you pick, a plan you approve,
Claude Code implementing it wave by wave with the repo's checks, a code review, then you approve the code, test it locally
with an ordered list of cases, and approve the push. The branch is yours, the base is always `develop`, and nothing is written
to Jira."""
from .graph import CHECKPOINT_TITLES, DEVFLOW_UI as JIRA_UI, HIDDEN_NODES, build_graph

WORKFLOW = "free_implement"

FORM = [
    {"name": "description", "label": "What to implement", "type": "textarea", "required": True, "rows": 8,
     "placeholder": "Paste the description: what to build or fix, rules, edge cases, acceptance criteria…"},
    {"name": "images", "label": "Mockups / images", "type": "images",
     "help": "Optional. Drag, browse or paste (Ctrl+V) up to 6 images. They are read once into a written brief; the coding agent of a UI repo can open them."},
    {"name": "note", "label": "Notes", "type": "textarea", "placeholder": "Optional: what the images mean, what must match, what to avoid"},
    {"name": "branch", "label": "Branch name", "type": "text", "required": True, "mono": True, "placeholder": "feature/export-orders",
     "help": "Created from a fresh origin/develop in every repo you pick (develop is always the base)."},
    {"name": "ref", "label": "Ticket / reference number", "type": "text", "placeholder": "Optional, e.g. AQS-123",
     "help": "Only used as a prefix of the commit message and MR title."},
    {"name": "repos", "label": "Repos", "type": "repos", "required": True, "help": "Only these repos are touched. Pick at least one."},
    {"name": "manual_code", "label": "I'll write the code myself (Claude Code CLI)", "type": "bool",
     "help": "Plan, branches, checks, review and push stay automatic; the run stops after the plan and waits while you code in the worktrees."},
]
CHECKPOINTS = {**CHECKPOINT_TITLES, "approve_code": "Approve the code changes", "manual_test": "Manual test"}
FREE_HIDDEN = [*HIDDEN_NODES, "apply_code_approval", "load_task"]
DEVFLOW_UI = {
    "title": "Freely implement",
    "description": "Describe a task (and paste mockups) without a Jira ticket: pick the repos and a branch, approve the plan, "
                   "review the code, test it locally, then approve the push and the merge requests.",
    "icon": "wand-sparkles", "color": "#42cb80", "form": FORM, "checkpoints": CHECKPOINTS, "hidden_nodes": FREE_HIDDEN, "free": True,
    "run_prefix": "free",
    "steps": ["preflight", "prepare_worktrees", "read_mockups", "analyze_requirements", "change_impact", "discover_repos",
              "plan_implementation", "approve_plan", "prepare_branches", "implement_repo", "integration_check", "repo_review",
              "contract_review", "approve_code", "manual_test_cases", "manual_test", "approve_push", "commit_and_push",
              "open_draft_mrs", "summary"],
    "nodes": {**JIRA_UI["nodes"], "read_mockups": "Read mockups", "approve_code": "Approve code", "manual_test_cases": "Test cases",
              "manual_test": "Manual test", "open_draft_mrs": "Open MRs"},
    "node_details": {
        **JIRA_UI["node_details"], "preflight": "Tools, repos and worktrees checked (no Jira)",
        "prepare_worktrees": "One worktree per selected repo, on your branch", "read_mockups": "Images and notes read once into a written brief",
        "analyze_requirements": "Acceptance criteria and open questions from your description",
        "prepare_branches": "Your branch, cut from a freshly fetched develop", "open_draft_mrs": "Draft MRs to develop",
        "approve_code": "You read the diff and the review", "manual_test_cases": "The ordered test you will run locally",
        "manual_test": "You test it locally",
    },
}


def factory(deps, checkpointer=None):
    return build_graph(deps, checkpointer=checkpointer, free=True)
