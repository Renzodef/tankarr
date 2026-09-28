import { expect, test } from "@playwright/test";

// This project's server runs with TANKARR_URL_BASE=/tankarr (see playwright.config.ts).
const origin = "http://127.0.0.1:18879";

test("the interface, its assets and the API work under a URL base", async ({ page, baseURL }) => {
  const errors: string[] = [];
  const failed: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("response", (response) => {
    if (response.status() >= 400) failed.push(`${response.status()} ${response.url()}`);
  });
  await page.goto(baseURL!);
  await expect(page.locator(".poster-card")).toHaveCount(30);
  expect(page.url()).toContain("/tankarr/");
  await page.locator('a.nav-item[href="#/wanted"]').click();
  await expect(page.locator(".wanted-table tbody tr")).toHaveCount(20);
  await page.locator(".wanted-table tbody tr").first().getByRole("link").first().click();
  await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
  expect(failed).toEqual([]);
  expect(errors).toEqual([]);

  const health = await page.request.get(`${origin}/tankarr/api/health`);
  expect(health.ok()).toBeTruthy();
  const rootHealth = await page.request.get(`${origin}/api/health`);
  expect(rootHealth.ok()).toBeTruthy();
  const outside = await page.request.get(`${origin}/api/manga`);
  expect(outside.status()).toBe(404);
  const redirect = await page.request.get(`${origin}/tankarr`, { maxRedirects: 0 });
  expect(redirect.status()).toBe(307);
  expect(redirect.headers()["location"]).toBe("/tankarr/");
});
