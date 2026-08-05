import { expect, test } from "@playwright/test";

test("home server-renders the first 25 live players and search controls", async ({
  page,
}) => {
  await page.goto("/");

  await expect(
    page.getByRole("heading", { name: "Tracked Players", exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("link", { name: "Full live leaderboard" })).toBeVisible();
  await expect(
    page.getByRole("searchbox", { name: "Search player tags or names" }),
  ).toBeVisible();
  await expect(page.locator("[data-testid='tracked-player-row']")).toHaveCount(25);
  await expect(page.getByText("Stale", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("Uncertain", { exact: true }).first()).toBeVisible();

  const initialHtml = await page.content();
  expect(initialHtml).toContain("Live tracked players");
});

test("home SSR keeps exactly 25 entries with JavaScript disabled", async ({
  browser,
}) => {
  const context = await browser.newContext({ javaScriptEnabled: false });
  const page = await context.newPage();
  const response = await page.goto("/");

  expect(response?.status()).toBe(200);
  await expect(page.locator("[data-testid='tracked-player-row']")).toHaveCount(25);
  await expect(page.getByText("Stale", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("Uncertain", { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("link", { name: "Full live leaderboard" })).toBeVisible();

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
    if (request.url().includes(":8011")) privateRequests.push(request.url());
  });

  await page.goto(`/?q=${"N".repeat(10_000)}`);

  await expect(page.getByText("Check the submitted value and try again.")).toBeVisible();
  expect(privateRequests.some((url) => url.includes("/v1/players/search"))).toBe(false);
});

test("the rendered quality panel keeps every fixture state visible", async ({ page }) => {
  await page.goto("/");
  const qualityPanel = page.getByRole("region", { name: "Fixture data-quality states" });

  for (const state of [
    "Missing",
    "Partial",
    "Stale",
    "Malformed",
    "Unclassified",
    "Uncertain",
    "Rate-limited",
    "Unavailable",
  ]) {
    await expect(qualityPanel.getByText(state, { exact: true })).toBeVisible();
  }
  await expect(page.getByText("63% measured", { exact: false })).toBeVisible();
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

  await expect(page.getByRole("table", { name: "Live tracked players" })).toBeVisible();
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
