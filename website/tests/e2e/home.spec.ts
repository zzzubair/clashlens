import { expect, test } from "@playwright/test";

test("home presents the minimal Clash Lens leaderboard", async ({ page }) => {
  await page.goto("/");

  await expect(
    page.getByRole("heading", { name: "Clash Lens", exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("link", { name: "View all →" })).toBeVisible();
  await expect(
    page.getByRole("searchbox", { name: "Search player tags or names" }),
  ).toBeVisible();
  await expect(page.locator("[data-testid='tracked-player-row']")).toHaveCount(25);
  const leaderboard = page.getByRole("table", { name: "Live leaderboard" });
  await expect(leaderboard).toBeVisible();
  await expect(leaderboard.getByRole("link", { name: "View player →" })).toHaveCount(25);
  await expect(
    leaderboard.getByRole("link", { name: "View player →" }).first(),
  ).toHaveAttribute("href", "/players/%232PP");
  await expect(page.locator(".site-brand")).toHaveCount(0);
  await expect(page.getByRole("link", { name: /Back/ })).toHaveCount(0);
  await expect(page.getByRole("columnheader").allTextContents()).resolves.toEqual([
    "Rank",
    "Player",
    "Clan",
    "Trophies",
    "Last updated",
  ]);
  await expect(page.getByText("Official rank", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Confidence:", { exact: false })).toHaveCount(0);
});

test("search suggests at most five known players while typing", async ({ page }) => {
  await page.goto("/");
  const search = page.getByRole("searchbox", { name: "Search player tags or names" });

  await expect(search).toHaveAttribute("placeholder", "Player tag or Name");
  await search.fill("a");

  const suggestions = page.getByRole("region", { name: "Player search suggestions" });
  await expect(suggestions).toBeVisible();
  await expect(suggestions.getByTestId("search-suggestion")).toHaveCount(5);
  await expect(page).toHaveURL(/\/$/);
});

test("full live leaderboard presents only the Clash Lens rank and public fields", async ({
  page,
}) => {
  await page.goto("/leaderboards/tracked");

  await expect(
    page.getByRole("heading", { name: "Live leaderboard", exact: true, level: 1 }),
  ).toBeVisible();
  await expect(page.getByRole("columnheader").allTextContents()).resolves.toEqual([
    "Rank",
    "Player",
    "Clan",
    "Trophies",
    "Last updated",
  ]);
  await expect(page.getByText("Official rank", { exact: true })).toHaveCount(0);
  await expect(page.getByLabel("Data provenance")).toHaveCount(0);
  await expect(page.getByText("actively tracked Legend I cohort")).toHaveCount(0);
  await expect(
    page
      .getByRole("table", { name: "Live leaderboard" })
      .getByRole("link", { name: "View player →" }),
  ).toHaveCount(100);
});

test("live leaderboard paginates with canonical absolute ranks", async ({ page }) => {
  await page.goto("/leaderboards/tracked?view=live&page=1");
  await page.getByRole("link", { name: "Next" }).click();
  await expect(page).toHaveURL(/\/leaderboards\/tracked\?view=live&page=2$/);
  const table = page.getByRole("table", { name: "Live leaderboard" });
  await expect(table.getByRole("row")).toHaveCount(2);
  await expect(table.getByRole("row").nth(1).getByRole("cell").first()).toHaveText("101");
  await expect(page.getByRole("link", { name: "Previous" })).toBeVisible();
});

test("unselected Daily canonical redirect preserves its page", async ({ page }) => {
  await page.goto("/leaderboards/tracked?view=daily&page=2");
  await expect(page).toHaveURL(
    /\/leaderboards\/tracked\?view=daily&season=2026-08&day=21&page=2$/,
  );
  await expect(
    page.getByRole("table", { name: "Daily leaderboard" }).getByRole("row"),
  ).toHaveCount(2);
});

test("daily leaderboard renders the frozen production wire fixture", async ({ page }) => {
  await page.goto("/leaderboards/tracked?view=daily");

  await expect(
    page.getByRole("heading", {
      name: "Daily leaderboard · Day 21",
      exact: true,
      level: 1,
    }),
  ).toBeVisible();
  const table = page.getByRole("table", { name: "Daily leaderboard" });
  await expect(table).toBeVisible();
  await expect(table.getByRole("row")).toHaveCount(101);
  await expect(table.getByRole("row").nth(1)).toContainText("Nova");
  await expect(table.getByRole("row").nth(1)).toContainText("#2PP");
  await expect(table.getByRole("row").nth(1)).toContainText("7,211");
  await page.getByRole("link", { name: "Older" }).click();
  await expect(page).toHaveURL(
    /\/leaderboards\/tracked\?view=daily&season=2026-07&day=28&page=1$/,
  );
  await expect(
    page.getByRole("heading", { name: "Daily leaderboard · Day 28", level: 1 }),
  ).toBeVisible();
  await expect(page.getByRole("link", { name: "Newer" })).toBeVisible();
});

test("leaderboard rejects unsafe page integers", async ({ request }) => {
  const response = await request.get(
    "/leaderboards/tracked?view=live&page=9007199254740992",
  );
  expect(response.status()).toBe(422);
});

test("leaderboard preserves an upstream validation status", async ({ request }) => {
  const response = await request.get(
    "/leaderboards/tracked?view=daily&season=fixture-422&day=1&page=1",
  );
  expect(response.status()).toBe(422);
});

test("shared header back returns from a player to the originating page", async ({
  page,
}) => {
  await page.goto("/leaderboards/tracked?view=daily");
  await page.getByRole("link", { name: "View player →" }).first().click();

  await expect(page).toHaveURL(/\/players\/%232PP$/);
  const back = page.getByRole("link", { name: /Back/ });
  await expect(back).toHaveAttribute("href", "/");
  await expect(page.locator("main").getByRole("link", { name: /Back/ })).toHaveCount(0);
  await back.click();

  await expect(page).toHaveURL(
    /\/leaderboards\/tracked\?view=daily&season=2026-08&day=21&page=1$/,
  );
});

test("shared header back falls home when a new tab has no back entry", async ({
  page,
}) => {
  await page.goto("/leaderboards/tracked");
  const popupPromise = page.waitForEvent("popup");
  await page.evaluate(() => window.open("/players/%232PP", "_blank"));
  const playerPage = await popupPromise;

  await expect(playerPage).toHaveURL(/\/players\/%232PP$/);
  await expect.poll(() => playerPage.evaluate(() => window.history.length)).toBe(1);
  await expect
    .poll(() => playerPage.evaluate(() => document.referrer))
    .toMatch(/\/leaderboards\/tracked\?view=live&page=1$/);
  await playerPage.getByRole("link", { name: /Back/ }).click();

  await expect(playerPage).toHaveURL(/\/$/);
  await playerPage.close();
});

test("home SSR keeps exactly 25 entries with JavaScript disabled", async ({
  browser,
}) => {
  const context = await browser.newContext({ javaScriptEnabled: false });
  const page = await context.newPage();
  const response = await page.goto("/");

  expect(response?.status()).toBe(200);
  await expect(page.locator("[data-testid='tracked-player-row']")).toHaveCount(25);
  await expect(page.getByRole("link", { name: "View all →" })).toBeVisible();

  await context.close();
});

test("exact valid tag search uses the canonical player label and route", async ({
  page,
}) => {
  await page.goto("/?q=%232pp");

  await expect(
    page.getByRole("heading", { name: "Exact valid player tag" }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Open the canonical player page" }),
  ).toHaveAttribute("href", "/players/%232PP");
});

test("an unknown but valid tag remains an exact canonical submission", async ({
  page,
}) => {
  await page.goto("/?q=%232P0");

  await expect(
    page.getByRole("heading", { name: "Exact valid player tag" }),
  ).toBeVisible();
  await expect(
    page.getByText("#2P0 is valid but is not in the known fixture cohort."),
  ).toBeVisible();
  await expect(
    page.getByText("valid but is not in the known fixture cohort"),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "Open the canonical player page" }),
  ).toHaveAttribute("href", "/players/%232P0");
});

test("name search labels known matches and keeps duplicate context", async ({ page }) => {
  await page.goto("/?q=Nova");

  await expect(
    page.getByRole("heading", { name: "Known Clash Lens players" }),
  ).toBeVisible();
  await expect(page.getByText("#2PP", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("#2PQ", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("Northwind", { exact: true }).first()).toBeVisible();
});

test("invalid search is a read-only validation state", async ({ page }) => {
  const postRequests: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST") postRequests.push(request.url());
  });

  await page.goto("/?q=not-a-valid-tag-or-known-name");

  await expect(
    page.getByRole("heading", { name: "No known players found" }),
  ).toBeVisible();
  await expect(page.getByText("No refresh or discovery work was created.")).toBeVisible();
  expect(postRequests).toEqual([]);
});

test("oversized search input is rejected without a private search request", async ({
  page,
}) => {
  const privateRequests: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes(":8010")) privateRequests.push(request.url());
  });

  await page.goto(`/?q=${"N".repeat(10_000)}`);

  await expect(page.getByText("Check the submitted value and try again.")).toBeVisible();
  expect(privateRequests.some((url) => url.includes("/v1/players/search"))).toBe(false);
});

test("home search can be completed with the keyboard", async ({ page }) => {
  await page.goto("/");
  const search = page.getByRole("searchbox", { name: "Search player tags or names" });
  await search.focus();
  await page.keyboard.type("Nova");
  await page.keyboard.press("Enter");

  await expect(
    page.getByRole("heading", { name: "Known Clash Lens players" }),
  ).toBeVisible();
  await expect(page.getByText("#2PP", { exact: true }).first()).toBeVisible();
});

test("tables and controls expose accessible names", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByRole("table", { name: "Live leaderboard" })).toBeVisible();
  await expect(
    page.getByRole("searchbox", { name: "Search player tags or names" }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "Search" })).toBeVisible();
});

test("public data responses are not stored by intermediaries", async ({ request }) => {
  for (const path of ["/", "/players/%232PP", "/leaderboards/tracked"]) {
    const response = await request.get(path);
    expect(response.status()).toBe(200);
    expect(response.headers()["cache-control"]).toBe("no-store");
  }
});
