import { expect, test, type Page } from "@playwright/test";

const libraryCards = (page: Page) => page.locator(".poster-card");
const wantedRows = (page: Page) => page.locator(".wanted-table tbody tr");
const wantedFilter = (page: Page) => page.getByRole("combobox", {
  name: "Filter wanted items by series or pattern",
});

test("real API: Library, series, Wanted sorting, filtering and pagination", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.goto("/");
  await expect(libraryCards(page)).toHaveCount(30);
  await page.getByPlaceholder("Filter library…").fill("Browser Series 012");
  await expect(libraryCards(page)).toHaveCount(1);
  await libraryCards(page).first().click();
  await expect(page.getByRole("heading", { name: "Browser Series 012", exact: true })).toBeVisible();
  await page.locator('a.nav-item[href="#/wanted"]').click();
  await expect(wantedRows(page)).toHaveCount(20);
  await wantedFilter(page).fill("Browser Series 012");
  await wantedFilter(page).press("Escape");
  await expect(wantedRows(page).first()).toContainText("Browser Series 012");
  await expect(page.locator(".list-result-count")).toContainText("100 of");
  await page.getByRole("button", { name: "Next page", exact: true }).click();
  await expect(wantedRows(page).first()).toContainText("Chapter 21");
  await page.getByRole("combobox", { name: "Sort wanted items" }).selectOption("item");
  await page.getByRole("button", { name: "Sort descending", exact: true }).click();
  await expect(wantedRows(page).first()).toContainText("Chapter 100");
  expect(errors).toEqual([]);
});

test("Wanted renders independently of an unavailable monitor, and retries it", async ({ page }) => {
  let failing = true;
  await page.route("**/api/monitor/status", route => failing
    ? route.fulfill({ status: 503, json: { detail: "Monitor is temporarily unavailable" } })
    : route.continue());
  await page.goto("/#/wanted");
  await expect(wantedRows(page).first()).toBeVisible();
  await expect(page.getByRole("button", { name: "Retry recovery status" })).toBeVisible();
  failing = false;
  await page.getByRole("button", { name: "Retry recovery status" }).click();
  await expect(page.getByRole("button", { name: "Retry recovery status" })).toHaveCount(0);
});

test("Wanted stays useful while the monitor request is still pending", async ({ page }) => {
  let releaseMonitor!: () => void;
  const monitorGate = new Promise<void>(resolve => { releaseMonitor = resolve; });
  await page.route("**/api/monitor/status", async route => {
    await monitorGate;
    await route.continue();
  });
  try {
    await page.goto("/#/wanted");
    await expect(wantedRows(page).first()).toBeVisible();
  } finally {
    releaseMonitor();
  }
});

for (const target of ["library", "wanted"] as const) {
  test(`${target}: initial request failure has a persistent retry and recovers`, async ({ page }) => {
    let failing = true;
    await page.route(target === "library" ? (url: URL) => url.pathname === "/api/manga" : "**/api/wanted?*", route => failing
      ? route.fulfill({ status: 503, json: { detail: "Temporary test outage" } })
      : route.continue());
    await page.goto(target === "library" ? "/" : "/#/wanted");
    const retry = page.getByRole("button", { name: `Retry ${target}`, exact: true });
    await expect(retry).toBeVisible();
    await expect(page.getByRole("alert").filter({ hasText: "Temporary test outage" }).first()).toBeVisible();
    failing = false;
    await retry.click();
    await expect((target === "library" ? libraryCards(page) : wantedRows(page)).first()).toBeVisible();
  });
}

test("failed lazy page preserves navigation and recovers on reload", async ({ page }) => {
  let failing = true;
  await page.route("**/WantedPage-*.js", route => failing ? route.abort() : route.continue());
  await page.goto("/");
  await expect(libraryCards(page).first()).toBeVisible();
  await page.locator('a.nav-item[href="#/wanted"]').click();
  await expect(page.getByRole("heading", { name: "This page could not be loaded" })).toBeVisible();
  await expect(page.locator(".sidebar")).toBeVisible();
  failing = false;
  await page.getByRole("button", { name: "Reload app", exact: true }).click();
  await expect(wantedRows(page).first()).toBeVisible();
});

