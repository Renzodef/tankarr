import { expect, test } from "@playwright/test";

// The online demo is the real interface in front of a recording, served by
// a service worker from a static host. Every page must come up from the
// recording alone: no server, no network beyond the static files.

test("the library, a series, the reader and the other pages come up from the recording", async ({ page }) => {
  const misses: string[] = [];
  page.on("response", (response) => {
    const url = new URL(response.url());
    if (url.pathname.includes("/api/") && response.status() >= 400) misses.push(`${response.status()} ${url.pathname}${url.search}`);
  });

  await page.goto("./");
  await expect(page.locator(".demo-banner")).toContainText("fictional library");
  await expect(page.locator(".poster-card").first()).toBeVisible();
  const covers = page.locator(".poster-card img");
  await expect.poll(async () => covers.count()).toBeGreaterThan(5);
  await page.waitForFunction(() => Array.from(document.images).every((image) => image.complete && image.naturalWidth > 0));

  await page.goto("./#/series/cartographers-daughter");
  await expect(page.locator("h1")).toContainText("The Cartographer's Daughter");

  await page.goto("./#/reader/cartographers-daughter-c141");
  const firstPage = page.getByAltText(/^Page 1 of/);
  await expect(firstPage).toBeVisible();
  await page.waitForFunction(() => Array.from(document.images).every((image) => image.complete && image.naturalWidth > 0));

  for (const hash of ["#/calendar", "#/wanted", "#/activity", "#/history", "#/system", "#/settings"]) {
    await page.goto(`./${hash}`);
    await expect(page.locator("main")).toBeVisible();
    await page.waitForLoadState("networkidle");
  }

  // A write is acknowledged and the page keeps working.
  await page.goto("./#/settings");
  await page.waitForLoadState("networkidle");
  expect(misses, "requests the recording could not answer").toEqual([]);
});
