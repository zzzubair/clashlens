import { expect, test } from "@playwright/test";

test("fan content notice is exact, linked and readable on phones in both themes", async ({
  page,
}) => {
  await page.setViewportSize({ width: 320, height: 812 });
  await page.goto("/");
  const footer = page.getByRole("contentinfo");
  await expect(footer.locator("p")).toHaveText(
    "This material is unofficial and is not endorsed by Supercell. For more information see Supercell's Fan Content Policy: www.supercell.com/fan-content-policy.",
  );
  await expect(
    footer.getByRole("link", { name: "www.supercell.com/fan-content-policy" }),
  ).toHaveAttribute("href", "https://www.supercell.com/fan-content-policy");
  for (const theme of ["light", "dark"]) {
    // Save the choice like the theme toggle does; the page resets an unsaved theme.
    await page.evaluate((value) => localStorage.setItem("clashlens-theme", value), theme);
    await page.reload();
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    await footer.scrollIntoViewIfNeeded();
    await expect(footer).toBeVisible();
    const bounds = await footer.boundingBox();
    expect(bounds).not.toBeNull();
    expect(bounds!.x).toBeGreaterThanOrEqual(0);
    expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(320);
    expect(await footer.evaluate((element) => element.scrollWidth)).toBeLessThanOrEqual(
      bounds!.width,
    );
    expect(
      await footer.evaluate((element) => parseFloat(getComputedStyle(element).fontSize)),
    ).toBeGreaterThanOrEqual(12);
  }
});

test("header and footer About links open the About page", async ({ page }) => {
  await page.goto("/");
  await page
    .getByRole("navigation", { name: "Main navigation" })
    .getByRole("link", { name: "About" })
    .click();
  await expect(page).toHaveURL(/\/about$/);
  await expect(page.getByRole("heading", { name: "About Clash Lens" })).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Join the Discord", exact: true }),
  ).toHaveAttribute("href", "https://discord.gg/792KJQTtRf");
  await expect(page.getByRole("contentinfo").locator("p")).toHaveText(
    "This material is unofficial and is not endorsed by Supercell. For more information see Supercell's Fan Content Policy: www.supercell.com/fan-content-policy.",
  );

  await page.goto("/leaderboards/tracked");
  await page
    .getByRole("contentinfo")
    .getByRole("navigation", { name: "Site information" })
    .getByRole("link", { name: "About" })
    .click();
  await expect(page).toHaveURL(/\/about$/);
  await expect(page.getByRole("heading", { name: "About Clash Lens" })).toBeVisible();
});

