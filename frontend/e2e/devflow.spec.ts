import { expect, test, type Page } from "@playwright/test";

// Drives the offline demo server (tests/ui/demo_server.py) through the browser: real graphs, git repos and
// worktrees, fake Claude, Jira and GitLab. Each test uses its own ticket key, since a ticket has one active run.

async function open(page: Page) {
  await page.goto("/?token=e2e");
  await expect(page.getByRole("heading", { name: "Workflow overview" })).toBeVisible();
}

async function startTicket(page: Page, key: string) {
  await page.getByTestId("new-run").click();
  await page.getByRole("textbox", { name: "Jira ticket" }).fill(key.toLowerCase());
  await page.getByTestId("repos-picker").getByText("api").click();
  await page.getByTestId("repos-picker").getByText("web").click();
  await expect(page.getByTestId("preflight")).toContainText("checks OK");
  await page.getByTestId("start-run").click();
}

const panel = (page: Page, name: string) => page.locator(`[data-testid="approval-panel"][data-checkpoint="${name}"]`);

test("a Jira ticket run goes from start to draft MRs entirely in the browser", async ({ page }) => {
  await open(page);
  await startTicket(page, "AQS-101");
  await expect(panel(page, "approve_plan")).toBeVisible();
  await expect(page.getByTestId("run-title")).toHaveText("Export orders as CSV");
  await expect(page.getByTestId("plan-waves")).toContainText("api");
  await expect(page.getByTestId("step-approve_plan")).toHaveAttribute("data-state", "waiting");
  await expect(page.getByTestId("stat-needs-you")).toContainText("01");

  // Revise needs a note; approve continues.
  await expect(page.getByTestId("choice-revise")).toBeDisabled();
  await page.getByTestId("choice-approve").click();

  await expect(panel(page, "manual_test")).toBeVisible();
  await expect(page.getByTestId("manual-repo-web")).toContainText("AQS-101");
  await expect(page.getByTestId("step-implement_repo")).toContainText("×2");

  // Node detail: the coding agent's live log, including the command the policy denied.
  await page.getByTestId("step-implement_repo").click();
  await page.getByTestId("node-detail").getByRole("tab", { name: /Logs/ }).click();
  await expect(page.getByTestId("node-logs")).toContainText("Denied");
  await page.getByTestId("node-detail").getByRole("tab", { name: "Diff" }).click();
  await expect(page.getByTestId("diff-view")).toContainText("feature.txt");

  await page.getByTestId("choice-ok").click();
  await expect(panel(page, "approve_push")).toBeVisible();
  await page.getByLabel("Decision note").fill("ship it");
  await page.getByTestId("choice-approve").click();

  await expect(page.getByTestId("run-output")).toContainText("Code Review");
  await expect(page.getByTestId("run-detail").getByText("Completed", { exact: true })).toBeVisible();

  // The rest of the run's record.
  await page.getByTestId("more-tabs").click();
  await page.getByRole("button", { name: "Decisions" }).click();
  await expect(page.getByTestId("decisions-tab")).toContainText("ship it");
  await page.getByTestId("more-tabs").click();
  await page.getByRole("button", { name: "Side effects" }).click();
  await expect(page.getByTestId("effects-tab")).toContainText("Create mr");
  await page.getByTestId("more-tabs").click();
  await page.getByRole("button", { name: "Tokens" }).click();
  await expect(page.getByTestId("tokens-tab")).toContainText("Estimated cost");
  await expect(page.getByTestId("run-tokens")).toContainText("$");
});

test("aborting at a checkpoint asks first, and the run can be reopened", async ({ page }) => {
  await open(page);
  await startTicket(page, "AQS-102");
  await expect(panel(page, "approve_plan")).toBeVisible();
  await page.getByTestId("approval-panel").getByTestId("choice-abort").click();
  await page.getByRole("dialog").getByRole("textbox").fill("wrong ticket");
  await page.getByTestId("confirm-abort").click();
  await expect(page.getByText("Aborted at approve_plan: wrong ticket.", { exact: false })).toBeVisible();
  await page.getByTestId("reopen").click();
  await expect(panel(page, "approve_plan")).toBeVisible();
});

test("any graph is monitored with no UI code: generated form and a generic interrupt", async ({ page }) => {
  await open(page);
  await page.getByTestId("new-run").click();
  await page.getByTestId("workflow-select").selectOption("question_graph");
  await page.getByLabel("Topic").fill("the export");
  await page.getByTestId("start-run").click();
  await expect(page.getByTestId("approval-panel")).toContainText("Ship the export?");
  await page.getByTestId("generic-answer").fill("yes");
  await page.getByTestId("generic-submit").click();
  await expect(page.getByTestId("run-output")).toHaveText("Shipped the export: yes");
  await page.getByTestId("run-detail").getByRole("button", { name: "Graph" }).click();
  await expect(page.getByTestId("graph-view")).toContainText("Finish");
});

test("approvals filter, tokens, worktrees, settings and connections pages render", async ({ page }) => {
  await open(page);
  await startTicket(page, "AQS-103");
  await expect(panel(page, "approve_plan")).toBeVisible();
  await page.getByRole("link", { name: /Approvals/ }).click();
  await expect(page.getByRole("heading", { name: "Approvals" })).toBeVisible();
  await expect(page.getByTestId("runs-list")).toContainText("AQS-103");

  await page.getByRole("link", { name: "Tokens" }).click();
  await expect(page.getByTestId("usage-stats")).toContainText("Estimated cost");

  await page.getByRole("link", { name: "Worktrees" }).click();
  await expect(page.getByTestId("worktrees")).toContainText("AQS-103");

  await page.goto("/settings/workspace");
  await expect(page.getByTestId("repo-editor-api")).toBeVisible();
  await expect(page.getByLabel("MCP servers")).toHaveValue(/••••/);
  await expect(page.getByLabel("MCP servers")).not.toHaveValue(/s3cret/);

  await page.goto("/settings/connections");
  await page.getByTestId("recheck").click();
  await expect(page.getByTestId("check-mcp.jira")).toContainText("OK");
  await expect(page.getByTestId("doctor-status")).not.toContainText("not checked");
});
