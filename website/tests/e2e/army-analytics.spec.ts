import { expect, test, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

// Must match FIXTURE_PREVIOUS_SEASON in fixture_server.py.
const PREVIOUS_SEASON = "1783916800";

async function expectNoSeriousAccessibilityViolations(page: Page) {
  const results = await new AxeBuilder({ page }).analyze();
  const seriousViolations = results.violations.filter((violation) =>
    ["critical", "serious"].includes(violation.impact ?? ""),
  );
  expect(seriousViolations, JSON.stringify(seriousViolations, null, 2)).toEqual([]);
}

test("anonymous day-one empty state links to the previous season without JavaScript", async ({
  browser,
}) => {
  const context = await browser.newContext({ javaScriptEnabled: false });
  const page = await context.newPage();
  const response = await page.goto("/analytics/armies");

  expect(response?.status()).toBe(200);
  await expect(page.getByText("No completed Legend days this season")).toBeVisible();
  const link = page.getByRole("link", { name: "View the previous season" });
  await expect(link).toHaveAttribute("href", new RegExp(`season=${PREVIOUS_SEASON}`));
  await link.click();

  // The previous-season URL loads its publication server-side.
  await expect(page).toHaveURL(new RegExp(`season=${PREVIOUS_SEASON}`));
  await expect(page.getByRole("table", { name: "Army analytics results" })).toBeVisible();
  await context.close();
});

test("direct loads render the URL-backed selection server-side without JavaScript", async ({
  browser,
}) => {
  const context = await browser.newContext({ javaScriptEnabled: false });
  const page = await context.newPage();
  const response = await page.goto(
    `/analytics/armies?season=${PREVIOUS_SEASON}&lens=defense&start_day=2&end_day=8&population=streak-top-10&category=heroes&sort=usage-count`,
  );

  expect(response?.status()).toBe(200);
  await expect(page.getByText(/Legend Days 2–8 · defense · streak-top-10/)).toBeVisible();
  await expect(page.getByText(/shielded member-days: 2/)).toBeVisible();
  // Row labels are th[scope=row], exposed as rowheader cells.
  await expect(page.getByRole("rowheader", { name: "Ice Golem" })).toBeVisible();
  await context.close();
});

test("analytics page has no serious or critical accessibility violations", async ({
  page,
}) => {
  await page.goto(`/analytics/armies?season=${PREVIOUS_SEASON}`);
  await expect(page.getByRole("table", { name: "Army analytics results" })).toBeVisible();
  await expectNoSeriousAccessibilityViolations(page);
});

test("analytics tables stay usable at a narrow viewport", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 900 });
  await page.goto(`/analytics/armies?season=${PREVIOUS_SEASON}`);
  await expect(page.getByRole("table", { name: "Army analytics results" })).toBeVisible();
});
