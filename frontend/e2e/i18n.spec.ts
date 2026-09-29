import { expect, test } from "@playwright/test";

const calendarLink = 'a.nav-item[href="#/calendar"]';

test("the interface follows the language chosen in this browser", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator(calendarLink)).toContainText("Calendar");
  await expect(page.locator("html")).toHaveAttribute("lang", "en");

  // The choice lives in localStorage, so a reload is enough to switch.
  await page.evaluate(() => window.localStorage.setItem("tankarr.locale", "it"));
  await page.reload();
  await expect(page.locator(calendarLink)).toContainText("Calendario");
  await expect(page.locator('a.nav-item[href="#/wanted"]')).toContainText("Ricercati");
  await expect(page.locator("html")).toHaveAttribute("lang", "it");

  // Settings → General offers the same choice and reloads the page.
  await page.locator('a.nav-item[href="#/settings"]').click();
  await expect(page.getByRole("heading", { name: "Lingua dell'interfaccia" })).toBeVisible();
  const selector = page.locator("#setting-interface-language");
  await expect(selector).toHaveValue("it");
  await selector.selectOption("en");
  await expect(page.locator(calendarLink)).toContainText("Calendar");
  await expect(page.locator("html")).toHaveAttribute("lang", "en");
  expect(await page.evaluate(() => window.localStorage.getItem("tankarr.locale"))).toBe("en");
});