test("home and the full leaderboard show collected synthetic players", async ({
  page,
}) => {
  await page.goto("/");

  await expect(
    page.getByRole("heading", { name: "Legend League", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("searchbox", { name: "Search players and Clash Lens profiles" }),
  ).toBeVisible();
  const homeTable = page.getByRole("table", { name: "Latest saved standings" });
  await expect(homeTable).toBeVisible();
  await expect(homeTable.getByText("Synthetic Clasher 001")).toBeVisible();
  await expect(homeTable.getByText("#2PP", { exact: true })).toBeVisible();

  await page.getByRole("link", { name: "Full rankings" }).click();
  await expect(page).toHaveURL(/\/leaderboards\/tracked\?view=live&page=1$/);
  await expect(page.getByRole("heading", { name: "Live Leaderboard" })).toBeVisible();
  await expect(page.getByRole("table", { name: "Live Leaderboard" })).toBeVisible();
});

test("player search uses saved backend data", async ({ page }) => {
  await page.goto("/?q=Synthetic%20Clasher%20001");

  await expect(
    page.getByRole("heading", { name: "Clash of Clans players" }),
  ).toBeVisible();
  await expect(page.getByText("Synthetic Clasher 001").first()).toBeVisible();
  await expect(page.getByText("#2PP", { exact: true }).first()).toBeVisible();
});

test("leaderboard updates stay visible and expandable on a 375 px phone", async ({
  page,
}) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/leaderboards/tracked?view=live&page=1");
  const table = page.getByRole("table", { name: "Live Leaderboard", exact: true });
  const rows = table.locator("tbody tr");
  expect(await rows.count()).toBeGreaterThan(0);
  for (const row of await rows.all()) {
    await expect(row.locator("summary")).toBeVisible();
    await expect(row.locator("summary")).toContainText("Last updated");
  }
  const update = rows.first().locator("details");
  await update.locator("summary").click();
  await expect(update.locator("time")).toBeVisible();
  await expect(update.locator("time")).toHaveAttribute("datetime", /.+/);
  await expect(update.locator("time")).toHaveAttribute("title", /UTC$/);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(375);
  expect(await table.evaluate((element) => element.scrollWidth)).toBeLessThanOrEqual(375);
});

test("leaderboard ages and old-data labels advance while the page stays open", async ({
  page,
}) => {
  await page.clock.install({ time: Date.now() + 60 * 60_000 });
  await page.goto("/leaderboards/tracked?view=live&page=1");
  const update = page
    .getByRole("table", { name: "Live Leaderboard", exact: true })
    .locator('tbody tr td[data-label="Last updated"]')
    .first();
  await expect(update).toContainText("Over 10 min old");
  const observedAt = Date.parse(
    (await update.locator("time").getAttribute("datetime")) ?? "",
  );
  await page.clock.setSystemTime(observedAt + 2 * 60_000);
  await page.clock.runFor(30_000);
  await expect(update).toContainText("2 minutes ago");
  await expect(update).not.toContainText("Over 10 min old");
  await page.clock.runFor(9 * 60_000);
  await expect(update).toContainText("11 minutes ago · Over 10 min old");
});

test("an out-of-range leaderboard page offers a working link to page one", async ({
  page,
}) => {
  const response = await page.goto("/leaderboards/tracked?view=live&page=999999");
  expect(response?.status()).toBe(404);
  await expect(
    page.getByRole("heading", { name: "This standings page is unavailable" }),
  ).toBeVisible();
  await page.getByRole("link", { name: "Go to page 1" }).click();
  await expect(page).toHaveURL(/view=live&page=1$/);
  await expect(
    page.getByRole("table", { name: "Live Leaderboard", exact: true }),
  ).toBeVisible();
});

test("search navigates without rebuilding the document and opens a matching player", async ({
  page,
}) => {
  await page.goto("/");
  await page.evaluate(() => {
    (
      window as Window & { clashLensNavigationMarker?: boolean }
    ).clashLensNavigationMarker = true;
  });

  await page
    .getByRole("searchbox", { name: "Search players and Clash Lens profiles" })
    .fill("Synthetic Clasher 001");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(page).toHaveURL(/\?q=Synthetic(?:\+|%20)Clasher(?:\+|%20)001$/);
  await expect(
    page.getByRole("heading", { name: "Clash of Clans players" }),
  ).toBeVisible();
  expect(
    await page.evaluate(
      () =>
        (window as Window & { clashLensNavigationMarker?: boolean })
          .clashLensNavigationMarker,
    ),
  ).toBe(true);

  await page
    .locator(".search-results")
    .getByRole("link", { name: "Synthetic Clasher 001" })
    .click();
  await expect(page).toHaveURL(/\/players\/%232PP$/);
  await expect(
    page.getByRole("heading", { name: "Synthetic Clasher 001" }),
  ).toBeVisible();
});

test("new search does not show suggestions from the previous query", async ({ page }) => {
  let releaseNewSearch = () => {};
  const delayedSearch = new Promise<void>((resolve) => {
    releaseNewSearch = resolve;
  });
  await page.route("**/resources/players/search*", async (route) => {
    if (new URL(route.request().url()).searchParams.get("q") === "Nobody Named This") {
      await delayedSearch;
    }
    await route.continue();
  });

  try {
    await page.goto("/");
    const input = page.getByRole("searchbox", {
      name: "Search players and Clash Lens profiles",
    });
    await input.fill("Synthetic Clasher 001");
    await expect(
      page.getByRole("region", { name: "Player and profile search suggestions" }),
    ).toContainText("Synthetic Clasher 001");

    const newRequest = page.waitForRequest((request) => {
      const url = new URL(request.url());
      return (
        url.pathname.startsWith("/resources/players/search") &&
        url.searchParams.get("q") === "Nobody Named This"
      );
    });
    await input.fill("Nobody Named This");
    await newRequest;
    await expect(
      page.getByRole("region", { name: "Player and profile search suggestions" }),
    ).not.toContainText("Synthetic Clasher 001");
  } finally {
    releaseNewSearch();
  }
});

test("public pages render without browser JavaScript", async ({ browser }) => {
  const context = await browser.newContext({ javaScriptEnabled: false });
  const page = await context.newPage();

  const response = await page.goto("/");
  expect(response?.status()).toBe(200);
  await expect(page.getByRole("table", { name: "Latest saved standings" })).toBeVisible();
  await page
    .getByRole("searchbox", { name: "Search players and Clash Lens profiles" })
    .fill("Synthetic Clasher 001");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(page).toHaveURL(/\?q=Synthetic(?:\+|%20)Clasher(?:\+|%20)001$/);
  await expect(
    page.getByRole("heading", { name: "Clash of Clans players" }),
  ).toBeVisible();

  await context.close();
});
