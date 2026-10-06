"""Structured outputs for the LLM steps (validated by the Anthropic SDK's structured output parsing)."""
from typing import Literal

from pydantic import BaseModel, Field

Layer = Literal["frontend", "backend", "database", "infra", "shared"]
Confidence = Literal["high", "medium", "low"]
FeedbackCategory = Literal[
    "integration_failure", "e2e_failure", "manual_test_feedback", "review_blocker",
    "requirement_gap", "contract_issue", "test_issue", "scope_issue", "unclear",
]


class SourcedPoint(BaseModel):
    text: str
    source: str = Field(description="Confluence page title and section, or 'Jira description'.")


class PageRef(BaseModel):
    page_id: str
    title: str
    confirmed: bool = Field(description="True if linked from the ticket, False if found by search.")


class RequirementContext(BaseModel):
    requirement: list[SourcedPoint] = Field(description="Detailed requirement: business rules, validations, flows, edge cases, acceptance criteria.")
    current_business: list[SourcedPoint] = Field(description="How it works today: existing rules, processes, screens, APIs, data the change touches.")
    conflicts: list[str] = Field(description="Places where Confluence and the Jira text disagree.")
    pages: list[PageRef]


class Analysis(BaseModel):
    summary: str
    kind: Literal["feature", "bug", "chore", "spike"]
    acceptance_criteria: list[str] = Field(description="Testable criteria.")
    questions: list[str] = Field(description="Questions that block planning. Empty if none.")


class ScopeNeed(BaseModel):
    repo: str
    reason: str


class Risk(BaseModel):
    level: Literal["low", "medium", "high"]
    reasons: list[str]


class Impact(BaseModel):
    layers: list[Layer]
    candidate_repos: list[str] = Field(description="Only repo names from the allowed list.")
    out_of_scope_repos: list[ScopeNeed] = Field(description="Repos that seem needed but are NOT in the allowed list.")
    contract_changes: list[str] = Field(description="Cross-repo API/DTO/event/shared-type changes.")
    migrations: list[str] = Field(description="Schema or data migrations and their ordering needs.")
    risk: Risk
    integration_required: bool
    e2e_required: bool
    manual_test_focus: list[str]
    questions: list[str] = Field(description="Open questions raised by the impact analysis. Empty if none.")


class RepoTasks(BaseModel):
    repo: str
    tasks: list[str]
    likely_files: list[str]


class DagEdge(BaseModel):
    upstream: str
    downstream: str
    carries: str = Field(description="What flows along the edge: a contract, a type package, a migration.")


class TestPlanItem(BaseModel):
    criterion: str
    tests: list[str]


class Plan(BaseModel):
    repos: list[RepoTasks]
    edges: list[DagEdge] = Field(description="Dependency edges between repos. Independent repos have none.")
    contracts: list[str] = Field(description="Contracts fixed up front so repos can be coded against them.")
    migrations: list[str]
    test_plan: list[TestPlanItem]
    risks: list[str]


class FeedbackItem(BaseModel):
    source: str
    category: FeedbackCategory
    repos: list[str] = Field(description="Repos that must change to resolve it. Empty if unknown or not a code change.")
    cause: str
    confidence: Confidence
    fix_instructions: str = Field(description="Concrete instructions for the coding agent, if this is a code/contract/test fix.")
    contract_changed: bool = Field(description="True if fixing it changes a contract other repos consume.")


class FeedbackAnalysis(BaseModel):
    items: list[FeedbackItem]


class ContractIssue(BaseModel):
    repos: list[str]
    issue: str


class ContractCheck(BaseModel):
    ok: bool
    issues: list[ContractIssue]


class ContractReview(BaseModel):
    blockers: list[ContractIssue] = Field(description="Producer/consumer mismatches, unsafe merge order, migrations out of sync, uncovered acceptance criteria.")
    notes: list[str]


class OutdatedPage(BaseModel):
    page: str
    reason: str


class Outdated(BaseModel):
    pages: list[OutdatedPage]


# --- address-review --------------------------------------------------------------
ThreadCategory = Literal["must_fix", "suggestion", "question", "out_of_scope", "disagree"]


