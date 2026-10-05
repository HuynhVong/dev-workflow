import { defineConfig } from "@playwright/test";
import { tmpdir } from "node:os";
import { join } from "node:path";

// End-to-end tests against the offline demo server (tests/ui/demo_server.py): real graphs, git repos and
// worktrees, fake Claude, Jira and GitLab. Build the UI first (`npm run build`).
const PORT = Number(process.env.DEVFLOW_E2E_PORT ?? 8798);
const PY = process.env.DEVFLOW_PYTHON ?? "../.venv/bin/python";
// The demo's data folder; the tests read the ticket review's commit from it. Set once, inherited by the workers.
process.env.DEVFLOW_E2E_DIR ??= join(tmpdir(), `devflow-e2e-${process.pid}`);

export default defineConfig({
  testDir: "e2e",
  timeout: 90_000,
  expect: { timeout: 20_000 },
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    viewport: { width: 1440, height: 1000 },
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM } : {},
    screenshot: "only-on-failure",
  },
  webServer: {
    command: `${PY} ../tests/ui/demo_server.py --port ${PORT} --token e2e --delay 0.15 --dir ${process.env.DEVFLOW_E2E_DIR}`,
    url: `http://127.0.0.1:${PORT}/?token=e2e`,
    reuseExistingServer: false,
    timeout: 60_000,
    stdout: "pipe",
  },
});
