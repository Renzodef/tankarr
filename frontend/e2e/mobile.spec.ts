import { expect, test } from "@playwright/test";

for (const width of [360, 390, 768]) {
  test(`mobile ${width}: navigate and add a series with reachable dialog actions`, async ({ page }) => {
    await page.setViewportSize({ width, height: 740 });
    const work = { id: "catalogue-mobile", title: "A Long Series Title That Must Remain Readable on a Small Phone", authors: ["Test Author"], description: "Description ".repeat(80), status: "ended", volume_count: 10, chapter_count: 54, in_library: false, work_type: "Manga" };
    await page.route("**/api/search?*", route => route.fulfill({ json: { works: [work], results: [work], errors: [] } }));
    await page.route("**/api/manga/catalogue-mobile/preview?*", route => route.fulfill({ json: work }));
    let submitted: Record<string, unknown> | undefined;
    await page.route("**/api/manga", route => {
      if (route.request().method() !== "POST") return route.continue();
      submitted = route.request().postDataJSON();
      return route.fulfill({ json: { id: "browser-000", queued: 0 } });
    });
    await page.goto("/");
    await expect(page.locator(".poster-card").first()).toBeVisible();
    await expect(page.locator(".sidebar")).not.toBeVisible();
    await page.getByRole("button", { name: "Open navigation" }).click();
    const menu = page.getByRole("dialog", { name: "Navigation" });
    await expect(menu.getByRole("link", { name: "Settings", exact: true })).toBeVisible();
    await menu.getByRole("link", { name: "Add New", exact: true }).click();
    await expect(menu).not.toBeVisible();
    const search = page.getByRole("searchbox", { name: "Search the catalogue" });
    await search.fill("Test series");
    await search.press("Enter");
    await page.locator(".result-card").getByRole("button", { name: "Add", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: `Add ${work.title}` });
    await expect(dialog.getByLabel("Reading language")).toBeVisible();
    const save = dialog.getByRole("button", { name: "Add to library", exact: true });
    await expect(save).toBeEnabled();
    if (width === 390) await page.screenshot({ path: test.info().outputPath("add-series-phone.png") });
    // Model the space left above an on-screen keyboard.
    await page.setViewportSize({ width, height: 430 });
    await expect(save).toBeInViewport();
    const bounds = await save.boundingBox();
    expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(430);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await save.click();
    await expect(page).toHaveURL(/#\/series\/browser-000$/);
    expect(submitted).toMatchObject({ manga_id: "catalogue-mobile", language: "en", monitor_mode: "all" });
  });
}

test("mobile navigation closes with Escape and restores focus", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/#/wanted");
  const toggle = page.getByRole("button", { name: "Open navigation" });
  await toggle.click();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("dialog", { name: "Navigation" })).not.toBeVisible();
  await expect(toggle).toBeFocused();
  await toggle.click();
  await page.setViewportSize({ width: 1280, height: 800 });
  await expect(page.getByRole("dialog", { name: "Navigation" })).not.toBeVisible();
  await expect(page.locator(".sidebar")).toBeVisible();
});

test("mobile primary pages keep controls inside the viewport", async ({ page }) => {
  test.setTimeout(90_000);
  await page.setViewportSize({ width: 390, height: 844 });
  for (const route of ["", "series/browser-000", "wanted", "calendar", "activity", "history", "import", "settings", "settings?tab=translation", "settings?tab=indexers", "settings?tab=reader", "settings?tab=security", "settings?tab=data", "system"]) {
    await page.goto(`/#/${route}`);
    await expect(page.locator(".content .page")).toBeVisible();
    await expect(page.locator(".content .spinner")).toHaveCount(0);
    const sizes = await page.locator(".content").evaluate(element => ({ width: element.clientWidth, scroll: element.scrollWidth }));
    expect(sizes.scroll, route).toBeLessThanOrEqual(sizes.width + 1);
    if (route === "settings") {
      await page.getByRole("combobox", { name: "Settings section", exact: true }).selectOption("translation");
      await expect(page).toHaveURL(/settings\?tab=translation$/);
    }
    await page.screenshot({ path: test.info().outputPath(`${route.replace(/[^a-z0-9-]/gi, "-") || "library"}-phone.png`) });
  }
});
