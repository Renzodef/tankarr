import { expect, test } from "@playwright/test";

for (const mode of ["manga", "webtoon"] as const) {
  test(`reader ${mode}: manual bookmark, return to series, resume in a new tab and slider direction`, async ({ page, context }) => {
    let bookmark: number | null = null;
    let writes = 0;
    await context.route("**/api/reader/series/browser-000/bookmark", async route => {
      writes++;
      if (route.request().method() === "DELETE") {
        bookmark = null;
        return route.fulfill({ status: 204 });
      }
      bookmark = route.request().postDataJSON().page_index;
      return route.fulfill({ json: { chapter_id: "fixture-book", page_index: bookmark } });
    });
    await context.route("**/api/manga/browser-000/reader-link", route => route.fulfill({ json: {
      reader: "tankarr", configured: true, available: true, url: "#/reader/fixture-book",
      books: [], bookmark: bookmark === null ? null : { chapter_id: "fixture-book", label: "Volume 1", page_index: bookmark, url: `#/reader/fixture-book?page=${bookmark + 1}` },
    } }));
    await context.route("**/api/reader/books/fixture-book", route => route.fulfill({ json: {
      release_id: "fixture-book", manga_id: "browser-000", series_title: "Browser Series 000", book_title: "Volume 1",
      display_mode: mode, display_mode_source: "automatic", reading_direction: mode === "manga" ? "rtl" : "ltr", page_count: 5, page_index: bookmark ?? 0,
      bookmarked: bookmark !== null, bookmark_page_index: bookmark, previous_release_id: null, next_release_id: null,
    } }));
    await context.route("**/api/reader/books/fixture-book/pages/*", async route => {
      const number = Number(route.request().url().split("/").at(-1)) + 1;
      await route.fulfill({ contentType: "image/svg+xml", body: `<svg xmlns="http://www.w3.org/2000/svg" width="600" height="${mode === "webtoon" ? 2400 : 900}"><rect width="100%" height="100%" fill="#333"/><text x="100" y="150" fill="white" font-size="60">Page ${number}</text></svg>` });
    });
    await page.goto("/#/reader/fixture-book");
    const slider = page.getByRole("slider", { name: "Current page" });
    await expect(slider).toHaveValue("1");
    const bounds = await slider.boundingBox();
    expect(bounds).not.toBeNull();
    if (mode === "manga") {
      expect(bounds!.width).toBeGreaterThan(bounds!.height);
      await slider.click({ position: { x: bounds!.width * 0.25, y: bounds!.height / 2 } });
    } else {
      expect(bounds!.height).toBeGreaterThan(bounds!.width);
      expect(await page.locator(".reader-canvas").evaluate(node => getComputedStyle(node).scrollbarWidth)).toBe("none");
      await slider.click({ position: { x: bounds!.width / 2, y: bounds!.height * 0.75 } });
    }
    await expect(slider).toHaveValue("4");
    expect(writes).toBe(0);
    await page.getByRole("button", { name: "Save bookmark", exact: true }).click();
    await expect(page.getByRole("status")).toHaveText("Bookmark saved · Volume 1 · page 4");
    await page.getByTitle("Back to series").click();
    await expect(page.getByText("Volume 1 · page 4", { exact: true })).toBeVisible();
    const continueLink = page.getByRole("link", { name: "Continue from bookmark" });
    await expect(continueLink).toHaveAttribute("target", "_blank");
    const popupPromise = page.waitForEvent("popup");
    await continueLink.click();
    const readerTab = await popupPromise;
    const resumedSlider = readerTab.getByRole("slider", { name: "Current page" });
    await expect(resumedSlider).toHaveValue("4");
    if (mode === "webtoon") {
      await expect.poll(async () => {
        const canvas = await readerTab.locator(".reader-canvas").boundingBox();
        const image = await readerTab.getByAltText("Page 4 of 5").boundingBox();
        return Math.abs(image!.y - canvas!.y);
      }).toBeLessThan(5);
    }
    expect(writes).toBe(1);
    await readerTab.getByRole("button", { name: "Remove bookmark", exact: true }).click();
    await expect(readerTab.getByRole("status")).toHaveText("No bookmark in this book");
    await readerTab.close();
    await page.bringToFront();
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await expect(page.getByText("No bookmark saved · use Save bookmark in the reader")).toBeVisible();
    expect(writes).toBe(2);
  });
}

