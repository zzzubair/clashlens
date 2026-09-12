import { expect, test } from "@playwright/test";

import {
  ensureAccount,
  expectNoSeriousAccessibilityViolations,
  signIn,
  signInDiscord,
} from "./helpers/account";

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

test("an account can link, use, and unlink a second sign-in provider", async ({
  page,
}) => {
  await signIn(page);
  await ensureAccount(page, "providerflow", "Provider Flow");
  const accountLabel = (await page.locator(".hero .player-tag").textContent())?.trim();
  expect(accountLabel).toMatch(/^@/);

  await page.goto("/account/providers");
  const discordRow = page.locator("li").filter({ hasText: "Discord" });
  await discordRow.getByRole("button", { name: "Link" }).click();
  await expect(page).toHaveURL(/\/account\/providers$/);
  await expect(discordRow.getByRole("button", { name: "Unlink" })).toBeVisible();

  await page.getByRole("button", { name: "Log out" }).click();
  await signInDiscord(page);
  await expect(page.getByText(accountLabel!, { exact: true })).toBeVisible();

  await page.goto("/account/providers");
  await discordRow.getByRole("button", { name: "Unlink" }).click();
  await expect(page).toHaveURL(/\/login$/);

  await signIn(page);
  await expect(page.getByText(accountLabel!, { exact: true })).toBeVisible();
  await page.goto("/account/providers");
  await expect(discordRow.getByRole("button", { name: "Link" })).toBeVisible();
});
