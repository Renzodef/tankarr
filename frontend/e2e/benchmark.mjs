// Run against tests/browser_server.py, never a production mutation endpoint.
// The first navigation is cold on a newly started API; later contexts have
// cold browser caches but can reuse server snapshots. These are lab timings,
// not field INP or measurements of competing products.
import { chromium } from "@playwright/test";
import { mkdir, writeFile } from "node:fs/promises";
import { performance } from "node:perf_hooks";

const baseURL = process.env.TANKARR_BENCH_URL ?? "http://127.0.0.1:18878";
const output = process.env.TANKARR_BENCH_OUTPUT ?? "test-results/benchmark";
const rounds = Number(process.env.TANKARR_BENCH_ROUNDS ?? 3);
await mkdir(output, { recursive: true });
const browser = await chromium.launch({
  executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
  headless: true,
});
const results = [];
try {
  for (let round = 0; round < rounds; round += 1) {
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const page = await context.newPage();
    page.setDefaultTimeout(90_000);
    const errors = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.addInitScript(() => {
      window.__tankarrMetrics = { lcp: 0, longTasks: [], interactions: [] };
      new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) window.__tankarrMetrics.lcp = entry.startTime;
      }).observe({ type: "largest-contentful-paint", buffered: true });
      new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) window.__tankarrMetrics.longTasks.push(entry.duration);
      }).observe({ type: "longtask", buffered: true });
      new PerformanceObserver((list) => {
        for (const entry of list.getEntries()) {
          if (entry.interactionId) window.__tankarrMetrics.interactions.push(entry.duration);
        }
      }).observe({ type: "event", buffered: true, durationThreshold: 16 });
    });
    const cdp = await context.newCDPSession(page);
    if (process.env.TANKARR_BENCH_THROTTLE === "1") {
      await cdp.send("Network.enable");
      await cdp.send("Network.emulateNetworkConditions", {
        offline: false, latency: 150, downloadThroughput: 200_000,
        uploadThroughput: 100_000, connectionType: "cellular3g",
      });
      await cdp.send("Emulation.setCPUThrottlingRate", { rate: 4 });
    }
    const started = performance.now();
    await page.goto(baseURL, { waitUntil: "domcontentloaded", timeout: 90_000 });
    await page.locator(".poster-card").first().waitFor();
    const libraryReadyMs = performance.now() - started;
    // Data readiness and visual readiness are different: a card can be in
    // the DOM while its cover is still transferring or being decoded.
    await page.waitForFunction(() => Array.from(document.querySelectorAll(".poster-card img"))
      .filter((img) => {
        const box = img.getBoundingClientRect();
        return box.bottom > 0 && box.top < innerHeight;
      }).every((img) => img.complete));
    const coverMetrics = await page.evaluate(async () => {
      const visible = Array.from(document.querySelectorAll(".poster-card img"))
        .filter((img) => {
          const box = img.getBoundingClientRect();
          return box.bottom > 0 && box.top < innerHeight;
        });
      await Promise.all(visible.map((img) => img.decode().catch(() => undefined)));
      await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      return {
        visibleCoversDecoded: visible.filter((img) => img.naturalWidth > 0).length,
        failedVisibleCovers: visible.filter((img) => img.naturalWidth === 0).length,
      };
    });
    const libraryVisualReadyMs = performance.now() - started;
    const libraryMetrics = await page.evaluate(() => ({
      ...window.__tankarrMetrics,
      scripts: performance.getEntriesByType("resource")
        .filter((entry) => entry.initiatorType === "script")
        .map((entry) => ({ name: entry.name.split("/").at(-1), bytes: entry.transferSize })),
    }));
    const wantedStarted = performance.now();
    await page.locator('a.nav-item[href="#/wanted"]').click();
    await page.locator(".wanted-table tbody tr").first().waitFor();
    const wantedReadyMs = performance.now() - wantedStarted;
    const title = (await page.locator(".wanted-table tbody tr td").first().innerText()).split("\n")[0].trim();
    const filter = page.getByRole("combobox", { name: "Filter wanted items by series or pattern" });
    const filterStarted = performance.now();
    await filter.fill(title);
    await page.waitForFunction((text) => {
      const cells = Array.from(document.querySelectorAll(".wanted-table tbody tr td:first-child"));
      return cells.length > 0 && cells.every((cell) => cell.textContent.includes(text));
    }, title);
    const filterReadyMs = performance.now() - filterStarted;
    await filter.press("Escape");
    const returned = performance.now();
    await page.locator('a.nav-item[href="#/"]').click();
    await page.locator(".poster-card").first().waitFor();
    const returnToLibraryMs = performance.now() - returned;
    const metrics = await page.evaluate(() => window.__tankarrMetrics);
    const result = {
      round, libraryReadyMs, libraryVisualReadyMs, ...coverMetrics,
      wantedReadyMs, filterReadyMs, returnToLibraryMs,
      lcpMs: libraryMetrics.lcp,
      largestLongTaskMs: Math.max(0, ...metrics.longTasks),
      largestObservedInteractionMs: Math.max(0, ...metrics.interactions),
      initialScripts: libraryMetrics.scripts, errors,
    };
    results.push(result);
    console.log(JSON.stringify(result));
    if (round === 0) {
      await page.screenshot({ path: `${output}/library.png`, fullPage: true });
      await page.locator('a.nav-item[href="#/wanted"]').click();
      await page.locator(".wanted-table tbody tr").first().waitFor();
      await page.screenshot({ path: `${output}/wanted.png`, fullPage: true });
    }
    await context.close();
  }
} finally {
  await browser.close();
  await writeFile(`${output}/metrics.json`, JSON.stringify({ baseURL, results }, null, 2));
}
