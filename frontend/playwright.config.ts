import { defineConfig, devices } from "@playwright/test";

const python = process.env.TANKARR_TEST_PYTHON ?? "python3";

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.spec.ts",
  fullyParallel: false,
  workers: 1,
  timeout: 45_000,
  expect: { timeout: 15_000 },
  reporter: [["list"], ["html", { open: "never" }]],
  use: {
    ...devices["Desktop Chrome"],
    launchOptions: {
      executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
    },
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "root",
      // The online demo has its own config (playwright.demo.config.ts).
      testIgnore: [/subpath\.spec\.ts/, /demo\.spec\.ts/],
      use: { baseURL: "http://127.0.0.1:18878" },
    },
    {
      // The same build served under a reverse proxy sub-path (TANKARR_URL_BASE).
      name: "subpath",
      testMatch: /subpath\.spec\.ts/,
      use: { baseURL: "http://127.0.0.1:18879/tankarr/" },
    },
  ],
  webServer: [
    {
      command: `${python} ../tests/browser_server.py --port 18878`,
      url: "http://127.0.0.1:18878/api/auth/status",
      timeout: 120_000,
      reuseExistingServer: false,
    },
    {
      command: `${python} ../tests/browser_server.py --port 18879 --url-base /tankarr`,
      url: "http://127.0.0.1:18879/tankarr/api/auth/status",
      timeout: 120_000,
      reuseExistingServer: false,
    },
  ],
});
