import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

async function expectNoSeriousAccessibilityViolations(page: Page) {
  const results = await new AxeBuilder({ page }).analyze();
  const seriousViolations = results.violations.filter((violation) =>
    ["critical", "serious"].includes(violation.impact ?? ""),
  );
  expect(seriousViolations, JSON.stringify(seriousViolations, null, 2)).toEqual([]);
}

test("home has no serious or critical accessibility violations", async ({ page }) => {
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "Clash Lens", exact: true }),
  ).toBeVisible();
  await expectNoSeriousAccessibilityViolations(page);
});

test("the skip link moves focus to the main content target", async ({ page }) => {
  await page.goto("/");
  const skipLink = page.getByRole("link", { name: "Skip to main content" });
  await skipLink.focus();
  await expect(skipLink).toBeFocused();
  await page.keyboard.press("Enter");

  await expect(page.locator("#main-content")).toBeFocused();
});

test("player page has no serious or critical accessibility violations", async ({
  page,
}) => {
  await page.goto("/players/%232PP");
  await expect(page.getByRole("heading", { name: "Nova" })).toBeVisible();
  await expectNoSeriousAccessibilityViolations(page);
});

test("public pages remain usable at a narrow viewport and 200 percent zoom", async ({
  page,
}) => {
  await page.setViewportSize({ width: 375, height: 900 });
  await page.goto("/");
  await page.evaluate(() => {
    document.documentElement.style.zoom = "2";
  });

  await expect(
    page.getByRole("searchbox", { name: "Search player tags or names" }),
  ).toBeVisible();
  await expect(page.getByRole("table", { name: "Live leaderboard" })).toBeVisible();
  expect(
    await page
      .locator(".table-wrap")
      .evaluate((element) => element.scrollWidth > element.clientWidth),
  ).toBe(true);

  await page.goto("/players/%232PP");
  await page.evaluate(() => {
    document.documentElement.style.zoom = "2";
  });
  await expect(page.getByRole("button", { name: "Refresh", exact: true })).toBeVisible();
});
