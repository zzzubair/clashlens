import { expect, test } from "@playwright/test";

import { expectNoSeriousAccessibilityViolations } from "./helpers/account";

test("loopback Google and Discord sign-in choices are available", async ({ page }) => {
  await page.goto("/login");

  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Continue with Google" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Continue with Discord" })).toBeVisible();
  await expectNoSeriousAccessibilityViolations(page);
});

test("Discord completes a local sign-in without cloud credentials", async ({ page }) => {
  await page.goto("/login");
  await page.getByRole("link", { name: "Continue with Discord" }).click();

  await expect(page).toHaveURL(/\/account(\/setup)?$/);
  await expect(
    page.getByRole("heading", { name: /Create your account|Your account/ }),
  ).toBeVisible();
});
