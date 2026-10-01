// Record the API of the fictional library for the online demo.
//
// Walks every page of the interface against a running tests/browser_server.py
// (the same server the screenshots use) and writes each API response it saw:
// an index in <output>/recording.json, every body as its own file under
// <output>/files/ (identical bodies share one), so the demo loads only what
// a visitor opens.
// The demo build (scripts/build-demo.mjs) ships that folder beside the
// interface, and src/demo/sw.ts answers the interface from it.
//
//   .venv/bin/python tests/demo_snapshot.py /tmp/tankarr-demo
//   .venv/bin/python tests/browser_server.py --snapshot /tmp/tankarr-demo/tankarr.sqlite3 \
//       --artwork-root /tmp/tankarr-demo/artwork --library-root /tmp/tankarr-demo/library --port 18880
//   npm run demo:record --prefix frontend
import { chromium } from "@playwright/test";
import { createHash } from "node:crypto";
import { mkdir, rm, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

const baseURL = (process.env.TANKARR_DEMO_URL ?? "http://127.0.0.1:18880").replace(/\/$/, "");
const output = resolve(process.env.TANKARR_DEMO_OUTPUT ?? "demo-data");
const origin = new URL(baseURL).origin;

const EXTENSIONS = {
  "application/json": "json",
  "text/plain": "txt",
  "image/png": "png",
  "image/jpeg": "jpg",
  "image/webp": "webp",
  "image/gif": "gif",
  "image/svg+xml": "svg",
  "image/avif": "avif",
};

await rm(output, { recursive: true, force: true });
await mkdir(resolve(output, "files"), { recursive: true });

/** @type {Record<string, {status: number, type: string, body?: string, file?: string}>} */
const responses = {};
let files = 0;

async function record(response) {
  const request = response.request();
  const url = new URL(request.url());
  if (url.origin !== origin || !url.pathname.startsWith("/api/")) return;
  const key = `${request.method()} ${url.pathname}${url.search}`;
  const status = response.status();
  if (status === 304 || (status >= 300 && status < 400)) return;
  const previous = responses[key];
  // A later response refreshes an earlier one, but an error never replaces
  // a good answer (a poll that raced the server's shutdown, for instance).
  if (previous && previous.status < 400 && status >= 400) return;
  const type = (response.headers()["content-type"] ?? "application/octet-stream").split(";")[0].trim();
  let body;
  try {
    body = await response.body();
  } catch {
    return; // the page navigated away before the body arrived
  }
  const extension = EXTENSIONS[type] ?? "bin";
  const name = `files/${createHash("sha256").update(body).digest("hex").slice(0, 20)}.${extension}`;
  if (!written.has(name)) {
    written.add(name);
    await writeFile(resolve(output, name), body);
    files += 1;
  }
  responses[key] = { status, type, file: name };
}
const written = new Set();

const browser = await chromium.launch({
  executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH,
  headless: true,
});
const pending = new Set();
try {
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 1,
    colorScheme: "dark",
    reducedMotion: "reduce",
  });
  const page = await context.newPage();
  page.setDefaultTimeout(60_000);
  page.on("response", (response) => {
    const task = record(response).catch((error) => console.warn(`skipped ${response.url()}: ${error}`));
    pending.add(task);
    void task.finally(() => pending.delete(task));
  });

  async function settle() {
    await page.waitForLoadState("networkidle");
    await page.waitForFunction(() =>
      Array.from(document.images).every((image) => image.complete),
    );
    await page.waitForTimeout(300);
  }

  async function visit(hash, ready) {
    await page.goto(`${baseURL}/${hash}`, { waitUntil: "domcontentloaded" });
    if (ready) {
      await page.locator(ready).first().waitFor().catch(() => console.warn(`${hash}: ${ready} did not appear`));
    }
    await settle();
    console.log(`visited ${hash}`);
  }

  /** Click every tab or segmented control on the page so each panel's reads are recorded. */
  async function openEveryTab() {
    const tabs = page.locator('[role="tab"], .tabs button, .segmented button');
    const count = await tabs.count();
    for (let index = 0; index < Math.min(count, 24); index += 1) {
      const tab = tabs.nth(index);
      if (!(await tab.isVisible().catch(() => false))) continue;
      await tab.click({ timeout: 5_000 }).catch(() => undefined);
      await settle();
    }
  }

  await visit("#/", ".poster-card");
  const library = await page.evaluate(() => fetch("/api/manga?cached=true").then((r) => r.json()));
  const seriesIds = library.map((item) => item.id);
  console.log(`${seriesIds.length} series`);

  const authors = new Set();
  for (const id of seriesIds) {
    await visit(`#/series/${encodeURIComponent(id)}`, "h1");
    await openEveryTab();
    for (const href of await page.locator('a[href^="#/authors/"]').evaluateAll((links) => links.map((a) => a.getAttribute("href")))) {
      if (href) authors.add(href);
    }
  }
  for (const href of authors) await visit(href, "h1");

  await visit("#/calendar", "main");
  for (const label of [/next/i, /previous/i, /today/i]) {
    const button = page.getByRole("button", { name: label }).first();
    if (await button.isVisible().catch(() => false)) {
      await button.click().catch(() => undefined);
      await settle();
    }
  }
  await visit("#/wanted", "main");
  await visit("#/activity", "main");
  await visit("#/history", "main");
  await visit("#/bookmarks", "main");
  await visit("#/import", "main");
  await visit("#/system", "main");
  await openEveryTab();
  await visit("#/settings", "main");
  await openEveryTab();
  await visit("#/add?q=lantern", "main");

  // Books the reader can open: the newest downloaded chapters of each series
  // are probed outside the page, so the probes are not recorded; the manifests
  // and pages are recorded when the reader opens them below.
  const readable = [];
  for (const id of seriesIds) {
    const detail = await (await page.request.get(`${baseURL}/api/manga/${encodeURIComponent(id)}`)).json();
    const downloaded = (detail.chapters ?? []).filter((chapter) => chapter.downloaded && String(chapter.library_path ?? "").endsWith(".cbz"));
    for (const chapter of downloaded.slice(-12)) {
      const probe = await page.request.get(`${baseURL}/api/reader/books/${encodeURIComponent(chapter.id)}`);
      if (probe.status() === 200) readable.push(chapter.id);
    }
  }
  console.log(`${readable.length} readable books`);
  for (const id of readable) {
    await visit(`#/reader/${encodeURIComponent(id)}`, 'img[alt^="Page"]');
    for (let step = 0; step < 8; step += 1) {
      await page.keyboard.press("ArrowLeft");
      await page.waitForTimeout(150);
    }
    await settle();
  }

  await Promise.all([...pending]);
  const recording = {
    version: 2,
    recorded_at: new Date().toISOString(),
    responses,
  };
  await writeFile(resolve(output, "recording.json"), JSON.stringify(recording));
  console.log(`${Object.keys(responses).length} responses, ${files} files -> ${output}`);
} finally {
  await browser.close();
}
