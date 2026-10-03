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

  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("link", { name: "Saved players" })
    .click();
  await expect(
    page.getByRole("heading", { name: "Saved players", exact: true }),
  ).toBeVisible();
  const emptySavedPlayers = page.getByRole("heading", {
    name: "No saved players yet",
  });
  const savedPlayer = page.getByText("#2PP", { exact: true }).first();
  await expect(emptySavedPlayers.or(savedPlayer)).toBeVisible();
  if (await emptySavedPlayers.isVisible()) {
    await page.getByLabel("Player tag").fill("#2PP");
    await page.getByRole("button", { name: "Save player" }).click();
  }
  await expect(savedPlayer).toBeVisible();

  await page.goto("/account/groups");
  if (await page.getByRole("heading", { name: "No private groups yet" }).isVisible()) {
    await page.getByLabel("Group name").first().fill("War plan");
    await page.getByLabel("Player tags").first().fill("#2PP");
    await page.getByRole("button", { name: "Create group" }).click();
    await expect(page.getByRole("heading", { name: "War plan" })).toBeVisible();
    await expect(page.getByLabel("Group name").first()).toHaveValue("");
    await expect(page.getByLabel("Player tags").first()).toHaveValue("");
  }
  await expect(page.getByRole("heading", { name: "War plan" })).toBeVisible();

  await page.goto("/account/verify-player");
  await page.getByLabel("Player tag").fill("#2PP");
  await page.getByLabel("API token", { exact: true }).fill("VERIFY-2PP");
  await page.getByRole("button", { name: "Link player" }).click();
  await expect(page).toHaveURL(/\/users\/[a-z][a-z0-9_]+\?linked=%232PP$/);
  await expect(page.getByText("Linked #2PP", { exact: true })).toBeVisible();
  const linkedAccounts = page.getByRole("region", { name: "Linked accounts" });
  await expect(linkedAccounts.getByText("#2PP", { exact: true })).toBeVisible();

  await page.goto("/users/lensscout");
  await expect(page.getByRole("heading", { name: "Lens Scout" })).toBeVisible();
  await expect(page.getByText("#2PP", { exact: true })).toBeVisible();

  expectNoPortRequests(requests, 8000, "private Python API");
  expectNoPageErrors(errors);
});

test("account pages redirect anonymous users to login", async ({ page }) => {
  await page.goto("/account");
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
});

test("account setup accepts names entered before the page JavaScript loads", async ({
  page,
}) => {
  const username = "earlyscout";
  await page.route("**/authorize?*", async (route) => {
    const url = new URL(route.request().url());
    url.searchParams.set("login_hint", "fixture-google-subject-2003");
    await route.continue({ url: url.href });
  });
  await signIn(page);
  await expect(page).toHaveURL(/\/account\/setup$/);

  let releaseScripts!: () => void;
  const scriptsReleased = new Promise<void>((resolve) => {
    releaseScripts = resolve;
  });
  await page.route("**/assets/*.js", async (route) => {
    await scriptsReleased;
    await route.continue();
  });
  try {
    await page.reload({ waitUntil: "commit" });
    await page.getByLabel("Username").fill(username);
    await page.getByLabel("Display name").fill("Early Scout");
    releaseScripts();
    await page.waitForLoadState("networkidle");
    // This control only works once the page JavaScript handles user input.
    await page.getByRole("button", { name: "Dark mode" }).click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    await expect(page.getByLabel("Username")).toHaveValue(username);
    await expect(page.getByLabel("Display name")).toHaveValue("Early Scout");
    await page.getByRole("button", { name: "Create account" }).click();
    await expect(page).toHaveURL(new RegExp(`/users/${username}$`));
    await expect(page.getByRole("heading", { name: "Early Scout" })).toBeVisible();
  } finally {
    releaseScripts();
  }
});