class ThreadPlan(BaseModel):
    thread_id: str
    category: ThreadCategory
    repo: str = Field(description="Repo that must change (usually the MR's repo). Empty if no code change.")
    summary: str = Field(description="What the reviewer asks, in one line.")
    proposed_action: Literal["fix", "answer", "skip"]
    fix_instructions: str = Field(description="Concrete instructions for the coding agent when the action is fix.")
    reply: str = Field(description="Draft reply to post on the thread: what was changed, or the answer, or why not.")
    behaviour_change: bool = Field(description="True if the fix changes runtime behaviour (not a rename, comment or style fix).")
    contract_change: bool = Field(description="True if the fix changes a contract another repo consumes.")


class ReviewTriage(BaseModel):
    items: list[ThreadPlan]


class ReviewFixPlan(BaseModel):
    edges: list[DagEdge] = Field(description="Dependency edges between the repos being fixed. Independent repos have none.")
    integration_required: bool


# --- ticket review ---------------------------------------------------------------
class TicketUnderstanding(BaseModel):
    summary: str = Field(description="What the ticket asks for, in two or three plain sentences.")
    requirement: list[str] = Field(description="The detailed requirement: business rules, validations, flows, edge cases.")
    acceptance_criteria: list[str] = Field(description="Testable acceptance criteria, from the ticket or derived from it.")
    out_of_scope: list[str] = Field(description="What the ticket explicitly does not cover. Empty if none.")
    unclear: list[str] = Field(description="Points the ticket leaves unclear that matter for reviewing or testing. Empty if none.")


class CriterionCoverage(BaseModel):
    criterion: str
    status: Literal["met", "partial", "missing", "unclear"]
    evidence: str = Field(description="Where in the diff it is implemented (file and what), or what is missing.")


class Coverage(BaseModel):
    criteria: list[CriterionCoverage]
    unrelated_changes: list[str] = Field(description="Changes in the commits that the ticket does not ask for. Empty if none.")


class TestCase(BaseModel):
    __test__ = False  # not a pytest class
    title: str
    criterion: str = Field(description="The acceptance criterion this case proves.")
    preconditions: list[str] = Field(description="Data or state needed before the steps (user role, existing records). Empty if none.")
    steps: list[str] = Field(description="Browser steps, one action each.")
    expected: str
    mode: Literal["auto", "needs_you"] = Field(description="needs_you when a person must act: OTP, captcha, real payment, "
                                                           "an email inbox, a device, or data only the developer can create.")
    needs_you_reason: str = Field(description="What the person must do, when mode is needs_you. Empty otherwise.")
    proof: list[str] = Field(description="What each screenshot must show to prove the criterion.")


class TestPlanDraft(BaseModel):
    __test__ = False
    cases: list[TestCase]
    not_testable: list[str] = Field(description="Criteria that cannot be checked in the browser, and why. Empty if none.")


class CommentDraft(BaseModel):
    conclusion: Literal["ready", "needs_changes", "blocked"] = Field(
        description="ready: criteria met and tests pass; needs_changes: blocker/major findings, missing criteria or failed tests; "
                    "blocked: the review could not be completed.")
    summary: str = Field(description="Three to five plain sentences for the Jira comment: what the ticket needs, whether "
                                     "the commits deliver it, and what the tests showed.")


class DesignBrief(BaseModel):
    """What the developer's mockups and notes ask for, in words, so later steps need not look at the images again."""
    summary: str = Field(description="One or two sentences: what the mockups show and what the notes ask for.")
    screens: list[str] = Field(description="Each screen or area: its layout (regions, order, alignment) and what is on it.")
    components: list[str] = Field(description="UI components and their states (buttons, inputs, tables, dialogs, empty/loading/error).")
    texts: list[str] = Field(description="Exact visible labels, headings, placeholders and messages, verbatim.")
    styling: list[str] = Field(description="Colors, typography, spacing, sizes, icons and any visual rule that can be read off.")
    interactions: list[str] = Field(description="Behaviour the mockups or notes imply: clicks, navigation, validation, responsive changes.")
    ambiguities: list[str] = Field(description="Things the images and notes do not settle and that change the result. Empty if none.")


class ManualCase(BaseModel):
    title: str
    steps: list[str] = Field(description="Numbered actions, in the order to do them.")
    expected: str = Field(description="What the developer must see or get.")
    covers: str = Field(description="The acceptance criterion or mockup part this proves.")


class ManualTest(BaseModel):
    """The ordered manual test a developer runs locally before approving the push."""
    cases: list[ManualCase]
