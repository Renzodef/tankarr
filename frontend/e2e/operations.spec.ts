import { expect, test } from "@playwright/test";

test("Activity preserves a healthy download queue when the torrent endpoint fails", async ({ page }) => {
  await page.addInitScript(() => {
    document.addEventListener("click", event => {
      const button = event.target instanceof Element ? event.target.closest("button") : null;
      if (button?.textContent?.trim() === "Retry activity") document.documentElement.dataset.activityRetry = "true";
    }, true);
  });
  await page.route("**/api/torrents?*", async route => {
    // Become available at the retry click, not before it: an automatic poll
    // must not win the test's race and remove the button before interaction.
    const retryClicked = await page.evaluate(() => document.documentElement.dataset.activityRetry === "true");
    return retryClicked ? route.continue() : route.fulfill({ status: 503, json: { detail: "Torrent feed unavailable" } });
  });
  await page.route("**/api/jobs/summary", route => route.fulfill({ json: { active: 1, counts: { queued: 1 }, series: [{ manga_id: "browser-042", manga_title: "Browser Series 042", manga_cover_url: null, queued: 1, working: 0, current: null }] } }));
  await page.goto("/#/activity");
  await expect(page.locator(".queue-series-title")).toContainText("Browser Series 042");
  await expect(page.getByRole("alert")).toContainText("Torrent feed unavailable");
  await expect(page.getByText("The queue is empty", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Retry activity", exact: true }).click();
  await expect(page.getByRole("button", { name: "Retry activity", exact: true })).toHaveCount(0);
});

test("Activity distinguishes an unavailable initial queue from an empty queue", async ({ page }) => {
  await page.route("**/api/jobs/summary", route => route.fulfill({ status: 503, json: { detail: "Queue feed unavailable" } }));
  await page.goto("/#/activity");
  await expect(page.getByRole("alert")).toContainText("Queue feed unavailable");
  await expect(page.getByText("The queue is empty", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("status").filter({ hasText: "Some queue data" })).toBeVisible();
});

test("Settings save preserves newer typing, labels fields, and guards unsaved navigation", async ({ page }) => {
  const settings = await (await page.request.get("/api/settings")).json();
  let releaseSave!: () => void;
  const gate = new Promise<void>(resolve => { releaseSave = resolve; });
  let saveRequested = false;
  await page.route("**/api/settings", async route => {
    if (route.request().method() !== "PUT") return route.fulfill({ json: settings });
    const changes = route.request().postDataJSON();
    saveRequested = true;
    await gate;
    for (const [key, value] of Object.entries(changes)) settings[key] = { ...settings[key], value };
    return route.fulfill({ json: { applied: Object.keys(changes), settings } });
  });
  try {
    await page.goto("/#/settings?tab=notifications");
    const topic = page.getByLabel("Topic", { exact: true });
    await topic.fill("saved-topic");
    await page.getByRole("button", { name: "Save Changes", exact: true }).click();
    await expect.poll(() => saveRequested).toBe(true);
    await topic.fill("newer-unsaved-topic");
    releaseSave();
    await expect(page.getByRole("button", { name: "Save Changes", exact: true })).toBeEnabled();
    await expect(topic).toHaveValue("newer-unsaved-topic");
    await expect(page.locator(".settings-save-bar")).toContainText("1 unsaved change");
    let dismissed = false;
    page.once("dialog", async dialog => { await dialog.dismiss(); dismissed = true; });
    await page.locator('a.nav-item[href="#/"]').click();
    await expect.poll(() => dismissed).toBe(true);
    await expect(topic).toHaveValue("newer-unsaved-topic");
    await expect(page).toHaveURL(/#\/settings\?tab=notifications$/);
    await page.getByRole("button", { name: "Discard", exact: true }).click();
    await page.locator('a.nav-item[href="#/"]').click();
    await expect(page.locator(".poster-card").first()).toBeVisible();
  } finally { releaseSave(); }
});

test("shared dialog has a name, contains keyboard focus and restores its trigger", async ({ page }) => {
  await page.goto("/#/series/browser-046");
  const edit = page.getByRole("button", { name: "Edit", exact: true });
  await edit.click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toHaveAccessibleName(/.+/);
  await expect.poll(() => dialog.evaluate(element => element.contains(document.activeElement))).toBe(true);
  await page.keyboard.press("Shift+Tab");
  await expect.poll(() => dialog.evaluate(element => element.contains(document.activeElement))).toBe(true);
  await page.keyboard.press("Tab");
  await expect.poll(() => dialog.evaluate(element => element.contains(document.activeElement))).toBe(true);
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(edit).toBeFocused();
});

test("Wanted evidence is accessible on mobile and shows actual cooldown and candidate reasons", async ({ page }) => {
  const payload = await (await page.request.get("/api/wanted?compact=true")).json();
  const recovery = { verdict: "exhausted", summary: "No verified release in the checked channels", checked_at: "2026-09-01T10:00:00Z", next_eligible_at: "2026-09-08T10:00:00Z", actionable_reason: "Wait for the cooldown or inspect another source.", channels: [{ channel: "indexer_book", outcome: "not_offered", detail: "No edition matched the canonical work", at: "2026-09-01T10:00:00Z", next_eligible_at: "2026-09-08T10:00:00Z" }] };
  payload[0].chapters.forEach((chapter: { recovery?: unknown }) => { chapter.recovery = recovery; });
  await page.route("**/api/wanted?*", route => route.fulfill({ json: payload }));
  await page.route("**/api/acquisition/preview", route => route.fulfill({ json: { explanation: "Saved acquisition policy", candidates: [{ id: "candidate-one", title: "Wrong edition", selected: false, reasons: [], rejections: ["Canonical edition does not match"] }] } }));
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/#/wanted");
  await page.getByRole("button", { name: /^Why still wanted:/ }).first().click();
  const dialog = page.getByRole("dialog", { name: "Why still wanted?", exact: true });
  await expect(dialog).toContainText("cooldown deadline, not a guaranteed");
  await expect(dialog).toContainText("No edition matched the canonical work");
  await expect(dialog).toContainText("Canonical edition does not match");
  await expect(dialog.getByRole("button", { name: "Inspect search results" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
});

test("legacy Tools links lead to Data or their series and the Tools tab is absent", async ({ page }) => {
  await page.goto("/#/settings?tab=tools");
  await expect(page).toHaveURL(/#\/settings\?tab=data$/);
  await expect(page.getByRole("button", { name: "Backup now", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Setup & tools", exact: true })).toHaveCount(0);
  await expect(page.getByText("Library lists", { exact: true })).toHaveCount(0);
  await page.goto("/#/settings?tab=tools&manga_id=browser-047");
  await expect(page).toHaveURL(/#\/series\/browser-047$/);
  await expect(page.getByRole("button", { name: "Edit", exact: true })).toBeVisible();
});

test("nightly repairs stay silent and opening System never applies or scans", async ({ page }) => {
  const requests: string[] = [];
  const job = { status: "ok", last_attempt_at: null, last_success_at: null, retry_after: null, error: null };
  const repair = { revision: "old-review", generated_at: "2026-01-01T00:00:00Z", total_actions: 33, truncated: false, can_apply: true, warnings: ["Repair waiting for retry"], actions: [{ id: "repair-one", kind: "move", path: "Series/file.cbz", reason: "Canonical organization drift", safe: true }] };
  await page.route("**/api/system/maintenance", route => route.fulfill({ json: { jobs: { backup: job, repair: { ...job, status: "error", error: "Repair waiting for retry" }, recycle: job }, repair } }));
  page.on("request", request => { if (/\/repair\/(preview|apply)$/.test(request.url())) requests.push(request.url()); });
  const loaded = page.waitForResponse(response => response.url().endsWith("/api/system/maintenance"));
  await page.goto("/#/system");
  await loaded;
  await expect(page.getByRole("heading", { name: "Health", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Library repair", exact: true })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Apply", exact: true })).toHaveCount(0);
  await expect(page.getByText("Repair waiting for retry")).toHaveCount(0);
  await expect(page.getByText("Series/file.cbz")).toHaveCount(0);
  expect(requests).toEqual([]);
});

test("setup uses explicit connection checks and links to configuration steps", async ({ page }) => {
  let probes = 0;
  await page.route("**/api/system/preflight", route => {
    if (route.request().method() === "POST") probes += 1;
    return route.fulfill({ json: { ready: true, checks: [{ id: "library", label: "Library availability", status: "ok", detail: "Disposable library ready", settings_tab: "general" }] } });
  });
  await page.goto("/#/setup");
  await expect(page.getByText("Disposable library ready", { exact: true })).toBeVisible();
  expect(probes).toBe(0);
  await page.getByRole("button", { name: "Run connection checks", exact: true }).click();
  await expect.poll(() => probes).toBe(1);
  await expect(page.getByRole("link", { name: "Open library settings", exact: true })).toHaveAttribute("href", "#/settings?tab=general");
  await page.getByRole("button", { name: "2. Reader", exact: true }).click();
  await expect(page.getByRole("link", { name: "Open reader settings", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Skip for now", exact: true }).click();
  await expect(page).toHaveURL(/#\/$/);
});

test("System About downloads diagnostics and Health can reopen setup", async ({ page }) => {
  await page.route("**/api/system/diagnostics/export", route => route.fulfill({ json: { version: 1, redacted: true } }));
  await page.goto("/#/system");
  const diagnostics = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download diagnostics", exact: true }).click();
  expect((await diagnostics).suggestedFilename()).toBe("tankarr-diagnostics.json");
  await page.getByRole("link", { name: "Re-run setup checks", exact: true }).click();
  await expect(page).toHaveURL(/#\/setup$/);
});

test("a delayed initial preflight cannot overwrite explicitly requested connection checks", async ({ page }) => {
  let releaseInitial!: () => void;
  const initialGate = new Promise<void>(resolve => { releaseInitial = resolve; });
  let initialStarted = false;
  await page.route("**/api/system/preflight", async route => {
    const initial = route.request().method() === "GET";
    if (initial) { initialStarted = true; await initialGate; }
    await route.fulfill({ json: { ready: true, checks: [{ id: "library", label: "Library availability", status: "ok", detail: initial ? "Older local result" : "Latest connection result" }] } });
  });
  try {
    await page.goto("/#/setup");
    await expect.poll(() => initialStarted).toBe(true);
    await page.getByRole("button", { name: "Run connection checks", exact: true }).click();
    await expect(page.getByText("Latest connection result", { exact: true })).toBeVisible();
    const initialResponse = page.waitForResponse(response => response.url().endsWith("/api/system/preflight") && response.request().method() === "GET");
    releaseInitial();
    await (await initialResponse).finished();
    await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
    await expect(page.getByText("Latest connection result", { exact: true })).toBeVisible();
    await expect(page.getByText("Older local result", { exact: true })).toHaveCount(0);
  } finally { releaseInitial(); }
});

test("Restore verifies a backup and guides offline recovery before explicit download", async ({ page }) => {
  const name = "tankarr-fixture-recovery.zip";
  let downloads = 0;
  await page.route("**/api/system/status", route => route.fulfill({ json: { backups: [{ name, size: 512, created_at: "2026-09-07T12:00:00Z" }] } }));
  await page.route(`**/api/system/backups/${name}/verify`, route => route.fulfill({ json: { verified: true, format: "tankarr-application-v1", contains_secrets: true, files: ["tankarr.sqlite3", "effective-settings.json"], excluded: ["media", "Suwayomi"] } }));
  await page.route(`**/api/system/backups/${name}/download`, route => { downloads += 1; return route.fulfill({ contentType: "application/zip", body: "fixture-only" }); });
  await page.goto("/#/settings?tab=data");
  await page.getByRole("button", { name: `Restore ${name}`, exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Restore backup", exact: true });
  await expect(dialog.getByRole("status")).toContainText("integrity verified");
  await expect(dialog).toContainText("never overwrites the running database");
  await expect(dialog).toContainText("credentials and private application data");
  expect(downloads).toBe(0);
  const download = page.waitForEvent("download");
  await dialog.getByRole("button", { name: "Download sensitive backup", exact: true }).click();
  expect((await download).suggestedFilename()).toBe(name);
  expect(downloads).toBe(1);
});

test("retained list and acquisition APIs remain available without Tools UI", async ({ page }) => {
  const exported = await page.request.get("/api/library/list/export");
  expect(exported.ok()).toBe(true);
  const list = await exported.json();
  expect(list.version).toBe(1);
  expect(list.items).toHaveLength(60);
  const preview = await page.request.post("/api/library/list/preview", { data: { items: list.items } });
  expect(preview.ok()).toBe(true);
  expect((await preview.json()).items.every((item: { status: string }) => item.status === "existing")).toBe(true);
  const acquisition = await page.request.post("/api/acquisition/preview", { data: { manga_id: "browser-048", preferences: {} } });
  expect(acquisition.ok()).toBe(true);
  expect((await acquisition.json()).total_candidates).toBe(100);
});

test("advanced import limits display MiB while saving exact byte values", async ({ page }) => {
  const settings = await (await page.request.get("/api/settings")).json();
  let saved: Record<string, unknown> | null = null;
  await page.route("**/api/settings", route => {
    if (route.request().method() === "PUT") {
      saved = route.request().postDataJSON();
      for (const [key, value] of Object.entries(saved!)) settings[key] = { ...settings[key], value };
      return route.fulfill({ json: { applied: Object.keys(saved!), settings } });
    }
    return route.fulfill({ json: settings });
  });
  await page.goto("/#/settings?tab=general");
  await page.getByLabel("Show advanced", { exact: true }).check();
  const expanded = page.getByLabel("Expanded archive size limit (MiB)", { exact: true });
  const reserve = page.getByLabel("Import free-space reserve (MiB)", { exact: true });
  await expect(expanded).toHaveValue(String(Number(settings.import_max_expanded_bytes.value) / (1024 * 1024)));
  await expanded.fill("8192");
  await reserve.fill("768");
  await page.getByRole("button", { name: "Save Changes", exact: true }).click();
  await expect.poll(() => saved).not.toBeNull();
  expect(saved!.import_max_expanded_bytes).toBe(String(8192 * 1024 * 1024));
  expect(saved!.import_disk_reserve_bytes).toBe(String(768 * 1024 * 1024));
});
