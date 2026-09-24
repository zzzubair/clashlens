import { expect, test } from "@playwright/test";

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
  await expect(
    page.getByRole("heading", { name: "Latest saved standings" }),
  ).toBeVisible();
  await expect(page.getByRole("table", { name: "Latest saved standings" })).toBeVisible();
});

test("player search uses saved backend data", async ({ page }) => {
  await page.goto("/?q=Synthetic%20Clasher%20001");

  await expect(
    page.getByRole("heading", { name: "Clash of Clans players" }),
  ).toBeVisible();
  await expect(page.getByText("Synthetic Clasher 001").first()).toBeVisible();
  await expect(page.getByText("#2PP", { exact: true }).first()).toBeVisible();
});

test("search and player links navigate without rebuilding the document", async ({
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
  expect(
    await page.evaluate(
      () =>
        (window as Window & { clashLensNavigationMarker?: boolean })
          .clashLensNavigationMarker,
    ),
  ).toBe(true);
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