test("Library and quick search share one request and see a saved rename", async ({ page }) => {
  const libraryRequests: URL[] = [];
  page.on("request", request => {
    const url = new URL(request.url());
    if (url.pathname === "/api/manga") libraryRequests.push(url);
  });
  // Hold the initial GET until the quick search asks for the same snapshot.
  let releaseLibrary!: () => void;
  const gate = new Promise<void>(resolve => { releaseLibrary = resolve; });
  await page.route((url: URL) => url.pathname === "/api/manga", async route => {
    await gate;
    await route.continue();
  });
  await page.goto("/");
  const search = page.getByRole("combobox", { name: "Search series in your library" });
  await search.fill("Browser Series 000");
  releaseLibrary();
  await expect(page.locator(".search-option").filter({ hasText: "Browser Series 000" })).toBeVisible();
  expect(libraryRequests.filter(url => url.searchParams.get("cached") === "true")).toHaveLength(1);
  // One shared first-paint request, followed by one background revalidation.
  await expect.poll(() => libraryRequests.filter(url => url.searchParams.get("fresh") === "true").length).toBe(1);
  await page.locator(".search-option").filter({ hasText: "Browser Series 000" }).click();
  await page.getByRole("button", { name: "Edit", exact: true }).click();
  await page.getByLabel("Choose a known title").selectOption("__custom__");
  await page.getByLabel("Series title", { exact: true }).fill("Renamed Browser Work");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Renamed Browser Work", exact: true })).toBeVisible();
  await search.fill("Renamed Browser Work");
  await expect(page.locator(".search-option").filter({ hasText: "Renamed Browser Work" })).toBeVisible();
});

test("Wanted refresh failure keeps visible rows, then retry succeeds", async ({ page }) => {
  await page.goto("/#/wanted");
  await expect(wantedRows(page).first()).toBeVisible();
  await page.route("**/api/wanted?*", route => route.fulfill({
    status: 503, json: { detail: "Refresh failed but cached data is valid" },
  }));
  await page.getByRole("button", { name: "Refresh wanted", exact: true }).click();
  await expect(page.getByRole("button", { name: "Retry wanted", exact: true })).toBeVisible();
  await expect(wantedRows(page).first()).toBeVisible();
  await page.unroute("**/api/wanted?*");
  await page.getByRole("button", { name: "Retry wanted", exact: true }).click();
  await expect(page.getByRole("button", { name: "Retry wanted", exact: true })).toHaveCount(0);
});

test("returning to Wanted displays the snapshot before revalidation completes", async ({ page }) => {
  await page.goto("/#/wanted");
  await expect(wantedRows(page).first()).toBeVisible();
  await page.locator('a.nav-item[href="#/"]').click();
  await expect(libraryCards(page).first()).toBeVisible();
  let releaseRefresh!: () => void;
  const gate = new Promise<void>(resolve => { releaseRefresh = resolve; });
  let revalidations = 0;
  await page.route("**/api/wanted?*", async route => {
    revalidations += 1;
    await gate;
    await route.continue();
  });
  try {
    await page.locator('a.nav-item[href="#/wanted"]').click();
    await expect(wantedRows(page)).toHaveCount(20);
    await expect.poll(() => revalidations).toBe(1);
    await expect(wantedRows(page).first()).toBeVisible();
  } finally {
    releaseRefresh();
  }
});

test("a superseded Wanted response cannot overwrite the newer refresh", async ({ page }) => {
  await page.goto("/#/wanted");
  await expect(wantedRows(page).first()).toBeVisible();
  const payload = await (await page.request.get("/api/wanted?compact=true")).json();
  const newest = [{ ...payload[0], manga: { ...payload[0].manga, title: "Newest response" } }];
  let releaseOld!: () => void;
  const oldGate = new Promise<void>(resolve => { releaseOld = resolve; });
  let calls = 0;
  let oldFinished = false;
  await page.route("**/api/wanted?*", async route => {
    calls += 1;
    if (calls === 1) {
      await oldGate;
      try {
        await route.fulfill({ json: payload });
      } catch {
        // Aborting the superseded request may close the browser's route.
      } finally {
        oldFinished = true;
      }
    } else {
      await route.fulfill({ json: newest });
    }
  });
  try {
    const refresh = page.getByRole("button", { name: "Refresh wanted", exact: true });
    await refresh.click();
    await expect.poll(() => calls).toBe(1);
    await refresh.click();
    await expect(wantedRows(page).first()).toContainText("Newest response");
    releaseOld();
    await expect.poll(() => oldFinished).toBe(true);
    await expect(wantedRows(page).first()).toContainText("Newest response");
    await page.locator('a.nav-item[href="#/"]').click();
    await expect(libraryCards(page).first()).toBeVisible();
    await page.locator('a.nav-item[href="#/wanted"]').click();
    await expect(wantedRows(page).first()).toContainText("Newest response");
  } finally {
    releaseOld();
  }
});

