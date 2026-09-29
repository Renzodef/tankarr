// Capture the documentation screenshots from a running tests/browser_server.py
// that serves the fictional library of tests/demo_snapshot.py:
//
//   .venv/bin/python tests/demo_snapshot.py /tmp/tankarr-demo
//   .venv/bin/python tests/browser_server.py --snapshot /tmp/tankarr-demo/tankarr.sqlite3 \
//       --artwork-root /tmp/tankarr-demo/artwork --port 18880
//   npm run screenshots --prefix frontend
//   .venv/bin/python tests/demo_snapshot.py --shrink docs/assets/screenshots   # PNG -> WebP
import { chromium } from "@playwright/test";
import { mkdir } from "node:fs/promises";
import { resolve } from "node:path";

const baseURL = process.env.TANKARR_SCREENSHOT_URL ?? "http://127.0.0.1:18880";
const output = resolve(process.env.TANKARR_SCREENSHOT_OUTPUT ?? "../docs/assets/screenshots");
await mkdir(output, { recursive: true });

// Activity is left out: the fixture runs no download worker, so the page
// would only show the banner saying so.
const pages = [
  { name: "library", hash: "#/", ready: ".poster-card" },
  { name: "series", hash: "#/series/cartographers-daughter", ready: "h1" },
  { name: "calendar", hash: "#/calendar", ready: ".calendar-day, .calendar-cell, main" },
  { name: "wanted", hash: "#/wanted", ready: ".wanted-table tbody tr" },
  { name: "system", hash: "#/system", ready: "h2:has-text('Scheduled tasks')" },
  { name: "settings", hash: "#/settings", ready: "h1.page-title:has-text('Settings')" },
];

const browser = await chromium.launch({
  executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
  headless: true,
});
try {
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 1,
    colorScheme: "dark",
    reducedMotion: "reduce",
  });
  const page = await context.newPage();
  page.setDefaultTimeout(60_000);
  for (const { name, hash, ready } of pages) {
    await page.goto(`${baseURL}/${hash}`, { waitUntil: "domcontentloaded" });
    await page.locator(ready).first().waitFor();
    await page.waitForLoadState("networkidle");
    // Every cover decoded, fonts ready, one more frame for layout.
    await page.waitForFunction(() =>
      Array.from(document.images).every((image) => image.complete && image.naturalWidth > 0),
    );
    await page.evaluate(() => document.fonts.ready);
    await page.waitForTimeout(400);
    const path = resolve(output, `${name}.png`);
    await page.screenshot({ path, type: "png" });
    console.log(`${name}: ${path}`);
  }
} finally {
  await browser.close();
}
