import { expect, test } from "@playwright/test";

test("home and the full leaderboard show collected synthetic players", async ({
  page,
}) => {
  await page.goto("/");

  await expect(
    page.getByRole("heading", { name: "Clash Lens", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("searchbox", { name: "Search player tags or names" }),
  ).toBeVisible();
  const homeTable = page.getByRole("table", { name: "Live leaderboard" });
  await expect(homeTable).toBeVisible();
  await expect(homeTable.getByText("Synthetic Clasher 001")).toBeVisible();
  await expect(homeTable.getByText("#2PP", { exact: true })).toBeVisible();

  await page.getByRole("link", { name: "View all →" }).click();
  await expect(page).toHaveURL(/\/leaderboards\/tracked\?view=live&page=1$/);
  await expect(page.getByRole("heading", { name: "Live leaderboard" })).toBeVisible();
  await expect(page.getByRole("table", { name: "Live leaderboard" })).toBeVisible();
});

test("player search uses saved backend data", async ({ page }) => {
  await page.goto("/?q=Synthetic%20Clasher%20001");

  await expect(
    page.getByRole("heading", { name: "Known Clash Lens players" }),
  ).toBeVisible();
  await expect(page.getByText("Synthetic Clasher 001").first()).toBeVisible();
  await expect(page.getByText("#2PP", { exact: true }).first()).toBeVisible();
});

test("public pages render without browser JavaScript", async ({ browser }) => {
  const context = await browser.newContext({ javaScriptEnabled: false });
  const page = await context.newPage();

  const response = await page.goto("/");
  expect(response?.status()).toBe(200);
  await expect(page.getByRole("table", { name: "Live leaderboard" })).toBeVisible();

  await context.close();
});
