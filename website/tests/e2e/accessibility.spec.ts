import { expect, test } from "@playwright/test";

import { expectNoSeriousAccessibilityViolations } from "./helpers/account";

for (const [name, path, heading] of [
  ["home", "/", "Clash Lens"],
  ["player", "/players/%232PP", "Synthetic Clasher 001"],
  ["leaderboard", "/leaderboards/tracked", "Live leaderboard"],
  ["login", "/login", "Sign in"],
] as const) {
  test(`${name} has no serious or critical accessibility violations`, async ({
    page,
  }) => {
    await page.goto(path);
    await expect(page.getByRole("heading", { name: heading }).first()).toBeVisible();
    await expectNoSeriousAccessibilityViolations(page);
  });
}

test("the skip link moves focus to the main content", async ({ page }) => {
  await page.goto("/");
  const skipLink = page.getByRole("link", { name: "Skip to main content" });
  await skipLink.focus();
  await page.keyboard.press("Enter");

  await expect(page.locator("#main-content")).toBeFocused();
});
