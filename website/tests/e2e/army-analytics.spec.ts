import { expect, test } from "@playwright/test";

import { expectNoSeriousAccessibilityViolations } from "./helpers/account";

test("army analytics loads the real backend's current honest state", async ({ page }) => {
  const response = await page.goto("/analytics/armies");

  expect([200, 404]).toContain(response?.status());
  await expect(page.getByRole("heading", { name: "Army analytics" })).toBeVisible();
  await expect(page.getByRole("form", { name: "Army analytics filters" })).toBeVisible();
  await expectNoSeriousAccessibilityViolations(page);
});
