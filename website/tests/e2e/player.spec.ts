import { expect, test } from "@playwright/test";

test("player page canonicalizes the tag and shows collected profile data", async ({
  page,
}) => {
  await page.goto("/players/%232pp");

  await expect(page).toHaveURL(/\/players\/%232PP$/);
  await expect(
    page.getByRole("heading", { name: "Synthetic Clasher 001" }),
  ).toBeVisible();
  await expect(page.getByText("#2PP", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Daily Legend log", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Current trophies", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Refresh", exact: true })).toBeVisible();
});

test("player page stays within a narrow viewport", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 900 });
  await page.goto("/players/%232PP");

  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    ),
  ).toBe(0);
});

test("season navigation clears refresh state for the same player", async ({ page }) => {
  await page.goto("/players/%232PP");
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  const refresh = page.getByRole("region", { name: "Player refresh" });
  await expect(refresh).toBeVisible();

  const seasons = page.getByRole("navigation", { name: "Historical seasons" });
  await seasons.getByRole("link").first().click();
  await expect(page).toHaveURL(/\/players\/%232PP\?season=/);
  await expect(refresh).toHaveCount(0);

  await seasons.getByRole("link", { name: "Current season" }).click();
  await expect(page).toHaveURL(/\/players\/%232PP$/);
  await expect(refresh).toHaveCount(0);
});
