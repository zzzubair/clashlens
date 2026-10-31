import { expect, test } from "@playwright/test";

import {
  ensureAccount,
  expectAccountButtonsRead,
  expectNoPageErrors,
  expectNoPortRequests,
  linkPlayer,
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

  // Saved Players became Watchlists: no menu link, and the old address opens them.
  await expect(
    page
      .getByRole("navigation", { name: "Main navigation" })
      .getByRole("link", { name: "Saved players" }),
  ).toHaveCount(0);
  await page.goto("/account/saved-players");
  await expect(page).toHaveURL((url) => url.pathname === "/account/watchlists");

  await page.goto("/account/watchlists");
  if (await page.getByRole("heading", { name: "No watchlists yet" }).isVisible()) {
    await page.getByLabel("Watchlist name").first().fill("War plan");
    await page.getByRole("button", { name: "Create watchlist" }).click();
    await expect(page.getByRole("heading", { name: "War plan" })).toBeVisible();
    await expect(page.getByLabel("Watchlist name").first()).toHaveValue("");
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
    await warPlan.locator(".group-more > summary").click();
    await warPlan.getByRole("link", { name: "Add player" }).click();
    await warPlan.getByLabel("Player tag").fill("#2PP");
    await warPlan.getByRole("button", { name: "Add", exact: true }).click();
  }
  await expect(players).toBeVisible();
  // Renaming, removing and deleting wait behind Edit, in the card's ⋯ menu.
  await expect(warPlan.getByRole("button", { name: /^Remove / })).toHaveCount(0);
  const cardMenu = warPlan.locator(".group-more > summary");
  const editToggle = warPlan.getByRole("link", { name: "Edit", exact: true });
  const closedAt = await cardMenu.boundingBox();
  await cardMenu.click();
  await editToggle.click();
  const edit = warPlan.locator(".group-edit");
  await expect(edit).toBeVisible();
  // Opening Edit leaves the menu where it was; the panel opens below the row.
  expect(await cardMenu.boundingBox()).toEqual(closedAt);
  await expect(edit.getByLabel("Watchlist name")).toHaveValue("War plan");
  await expect(
    edit.getByRole("button", { name: /^Remove .+ from War plan$/ }),
  ).toBeVisible();
  const yesDelete = edit.getByRole("button", { name: "Yes, delete watchlist" });
  await edit.locator("summary", { hasText: "Delete watchlist" }).click();
  await expect(yesDelete).toBeVisible();
  await edit.getByRole("link", { name: "Keep watchlist" }).click();
  await expect(yesDelete).toBeHidden();
  await cardMenu.click();
  await editToggle.click();
  await expect(edit).toBeHidden();
  await expect(players).toBeVisible();

  // A profile saves a player into a group; War plan already holds #2PP.
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/players/%232PP");
  const addToGroup = page.getByRole("link", { name: "Add to watchlist" });
  expect(
    await addToGroup.evaluate((element) => {
      const box = element.getBoundingClientRect();
      return box.left >= 0 && box.right <= window.innerWidth;
    }),
  ).toBe(true);
  await addToGroup.click();
  await expect(page).toHaveURL(/\/account\/watchlists\/add\/2PP$/);
  await expect(
    page.getByRole("heading", { name: "Add #2PP to a watchlist" }),
  ).toBeVisible();
  await expect(page.getByText(/#2PP is already in/).first()).toBeVisible();
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto("/account/watchlists");

  // Back returns to Your watchlists in one step, however many views were opened.
  for (const back of [
    () => page.getByRole("link", { name: "Back to Your watchlists" }).click(),
    () => page.goBack(),
  ]) {
    await warPlan.getByRole("link", { name: "Compare players" }).click();
    await page.getByRole("link", { name: "14 days", exact: true }).click();
    await expect(page).toHaveURL(/days=14/);
    await page.getByRole("link", { name: "Attack", exact: true }).click();
    await expect(page).toHaveURL(/sort=attack/);
    await back();
    await expect(page).toHaveURL((url) => url.pathname === "/account/watchlists");
    await expect(page.getByRole("heading", { name: "Your watchlists" })).toBeVisible();
  }

  // A player opened from the comparison goes Back to it, on the same view
  // (sorted by Gain, its default).
  await warPlan.getByRole("link", { name: "Compare players" }).click();
  await page.getByRole("link", { name: "14 days", exact: true }).click();
  await expect(page).toHaveURL(/days=14&sort=net$/);
  await page.locator(".compare-name").first().click();
  await expect(page).toHaveURL(/\/players\//);
  await page.getByRole("link", { name: "Back to War plan" }).click();
  await expect(page).toHaveURL(/\/account\/watchlists\/[^/]+\?days=14&sort=net$/);

  await page.goto("/account/verify-player");
  await linkPlayer(page, "#2PP", /\/users\/[a-z][a-z0-9_]+\?linked=%232PP$/);
  await expect(page.getByRole("status")).toHaveText(/ linked\.$/);
  const linkedAccounts = page.getByRole("region", { name: "Clash players" });
  await expect(linkedAccounts.getByText("#2PP", { exact: true })).toBeVisible();

  await page.goto("/users/lensscout");
  await expect(page.getByRole("heading", { name: "Lens Scout" })).toBeVisible();
  await expect(page.getByText("#2PP", { exact: true })).toBeVisible();

  expectNoPortRequests(requests, 8000, "private Python API");
  expectNoPageErrors(errors);
});

test("the account name opens your profile, tools, Settings and Sign out beside Join Discord", async ({
  page,
  browser,
  baseURL,
}) => {
  await signIn(page);
  await ensureAccount(page, "lensscout", "Lens Scout");
  await page.goto("/blog");
  const header = page.getByRole("banner");
  const discord = header.getByRole("link", { name: /^Join Discord/ });
  await expect(discord).toHaveAttribute("href", "https://discord.gg/792KJQTtRf");
  await expect(discord).toHaveAttribute("target", "_blank");
  const menu = header.locator("details.account-menu");
  const toggle = menu.locator("summary");
  const account = header.getByRole("link", { name: "Your profile", exact: true });
  const signOut = header.getByRole("button", { name: "Sign out" });
  await expect(menu).not.toHaveAttribute("open");
  await expect(signOut).toBeHidden();

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
  await expect(signOut).toBeVisible();
  await page.getByRole("heading", { name: "Blog", exact: true }).click();
  await expect(signOut).toBeHidden();

  await page.setViewportSize({ width: 390, height: 844 });
  await toggle.click();
  // On a phone Join Discord sits in the bottom bar's More sheet.
  for (const control of [toggle, account, signOut]) {
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
    await noScriptPage.goto("/blog");
    const noScriptHeader = noScriptPage.getByRole("banner");
    await noScriptHeader.locator("details.account-menu > summary").click();
    await expect(noScriptHeader.getByRole("button", { name: "Sign out" })).toBeVisible();
    await noScriptHeader.getByRole("link", { name: "Your profile", exact: true }).click();
    await expect(noScriptPage).toHaveURL(/\/users\/[a-z][a-z0-9_]+$/);
  } finally {
    await noScript.close();
  }

  await toggle.click();
  for (const [name, path] of [
    ["Discord alerts", "/account/discord-alerts"],
    ["Stream overlay", "/account/obs-overlay"],
    ["Settings", "/account/settings"],
  ])
    await expect(header.getByRole("link", { name, exact: true })).toHaveAttribute(
      "href",
      path,
    );
  await signOut.click();
  await expect(page).toHaveURL("/?saved=logout");
  await expect(
    header.getByRole("link", { name: "Sign in", exact: true }),
  ).toHaveAttribute("href", "/login?returnPath=%2F");
  await expect(header.getByRole("link", { name: "Account", exact: true })).toHaveCount(0);
});

test("a page that needs an account says why, with Create account and Sign in", async ({
  page,
}) => {
  for (const [path, title, reason] of [
    ["/account", "Your account", "Sign in to see them."],
    ["/account/watchlists", "Watchlists", "Sign in to keep them."],
    ["/crews", "Crews", "Sign in to make or join one."],
  ] as const) {
    const response = await page.goto(path);
    expect(response?.status()).toBe(401);
    await expect(page.getByRole("heading", { name: title, level: 1 })).toBeVisible();
    const box = page.getByRole("region", { name: "Account needed" });
    await expect(box).toContainText(reason);
    const back = encodeURIComponent(path);
    await expect(box.getByRole("link", { name: "Create account" })).toHaveAttribute(
      "href",
      `/signup?returnPath=${back}`,
    );
    await expect(box.getByRole("link", { name: "Sign in" })).toHaveAttribute(
      "href",
      `/login?returnPath=${back}`,
    );
  }
  // Before an account exists there is no Account menu or Sign out.
  await expect(page.getByRole("banner").getByText("Sign out")).toHaveCount(0);
});

test("Already have one? stays with its Sign in on a phone and on a desktop", async ({
  page,
}) => {
  for (const [width, height, phone] of [
    [390, 844, true],
    [1280, 800, false],
  ] as const) {
    await page.setViewportSize({ width, height });
    for (const path of ["/crews", "/account/watchlists", "/account"]) {
      await page.goto(path);
      await expectAccountButtonsRead(page, phone);
    }
  }
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
    await page.getByLabel("Username", { exact: true }).fill(username);
    await page.getByLabel("Display name").fill("Early Scout");
    releaseScripts();
    await page.waitForLoadState("networkidle");
    // This control only works once the page JavaScript handles user input.
    await page.getByRole("button", { name: "Dark mode" }).click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
    await expect(page.getByLabel("Username", { exact: true })).toHaveValue(username);
    await expect(page.getByLabel("Display name")).toHaveValue("Early Scout");
    await page.getByRole("button", { name: "Create account" }).click();
    // A new account is asked to link a player next.
    await expect(page).toHaveURL(/\/account\/verify-player$/);
    await page.goto("/account");
    await expect(page).toHaveURL(new RegExp(`/users/${username}$`));
    await expect(page.getByRole("heading", { name: "Early Scout" })).toBeVisible();
  } finally {
    releaseScripts();
  }
});
