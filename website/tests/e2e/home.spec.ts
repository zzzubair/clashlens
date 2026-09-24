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

test("public pages render without browser JavaScript", async ({ browser }) => {
  const context = await browser.newContext({ javaScriptEnabled: false });
  const page = await context.newPage();

  const response = await page.goto("/");
  expect(response?.status()).toBe(200);
  await expect(page.getByRole("table", { name: "Latest saved standings" })).toBeVisible();

  await context.close();
});

test("search suggestions keep the exact tag alongside a matching player name", async ({
  page,
}) => {
  await page.route("**/resources/players/search?*", async (route) => {
    await route.fulfill({
      json: {
        search: {
          exactTag: "#2PP",
          users: [],
          results: [{ tag: "#2PY", name: "#2PP", clan: "Test clan", trophies: 5000 }],
        },
        error: null,
      },
    });
  });
  await page.goto("/");
  await page
    .getByRole("searchbox", { name: "Search players and Clash Lens profiles" })
    .fill("#2PP");
  const suggestions = page.getByRole("region", {
    name: "Player and profile search suggestions",
  });
  await expect(
    suggestions.getByRole("link", { name: "Open #2PP Player tag" }),
  ).toHaveAttribute("href", "/players/%232PP");
  await expect(suggestions.locator('a[href="/players/%232PY"]')).toBeVisible();
});
