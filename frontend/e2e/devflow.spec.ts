import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { join } from "node:path";

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

  // The connections page is taller than the window: only the content scrolls, the sidebar stays full height.
  await page.setViewportSize({ width: 1440, height: 600 });
  const content = page.getByTestId("content-scroll");
  expect(await content.evaluate((el) => el.scrollHeight > el.clientHeight)).toBe(true);
  await content.evaluate((el) => el.scrollTo(0, el.scrollHeight));
  expect(await page.evaluate(() => window.scrollY)).toBe(0);
  const side = await page.locator("aside").boundingBox();
  expect(side).toMatchObject({ y: 0, height: 600 });
  await expect(page.locator("aside").getByLabel("Settings")).toBeInViewport(); // the button at the sidebar's foot
});

test("a ticket review: checkout, approved test plan, a step that needs you, proof, and the Jira comment", async ({ page }) => {
  const sha = readFileSync(join(process.env.DEVFLOW_E2E_DIR!, "review-commit.txt"), "utf8").trim();
  await open(page);
  await page.getByTestId("new-run").click();
  await page.getByTestId("workflow-select").selectOption("ticket_review");
  await page.getByRole("textbox", { name: "Jira ticket" }).fill("aqs-900");
  await page.getByLabel("Commits per repo").fill(`web=${sha}`);
  await expect(page.getByTestId("preflight")).toContainText("checks OK");
  await page.getByTestId("start-run").click();

  await expect(panel(page, "checkout_gate")).toBeVisible();
  await expect(page.getByTestId("checkout-web")).toContainText("rtk git checkout AQS-900");
  await expect(page.getByTestId("app-url")).toHaveValue("http://localhost:5173");
  await page.getByTestId("app-url").fill("http://localhost:3000");
  await page.getByTestId("choice-ready").click();

  await expect(panel(page, "approve_test_plan")).toBeVisible();
  await expect(page.getByTestId("test-plan")).toContainText("Filtered export");
  await expect(page.getByTestId("choice-regenerate")).toBeDisabled();
  await page.getByTestId("choice-approve").click();

  await expect(panel(page, "human_step")).toBeVisible();
  await expect(page.getByTestId("human-ask")).toContainText("Log in with your SSO account");
  await expect(panel(page, "human_step")).toContainText("Open http://localhost:3000");
  await page.getByLabel("Decision note").fill("Logged in as qa@acme.io");
  await page.getByTestId("choice-continue").click();

  await expect(panel(page, "review_results")).toBeVisible();
  await expect(page.getByTestId("test-results")).toContainText("TC2");
  await expect(page.getByTestId("proof-shots").first().locator("img").first()).toHaveJSProperty("complete", true);
  await page.getByTestId("choice-accept").click();

  await expect(panel(page, "approve_comment")).toBeVisible();
  await expect(page.getByTestId("jira-comment")).toHaveValue(/Ticket review: AQS-900/);
  await expect(page.getByTestId("choice-edit")).toBeDisabled();
  await page.getByTestId("jira-comment").fill((await page.getByTestId("jira-comment").inputValue()) + "\n\nAlso checked on Safari.");
  await expect(page.getByTestId("choice-approve")).toBeDisabled();
  await page.getByTestId("choice-edit").click();
  await expect(page.getByTestId("jira-comment")).toHaveValue(/Also checked on Safari\./);
  await page.getByTestId("choice-approve").click();

  await expect(page.getByTestId("run-output")).toContainText("Posted to Jira");
});

test("a standup: dates and template in, tickets from Jira's history confirmed, report edited and saved", async ({ page }) => {
  await open(page);
  await page.getByTestId("new-run").click();
  await page.getByTestId("workflow-select").selectOption("standup");
  await page.getByLabel("From date").fill("2026-10-01");
  await page.getByLabel("To date").fill("2026-10-02");
  await page.getByLabel("Report template").fill("## Work report {{from}} → {{to}}\n### Started\n### Reviewed\n### Notes");
  await page.getByTestId("start-run").click();

  await expect(panel(page, "confirm_tickets")).toBeVisible();
  await expect(page.getByTestId("tickets-started")).toContainText("AQS-1");
  await expect(page.getByTestId("tickets-started")).not.toContainText("AQS-2 ");
  await expect(page.getByTestId("tickets-reviewed")).toContainText("AQS-10");
  await expect(page.getByTestId("tickets-reviewed")).not.toContainText("AQS-11");
  await page.getByTestId("ticket-AQS-3").uncheck();
  await page.getByTestId("choice-continue").click();

  await expect(panel(page, "review_report")).toBeVisible();
  await expect(page.getByTestId("standup-report")).toHaveValue(/### Reviewed/);
  await expect(page.getByTestId("choice-edit")).toBeDisabled();
  await page.getByTestId("standup-report").fill((await page.getByTestId("standup-report").inputValue()) + "\nDemo on Friday.");
  await expect(page.getByTestId("choice-accept")).toBeDisabled();
  await page.getByTestId("choice-edit").click();
  await expect(page.getByTestId("standup-report")).toHaveValue(/Demo on Friday\./);
  await page.getByTestId("choice-accept").click();
  await expect(page.getByText("Completed").first()).toBeVisible();
});