test("Library refresh failure preserves cards and can recover", async ({ page }) => {
  await page.goto("/");
  await expect(libraryCards(page)).toHaveCount(30);
  await page.route(/\/api\/manga(?:\?.*)?$/, route => route.fulfill({
    status: 503, json: { detail: "Library refresh outage" },
  }));
  await page.getByRole("button", { name: "Refresh library", exact: true }).click();
  const retry = page.getByRole("button", { name: "Retry library", exact: true });
  await expect(retry).toBeVisible();
  await expect(libraryCards(page)).toHaveCount(30);
  await page.unroute(/\/api\/manga(?:\?.*)?$/);
  await retry.click();
  await expect(retry).toHaveCount(0);
});

test("changing a still-loading cover preserves the replacement image request", async ({ page }) => {
  const payload = await (await page.request.get("/api/manga")).json();
  let replacement = false;
  await page.route(/\/api\/manga(?:\?.*)?$/, route => route.fulfill({
    json: payload.map((manga: Record<string, unknown>, index: number) => index === 0
      ? { ...manga, artwork_url: `/browser-cover-${replacement ? "new" : "old"}.svg` }
      : manga),
  }));
  let releaseImages!: () => void;
  const gate = new Promise<void>(resolve => { releaseImages = resolve; });
  await page.route("**/browser-cover-*.svg", async route => {
    await gate;
    try {
      await route.fulfill({
        contentType: "image/svg+xml",
        body: '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="480"><rect width="320" height="480" fill="#268b7e"/></svg>',
      });
    } catch {
      // The superseded image is allowed to be cancelled by the browser.
    }
  });
  try {
    await page.goto("/", { waitUntil: "domcontentloaded" });
    const cover = libraryCards(page).first().locator("img");
    await expect(cover).toHaveAttribute("src", "/browser-cover-old.svg");
    replacement = true;
    await page.getByRole("button", { name: "Refresh library", exact: true }).click();
    await expect(cover).toHaveAttribute("src", "/browser-cover-new.svg");
    releaseImages();
    await expect.poll(() => cover.evaluate((img: HTMLImageElement) => img.complete && img.naturalWidth > 0)).toBe(true);
    await expect(cover).toHaveAttribute("src", "/browser-cover-new.svg");
  } finally {
    releaseImages();
  }
});

test("mobile Wanted navigation and filtering fit the viewport", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/#/wanted");
  await expect(wantedRows(page).first()).toBeVisible();
  await wantedFilter(page).fill("Browser Series 012");
  await wantedFilter(page).press("Escape");
  await expect(wantedRows(page).first()).toContainText("Browser Series 012");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
});

test("slow network and CPU still allow navigating and filtering", async ({ page, context }) => {
  const cdp = await context.newCDPSession(page);
  await cdp.send("Network.enable");
  await cdp.send("Network.emulateNetworkConditions", {
    offline: false, latency: 150, downloadThroughput: 200_000,
    uploadThroughput: 100_000, connectionType: "cellular3g",
  });
  await cdp.send("Emulation.setCPUThrottlingRate", { rate: 4 });
  await page.goto("/#/wanted");
  await expect(wantedRows(page).first()).toBeVisible({ timeout: 30_000 });
  await wantedFilter(page).fill("Browser Series 012");
  await expect(page.locator(".list-result-count")).toContainText("100 of");
  await expect(wantedRows(page)).toHaveCount(20);
});
