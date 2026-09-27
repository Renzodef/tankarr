import { expect, test, type Page } from "@playwright/test";

const seriesRequest = (id: string) => new RegExp(`/api/manga/${id}\\?`);
const seriesTitle = (page: Page, title: string) => page.getByRole("heading", {
  name: title, exact: true,
});
const triggerSeriesPoll = (page: Page) => page.evaluate(() => {
  window.dispatchEvent(new Event("focus"));
});

for (const failure of ["http", "network"] as const) {
  test(`series initial ${failure} failure is retryable, not a false 404`, async ({ page }) => {
    let failing = true;
    await page.route(seriesRequest("browser-040"), route => {
      if (!failing) return route.continue();
      return failure === "network" ? route.abort("connectionfailed") : route.fulfill({
        status: 503, json: { detail: "Series metadata temporarily unavailable" },
      });
    });
    await page.goto("/#/series/browser-040");
    const retry = page.getByRole("button", { name: "Retry series", exact: true });
    await expect(retry).toBeVisible();
    await expect(page.getByRole("alert")).toContainText(
      failure === "network" ? "API is unreachable" : "temporarily unavailable",
    );
    await expect(page.getByText("Series not found", { exact: false })).toHaveCount(0);
    failing = false;
    await retry.click();
    await expect(seriesTitle(page, "Browser Series 040")).toBeVisible();
    await expect(retry).toHaveCount(0);
  });
}

test("a real series 404 retains the explicit missing-series state", async ({ page }) => {
  const response = await page.request.get("/api/manga/browser-does-not-exist?compact=true");
  expect(response.status()).toBe(404);
  await page.goto("/#/series/browser-does-not-exist");
  await expect(page.getByText("Series not found", { exact: false })).toBeVisible();
  await expect(page.getByRole("button", { name: "Retry series", exact: true })).toHaveCount(0);
});

test("series polling failure preserves the snapshot and recovers with retry", async ({ page }) => {
  await page.goto("/#/series/browser-041");
  await expect(seriesTitle(page, "Browser Series 041")).toBeVisible();
  await page.route(seriesRequest("browser-041"), route => route.fulfill({
    status: 503, json: { detail: "Refresh unavailable" },
  }));
  await triggerSeriesPoll(page);
  const retry = page.getByRole("button", { name: "Retry series", exact: true });
  await expect(retry).toBeVisible();
  await expect(page.getByRole("alert")).toContainText("Showing the last loaded data.");
  await expect(seriesTitle(page, "Browser Series 041")).toBeVisible();
  await expect(page.getByText("Series not found", { exact: false })).toHaveCount(0);
  await page.unroute(seriesRequest("browser-041"));
  await retry.click();
  await expect(retry).toHaveCount(0);
  await expect(seriesTitle(page, "Browser Series 041")).toBeVisible();
});

test("a saved series rename supersedes a coalesced, still-pending poll", async ({ page }) => {
  const id = "browser-045";
  const original = await (await page.request.get(`/api/manga/${id}?compact=true`)).json();
  await page.goto(`/#/series/${id}`);
  await expect(seriesTitle(page, "Browser Series 045")).toBeVisible();
  let releaseOld!: () => void;
  const gate = new Promise<void>(resolve => { releaseOld = resolve; });
  let requests = 0;
  let oldFinished = false;
  await page.route(seriesRequest(id), async route => {
    requests += 1;
    if (requests !== 1) return route.continue();
    await gate;
    try {
      await route.fulfill({ json: original });
    } catch {
      // A post-edit refresh is allowed to abort the old request entirely.
    } finally {
      oldFinished = true;
    }
  });
  try {
    // The heading can paint before the parallel units request has settled. In
    // that short window a focus event correctly joins the initial load, so
    // keep waking the poll until the dedicated refresh has actually started.
    await expect.poll(async () => {
      await triggerSeriesPoll(page);
      return requests;
    }).toBe(1);
    await triggerSeriesPoll(page);
    await triggerSeriesPoll(page);
    await page.getByRole("button", { name: "Edit", exact: true }).click();
    expect(requests).toBe(1);
    await page.getByLabel("Choose a known title").selectOption("__custom__");
    await page.getByLabel("Series title", { exact: true }).fill("Newer series snapshot");
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(seriesTitle(page, "Newer series snapshot")).toBeVisible();
    releaseOld();
    await expect.poll(() => oldFinished).toBe(true);
    await expect(seriesTitle(page, "Newer series snapshot")).toBeVisible();
    await expect(seriesTitle(page, "Browser Series 045")).toHaveCount(0);
  } finally {
    releaseOld();
  }
});

test("leaving a loading series cannot populate the next route with its response", async ({ page }) => {
  let releaseOld!: () => void;
  const gate = new Promise<void>(resolve => { releaseOld = resolve; });
  const original = await (await page.request.get("/api/manga/browser-044?compact=true")).json();
  let requested = false;
  let finished = false;
  const errors: string[] = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.route(seriesRequest("browser-044"), async route => {
    requested = true;
    await gate;
    try {
      await route.fulfill({ json: original });
    } catch {
      // Navigating away may cancel the old request before fulfillment.
    } finally {
      finished = true;
    }
  });
  try {
    await page.goto("/#/series/browser-044");
    await expect.poll(() => requested).toBe(true);
    await page.evaluate(() => { window.location.hash = "/series/browser-043"; });
    await expect(seriesTitle(page, "Browser Series 043")).toBeVisible();
    releaseOld();
    await expect.poll(() => finished).toBe(true);
    await expect(seriesTitle(page, "Browser Series 043")).toBeVisible();
    await expect(seriesTitle(page, "Browser Series 044")).toHaveCount(0);
    expect(errors).toEqual([]);
  } finally {
    releaseOld();
  }
});

test("Activity requests a 64-pixel thumbnail instead of the original series cover", async ({ page }) => {
  await page.route("**/api/jobs/summary", route => route.fulfill({
    json: {
      counts: { queued: 1 }, active: 1,
      series: [{
        manga_id: "browser-042", manga_title: "Browser Series 042",
        manga_cover_url: "/api/metadata/artwork/browser-042/series?rev=fixture",
        queued: 1, working: 0, current: null,
      }],
    },
  }));
  const images: string[] = [];
  await page.route("**/api/metadata/artwork/browser-042/series**", route => {
    images.push(new URL(route.request().url()).pathname);
    return route.fulfill({
      contentType: "image/svg+xml",
      body: '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="96"><rect width="64" height="96" fill="#268b7e"/></svg>',
    });
  });
  await page.goto("/#/activity");
  const cover = page.locator("img.queue-series-cover");
  await expect(cover).toBeVisible();
  await expect(cover).toHaveAttribute("src", "/api/metadata/artwork/browser-042/series/thumbnail/64?rev=fixture");
  await expect.poll(() => cover.evaluate((image: HTMLImageElement) => image.complete && image.naturalWidth > 0)).toBe(true);
  expect(images).toEqual(["/api/metadata/artwork/browser-042/series/thumbnail/64"]);
});
