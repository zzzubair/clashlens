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

  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/players/%232PP");
  const removeSaved = page.getByRole("button", { name: "Remove from Saved Players" });
  const addSaved = page.getByRole("button", { name: "Add to Saved Players" });
  await expect(removeSaved).toBeEnabled();
  await removeSaved.click();
  await expect(addSaved).toBeEnabled();
  await expect(page).toHaveURL(/\/players\/%232PP$/);
  await page.getByRole("link", { name: "View Saved Players" }).click();
  await expect(page.getByRole("heading", { name: "No saved players yet" })).toBeVisible();
  await expect(page.getByLabel("Player tag")).toBeVisible();
  await page.goto("/players/%232PP");
  await addSaved.click();
  await expect(removeSaved).toBeEnabled();
  await page.reload();
  await expect(removeSaved).toBeEnabled();
  expect(
    await removeSaved.evaluate((element) => {
      const box = element.getBoundingClientRect();
      return box.left >= 0 && box.right <= window.innerWidth;
    }),
  ).toBe(true);
  await page.getByRole("link", { name: "View Saved Players" }).click();
  await expect(savedPlayer).toBeVisible();
  await page.setViewportSize({ width: 1280, height: 720 });

  await page.goto("/account/groups");
  if (await page.getByRole("heading", { name: "No private groups yet" }).isVisible()) {
    await page.getByLabel("Group name").first().fill("War plan");
    await page.getByRole("button", { name: "Create group" }).click();
    await expect(page.getByRole("heading", { name: "War plan" })).toBeVisible();
    await expect(page.getByLabel("Group name").first()).toHaveValue("");
    await expect(
      page
        .locator(".group-card")
        .filter({ has: page.getByRole("heading", { name: "War plan" }) })
        .getByLabel("Player tag"),
    ).toHaveValue("");
  }
  await expect(page.getByRole("heading", { name: "War plan" })).toBeVisible();
  const warPlan = page.locator(".group-card").filter({
    has: page.getByRole("heading", { name: "War plan" }),
  });
  const players = warPlan.getByRole("list", { name: "Players in War plan" });
  if ((await players.count()) === 0) {
    // Players join one at a time, after the game confirms the tag.
    await warPlan.getByRole("link", { name: "Add player" }).click();
    await warPlan.getByLabel("Player tag").fill("#2PP");
    await warPlan.getByRole("button", { name: "Add", exact: true }).click();
  }
  await expect(players).toBeVisible();
  // Renaming, removing and deleting wait behind Edit.
  await expect(warPlan.getByRole("button", { name: /^Remove / })).toHaveCount(0);
  const editToggle = warPlan.getByRole("link", { name: "Edit", exact: true });
  const closedAt = await editToggle.boundingBox();
  await editToggle.click();
  const edit = warPlan.locator(".group-edit");
  await expect(edit).toBeVisible();
  // Opening Edit leaves its button where it was; the panel opens below the row.
  expect(await editToggle.boundingBox()).toEqual(closedAt);
  await expect(edit.getByLabel("Group name")).toHaveValue("War plan");
  await expect(
    edit.getByRole("button", { name: /^Remove .+ from War plan$/ }),
  ).toBeVisible();
  const yesDelete = edit.getByRole("button", { name: "Yes, delete group" });
  await edit.locator("summary", { hasText: "Delete group" }).click();
  await expect(yesDelete).toBeVisible();
  await edit.getByRole("link", { name: "Keep group" }).click();
  await expect(yesDelete).toBeHidden();
  await editToggle.click();
  await expect(edit).toBeHidden();
  await expect(players).toBeVisible();

  // Back returns to Your groups in one step, however many views were opened.
  for (const back of [
    () => page.getByRole("link", { name: "Back to your groups" }).click(),
    () => page.goBack(),
  ]) {
    await warPlan.getByRole("link", { name: "Compare players" }).click();
    await page.getByRole("link", { name: "14 days", exact: true }).click();
    await expect(page).toHaveURL(/days=14/);
    await page.getByRole("link", { name: "Attack", exact: true }).click();
    await expect(page).toHaveURL(/sort=attack/);
    await back();
    await expect(page).toHaveURL((url) => url.pathname === "/account/groups");
    await expect(page.getByRole("heading", { name: "Your groups" })).toBeVisible();
  }

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

test("the account name opens Account and Log out beside Join Discord", async ({
  page,
  browser,
  baseURL,
}) => {
  await signIn(page);
  await ensureAccount(page, "lensscout", "Lens Scout");
  await page.goto("/about");
  const header = page.getByRole("navigation", { name: "Account and appearance" });
  const discord = header.getByRole("link", { name: /^Join Discord/ });
  await expect(discord).toHaveAttribute("href", "https://discord.gg/792KJQTtRf");
  await expect(discord).toHaveAttribute("target", "_blank");
  const menu = header.locator("details.account-menu");
  const toggle = menu.locator("summary");
  const account = header.getByRole("link", { name: "Account", exact: true });
  const logOut = header.getByRole("button", { name: "Log out" });
  await expect(menu).not.toHaveAttribute("open");
  await expect(logOut).toBeHidden();

  await toggle.focus();
  await page.keyboard.press("Enter");
  await expect(menu).toHaveAttribute("open");
  await page.keyboard.press("Tab");
  await expect(account).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(account).toBeHidden();
  await expect(menu).not.toHaveAttribute("open");
  await expect(toggle).toBeFocused();

  await toggle.click();
  await expect(logOut).toBeVisible();
  await page.getByRole("heading", { name: "About Clash Lens" }).click();
  await expect(logOut).toBeHidden();

  await page.setViewportSize({ width: 390, height: 844 });
  await toggle.click();
  for (const control of [toggle, discord, account, logOut]) {
    const box = await control.boundingBox();
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(390);
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
  await account.click();
  await expect(page).toHaveURL(/\/users\/[a-z][a-z0-9_]+$/);
  await expect(account).toBeHidden();

  const noScript = await browser.newContext({
    javaScriptEnabled: false,
    baseURL,
    storageState: await page.context().storageState(),
  });
  try {
    const noScriptPage = await noScript.newPage();
    await noScriptPage.goto("/about");
    const noScriptHeader = noScriptPage.getByRole("navigation", {
      name: "Account and appearance",
    });
    await noScriptHeader.locator("details.account-menu > summary").click();
    await expect(noScriptHeader.getByRole("button", { name: "Log out" })).toBeVisible();
    await noScriptHeader.getByRole("link", { name: "Account", exact: true }).click();
    await expect(noScriptPage).toHaveURL(/\/users\/[a-z][a-z0-9_]+$/);
  } finally {
    await noScript.close();
  }

  await toggle.click();
  await logOut.click();
  await expect(page).toHaveURL("/");
  await expect(
    header.getByRole("link", { name: "Account", exact: true }),
  ).toHaveAttribute("href", "/login");
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
