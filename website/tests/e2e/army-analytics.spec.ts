import { expect, test } from "@playwright/test";

import { expectNoSeriousAccessibilityViolations } from "./helpers/account";

test("army analytics loads the real backend's current honest state", async ({ page }) => {
  const response = await page.goto("/analytics/armies");

  expect([200, 404]).toContain(response?.status());
  await expect(page.getByRole("heading", { name: "Army analytics" })).toBeVisible();
  await expect(page.getByRole("form", { name: "Army analytics filters" })).toBeVisible();
  await expect(
    page
      .getByRole("region", { name: "Army evidence coverage" })
      .or(
        page.getByText(
          /^(No completed Legend days this season|No completed Legend-day army publication is available for this selection\.|Army analytics are unavailable for the selected Legend days\.)$/,
        ),
      ),
  ).toBeVisible();
  await expectNoSeriousAccessibilityViolations(page);
});

test("missing historical summary stays unavailable with legacy filters in the URL", async ({
  page,
}) => {
  const response = await page.goto(
    "/analytics/armies?season=missing-history-126&start_day=1&end_day=1&population=top-5",
  );
  expect(response?.status()).toBe(404);
  await expect(
    page.getByText("Army analytics are unavailable for the selected Legend days."),
  ).toBeVisible();
  await expect(page.getByRole("table")).toHaveCount(0);
  await expectNoSeriousAccessibilityViolations(page);
});

test("retired IDs render honest labels, quantities and stars before and after naming", async ({
  page,
}) => {
  const season = process.env.CLASHLENS_E2E_HISTORY_SEASON;
  const unknown = process.env.CLASHLENS_E2E_HISTORY_NAMES === "unknown";
  test.skip(!season, "Requires the retired-history PostgreSQL restore fixture.");
  for (const [category, namespace, id, quantity] of [
    ["troops", "troop", 900, 5],
    ["siege", "siege", 901, 1],
    ["spells", "spell", 900, 1],
    ["heroes", "hero", 900, 1],
    ["pets", "pet", 900, 1],
    ["equipment", "equipment", 900, 1],
  ] as const) {
    const response = await page.goto(`/analytics/armies?season=${season}&category=${category}`);
    expect(response?.status()).toBe(200);
    const kind = category === "troops" || category === "siege" ? "troop or siege" : namespace;
    const label = unknown ? `Unknown ${kind} (ID ${id})` : `Named ${namespace}`;
    const row = page.getByRole("row").filter({ hasText: label });
    await expect(row).toContainText("1 / 2");
    await expect(row).toContainText("50.0%");
    await expect(row.getByRole("cell").first()).toHaveText(String(quantity));
    await expect(row.getByRole("cell").nth(1)).toHaveText("0");
    await expect(row.getByRole("cell").nth(2)).toHaveText("1");
    await expect(row.getByRole("cell").nth(3)).toHaveText("0");
    await expect(page.getByRole("columnheader", { name: "Quantity" })).toBeVisible();
    await expect(page.getByRole("columnheader", { name: "Average stars" })).toHaveCount(0);
    await expect(page.getByLabel("Start Legend day")).toBeDisabled();
    if (unknown) {
      await expect(page.getByText(/Collection coverage partial/)).toBeVisible();
    } else if (category === "troops" || category === "siege") {
      await expect(page.getByRole("row").filter({ hasText: category === "troops" ? "Named siege" : "Named troop" })).toHaveCount(0);
    }
    await expectNoSeriousAccessibilityViolations(page);
  }
});
