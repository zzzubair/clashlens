import { expect, test } from "@playwright/test";

import {
  ensureAccount,
  expectNoPageErrors,
  expectNoPortRequests,
  signIn,
  trackPageErrors,
  trackRequests,
} from "./helpers/account";

test("a Clasher can sign in and use account features against the real backend", async ({
  page,
}) => {
  const errors = trackPageErrors(page);
  const requests = trackRequests(page);

  await signIn(page);
  await ensureAccount(page, "lensscout", "Lens Scout");

  await page.getByRole("link", { name: "Manage saved players" }).click();
  if (await page.getByRole("heading", { name: "No saved players yet" }).isVisible()) {
    await page.getByLabel("Player tag").fill("#2PP");
    await page.getByRole("button", { name: "Save player" }).click();
  }
  await expect(page.getByText("#2PP", { exact: true }).first()).toBeVisible();

  await page.goto("/account/groups");
  if (await page.getByRole("heading", { name: "No private groups yet" }).isVisible()) {
    await page.getByLabel("Group name").first().fill("War plan");
    await page.getByLabel("Player tags").first().fill("#2PP");
    await page.getByRole("button", { name: "Create group" }).click();
  }
  await expect(page.getByRole("heading", { name: "War plan" })).toBeVisible();

  await page.goto("/account/verify-player");
  await page.getByLabel("Player tag").fill("#2PP");
  await page.getByLabel("One-time verification token").fill("VERIFY-2PP");
  await page.getByRole("button", { name: "Verify player" }).click();
  await expect(
    page.getByText(/The player was verified and linked|This player is already linked/),
  ).toBeVisible();

  await page.goto("/users/lensscout");
  await expect(page.getByRole("heading", { name: "Lens Scout" })).toBeVisible();
  await expect(page.getByText("#2PP", { exact: true })).toBeVisible();

  expectNoPortRequests(requests, 8000, "private Python API");
  expectNoPageErrors(errors);
});

test("account pages redirect anonymous users to login", async ({ page }) => {
  await page.goto("/account");
  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
});
