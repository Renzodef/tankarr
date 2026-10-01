import { expect, test } from "@playwright/test";

// The online demo is the real interface in front of a recording, served by
// a service worker from a static host. Every page must come up from the
// recording alone: no server, no network beyond the static files. The
// recording may hold the fictional library or the real works, so the pages
// visited are taken from the recording itself.

test("the library, a series, the reader and the other pages come up from the recording", async ({ page }) => {
  const misses: string[] = [];
  page.on("response", (response) => {
    const url = new URL(response.url());
    if (url.pathname.includes("/api/") && response.status() >= 400) misses.push(`${response.status()} ${url.pathname}${url.search}`);
  });

  await page.goto("./");
  await expect(page.locator(".demo-banner")).toContainText("demo of Tankarr");
  await expect(page.locator(".poster-card").first()).toBeVisible();
  const covers = page.locator(".poster-card img");
  await expect.poll(async () => covers.count()).toBeGreaterThan(5);
  await page.waitForFunction(() => Array.from(document.images).every((image) => image.complete && image.naturalWidth > 0));

  const recording = await page.evaluate(async () => {
    const response = await fetch("demo-data/recording.json");
    const responses = (await response.json()).responses as Record<string, { status: number }>;
    return Object.entries(responses).filter(([, entry]) => entry.status === 200).map(([key]) => key);
  });
  const series = [...new Set(recording.map((key) => /^GET \/api\/manga\/([^/?]+)(?:\?.*)?$/.exec(key)?.[1]).filter((id): id is string => Boolean(id)))];
  const books = recording.map((key) => /^GET \/api\/reader\/books\/([^/?]+)$/.exec(key)?.[1]).filter((id): id is string => Boolean(id));
  expect(series.length, "series pages in the recording").toBeGreaterThan(5);
  expect(books.length, "readable books in the recording").toBeGreaterThan(0);

  await page.goto(`./#/series/${series[0]}`);
  await expect(page.locator("h1")).not.toBeEmpty();
  await page.waitForLoadState("networkidle");

  await page.goto(`./#/reader/${books[0]}`);
  await expect(page.getByAltText(/^Page 1 of/)).toBeVisible();
  await page.waitForFunction(() => Array.from(document.images).every((image) => image.complete && image.naturalWidth > 0));

  for (const hash of ["#/calendar", "#/wanted", "#/activity", "#/history", "#/system", "#/settings"]) {
    await page.goto(`./${hash}`);
    await expect(page.locator("main")).toBeVisible();
    await page.waitForLoadState("networkidle");
  }
  expect(misses, "requests the recording could not answer").toEqual([]);
});