test.describe("mobile reader seek bar", () => {
  test.use({ viewport: { width: 390, height: 740 }, isMobile: true, hasTouch: true });

  for (const mode of ["manga", "webtoon"] as const) {
    test(`${mode}: full-width touch drag previews then opens the selected page`, async ({ page, context }) => {
      await page.route("**/api/reader/books/touch-book", route => route.fulfill({ json: {
        release_id: "touch-book", manga_id: "browser-000", series_title: "Example", book_title: "Volume 1",
        display_mode: mode, display_mode_source: "automatic", reading_direction: mode === "manga" ? "rtl" : "ltr", page_count: 40, page_index: 0,
        bookmarked: false, bookmark_page_index: null, previous_release_id: null, next_release_id: null,
      } }));
      await page.route("**/api/reader/books/touch-book/pages/*", route => route.fulfill({
        contentType: "image/svg+xml",
        body: '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="1800"><rect width="600" height="1800" fill="#444"/></svg>',
      }));
      await page.goto("/#/reader/touch-book");
      const slider = page.getByRole("slider", { name: "Current page" });
      await expect(slider).toHaveValue("1");
      await expect(slider).toHaveAttribute("aria-orientation", "horizontal");
      const box = (await slider.boundingBox())!;
      expect(box.width).toBeGreaterThanOrEqual(350);
      expect(box.height).toBeGreaterThanOrEqual(44);
      const footer = (await page.locator(".reader-footer").boundingBox())!;
      expect(footer.y + footer.height).toBeLessThanOrEqual(741);
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
      const canvas = page.locator(".reader-canvas");
      const scrollBefore = await canvas.evaluate(node => node.scrollTop);
      const touch = await context.newCDPSession(page);
      const y = box.y + box.height / 2;
      await touch.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: mode === "manga" ? box.x + box.width - 14 : box.x + 14, y }] });
      await touch.send("Input.dispatchTouchEvent", { type: "touchMove", touchPoints: [{ x: box.x + box.width * (mode === "manga" ? 0.25 : 0.75), y }] });
      const selected = Number(await slider.inputValue());
      expect(selected).toBeGreaterThan(20);
      await expect(page.locator(".reader-counter")).toHaveText(`${selected} / 40`);
      // Seeking previews the number without loading intermediate manga pages
      // or moving the webtoon canvas until the finger is released.
      if (mode === "manga") await expect(canvas.locator("img")).toHaveAttribute("alt", "Page 1 of 40");
      else expect(await canvas.evaluate(node => node.scrollTop)).toBe(scrollBefore);
      await touch.send("Input.dispatchTouchEvent", { type: "touchEnd", touchPoints: [] });
      if (mode === "manga") await expect(canvas.locator("img")).toHaveAttribute("alt", `Page ${selected} of 40`);
      else await expect.poll(async () => {
        const image = (await page.getByAltText(`Page ${selected} of 40`).boundingBox())!;
        return Math.abs(image.y - (await canvas.boundingBox())!.y);
      }).toBeLessThan(5);
      await expect(slider).toHaveValue(String(selected));
      await touch.send("Input.dispatchTouchEvent", { type: "touchStart", touchPoints: [{ x: box.x + box.width / 2, y }] });
      await touch.send("Input.dispatchTouchEvent", { type: "touchCancel", touchPoints: [] });
      await expect(slider).toHaveValue(String(selected));
      await expect(page.locator(".reader-counter")).toHaveText(`${selected} / 40`);
      await page.screenshot({ path: test.info().outputPath(`reader-${mode}-mobile.png`) });
      await page.setViewportSize({ width: 844, height: 390 });
      await expect(slider).toHaveAttribute("aria-orientation", "horizontal");
      const landscapeBar = (await slider.boundingBox())!;
      expect(landscapeBar.width).toBeGreaterThan(800);
      const landscapeFooter = (await page.locator(".reader-footer").boundingBox())!;
      expect(landscapeFooter.y + landscapeFooter.height).toBeLessThanOrEqual(391);
      await touch.detach();
    });
  }
});
