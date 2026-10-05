import { expect, type Locator, type Page, test } from "@playwright/test";

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
  await page.goto("/players/2PP");
  const removeSaved = page.getByRole("button", { name: "Remove from Saved Players" });
  const addSaved = page.getByRole("button", { name: "Add to Saved Players" });
  await expect(removeSaved).toBeEnabled();
  await removeSaved.click();
  await expect(addSaved).toBeEnabled();
  await expect(page).toHaveURL(/\/players\/2PP$/);
  await page.getByRole("link", { name: "View Saved Players" }).click();
  await expect(page.getByRole("heading", { name: "No saved players yet" })).toBeVisible();
  await expect(page.getByLabel("Player tag")).toBeVisible();
  await page.goto("/players/2PP");
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
        .getByLabel("Add player"),
    ).toHaveValue("");
  }
  await expect(page.getByRole("heading", { name: "War plan" })).toBeVisible();
  const warPlan = page.locator(".group-card").filter({
    has: page.getByRole("heading", { name: "War plan" }),
  });
  const member = warPlan.getByRole("button", { name: /^Remove .+ from War plan$/ });
  if ((await member.count()) === 0) {
    // Players join one at a time, after the game confirms the tag.
    await warPlan.getByLabel("Add player").fill("#2PP");
    await warPlan.getByRole("button", { name: "Add player" }).click();
  }
  await expect(member).toBeVisible();

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

/** A group holding #2PP, whose trophies the fixtures keep current. */
async function openSeasonWatch(page: Page): Promise<Locator> {
  await signIn(page);
  await ensureAccount(page, "lensscout", "Lens Scout");
  await page.goto("/account/groups");
  const watch = page.locator(".group-card").filter({
    has: page.getByRole("heading", { name: "Season watch" }),
  });
  if ((await watch.count()) === 0) {
    await page.getByLabel("Group name").first().fill("Season watch");
    await page.getByRole("button", { name: "Create group" }).click();
    await watch.getByLabel("Add player").fill("#2PP");
    await watch.getByRole("button", { name: "Add player" }).click();
  }
  await expect(watch.locator("li").filter({ hasText: "#2PP" })).toBeVisible();
  return watch;
}

/**
 * Open `path` once, then move the browser clock past the end of the Season it
 * loaded: any Season current now ends within 28 days.
 */
async function expectTrophiesToExpire(
  page: Page,
  path: string,
  trophies: Locator,
  shown: RegExp,
): Promise<void> {
  const errors = trackPageErrors(page);
  const reloads: string[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === `${path}.data`) reloads.push(request.url());
  });
  await page.clock.install();
  await page.goto(path);
  await expect(trophies).toHaveText(shown);
  expect(reloads).toEqual([]);

  // The browser clock wraps any single jump past 2**31 - 1 ms (about 24.8 days).
  await page.clock.fastForward(14 * 86_400_000);
  await page.clock.fastForward(14 * 86_400_000);
  await expect(trophies).toHaveText("Waiting for this player's Season reset");
  await expect.poll(() => reloads.length).toBe(1);
  await expect(trophies).toHaveText("Waiting for this player's Season reset");
  expectNoPageErrors(errors);
}

test("an open group list stops showing trophies when their Season ends", async ({
  page,
}) => {
  const watch = await openSeasonWatch(page);
  await expectTrophiesToExpire(
    page,
    "/account/groups",
    watch.locator("li").filter({ hasText: "#2PP" }).locator(".group-member-detail"),
    /^[\d,]+ trophies$/,
  );
});

test("an open group comparison stops showing trophies when their Season ends", async ({
  page,
}) => {
  const watch = await openSeasonWatch(page);
  const compare = await watch
    .getByRole("link", { name: "Compare players" })
    .getAttribute("href");
  expect(compare).toMatch(/^\/account\/groups\/[0-9a-f-]+$/);
  await expectTrophiesToExpire(
    page,
    compare as string,
    page
      .getByRole("row")
      .filter({ hasText: "#2PP" })
      .locator('[data-label="Trophies now"]'),
    /^\d/,
  );
});
