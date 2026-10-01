import { defineConfig, devices } from "@playwright/test";

// The built online demo (npm run demo:build), served under its base path.
export default defineConfig({
  testDir: "./e2e",
  testMatch: /demo\.spec\.ts/,
  fullyParallel: false,
  workers: 1,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: [["list"]],
  use: {
    ...devices["Desktop Chrome"],
    launchOptions: { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH },
    baseURL: "http://127.0.0.1:18881/tankarr/demo/",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  webServer: {
    command: "node scripts/serve-demo.mjs 18881",
    url: "http://127.0.0.1:18881/tankarr/demo/demo-data/recording.json",
    timeout: 60_000,
    reuseExistingServer: false,
  },
});
