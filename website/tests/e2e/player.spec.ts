import { expect, test as base } from "@playwright/test";

const test = base.extend({
  page: async ({ page }, use, testInfo) => {
    const identity = `playwright-${testInfo.testId.replace(/[^A-Za-z0-9_-]/g, "-")}`;
    await page.setExtraHTTPHeaders({ "x-forwarded-for": identity });
    await use(page);
  },
});

test("player page redirects to the uppercase canonical tag and shows player data", async ({
  page,
}) => {
  await page.goto("/players/%232pp");

  await expect(page).toHaveURL(/\/players\/%232PP$/);
  await expect(page.getByRole("heading", { name: "Nova" })).toBeVisible();
  await expect(page.getByText("Current Legend day")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Offense" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Defense" })).toBeVisible();
  await expect(page.getByText("Trophies", { exact: true })).toBeVisible();
  await expect(page.getByText("Last updated", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Refresh", exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Legend season" })).toBeVisible();
  await expect(page.getByRole("columnheader").allTextContents()).resolves.toEqual([
    "Day",
    "Offense",
    "Defense",
    "Trophy change",
  ]);
  await expect(page.getByText("Saved profile data")).toHaveCount(0);
  await expect(page.getByText("Completeness and uncertainty")).toHaveCount(0);
  await expect(page.getByText("Data quality")).toHaveCount(0);
  await expect(page.getByText("Trust and provenance")).toHaveCount(0);
});

test("canonical redirects preserve refresh query state", async ({ page }) => {
  await page.goto("/players/%232pp?refresh=refresh-2pp-1");

  await expect(page).toHaveURL(/\/players\/%232PP\?refresh=refresh-2pp-1$/);
});

test("refresh shows progress and publishes newer saved player data", async ({ page }) => {
  const pythonRequests: string[] = [];
  const refreshSubmissions: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes(":8011")) pythonRequests.push(request.url());
    if (request.method() === "POST") {
      refreshSubmissions.push(new URL(request.url()).pathname);
    }
  });

  await page.goto("/players/%232PQ");
  await expect(page.locator(".player-trophy-card strong")).toHaveText("7,197");
  await page.getByRole("button", { name: "Refresh", exact: true }).click();

  await expect(page.getByText("Refreshing…", { exact: true })).toBeVisible();
  await expect(page.getByRole("progressbar", { name: "Refresh progress" })).toBeVisible();
  await expect(page.getByTestId("refresh-work-id")).toBeVisible();
  await expect(page.getByText("Updated.", { exact: true })).toBeVisible({
    timeout: 5_000,
  });
  await expect(page.locator(".player-trophy-card strong")).toHaveText("7,201");
  expect(refreshSubmissions.map((path) => path.replace(/\.data$/, ""))).toEqual([
    "/resources/players/%232PQ/refresh",
  ]);
  expect(pythonRequests).toEqual([]);
});

test("concurrent refreshes reuse one work identity", async ({ browser }) => {
  const context = await browser.newContext({
    extraHTTPHeaders: { "x-forwarded-for": "playwright-concurrent-refresh" },
  });
  const first = await context.newPage();
  const second = await context.newPage();
  await Promise.all([first.goto("/players/%232P8"), second.goto("/players/%232P8")]);

  await Promise.all([
    first.getByRole("button", { name: "Refresh", exact: true }).click(),
    second.getByRole("button", { name: "Refresh", exact: true }).click(),
  ]);
  const firstWorkId = await first.getByTestId("refresh-work-id").textContent();
  const secondWorkId = await second.getByTestId("refresh-work-id").textContent();

  expect(firstWorkId).toBeTruthy();
  expect(secondWorkId).toBe(firstWorkId);
  await context.close();
});

test("refresh status is bound to the player route", async ({ page }) => {
  await page.goto("/players/%232PG");
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  const workId = (await page.getByTestId("refresh-work-id").textContent())?.replace(
    "Work ID: ",
    "",
  );

  expect(workId).toBeTruthy();
  const response = await page.request.get(
    `/resources/players/%232PQ/refresh?workId=${encodeURIComponent(workId ?? "")}`,
  );

  expect(response.status()).toBe(409);
  await expect(response.json()).resolves.toEqual({
    error: {
      code: "conflict",
      message: "The request conflicts with current saved data.",
    },
  });
});

test("an unavailable refresh keeps saved profile data visible", async ({ page }) => {
  await page.goto("/players/%232PV");
  await expect(page.getByRole("heading", { name: "Lumen" })).toBeVisible();
  await expect(page.getByText("7,061", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: "Refresh", exact: true }).click();

  await expect(
    page.getByText("Saved data is still available, but the live service is unavailable."),
  ).toBeVisible();
  await expect(page.getByText("Current Legend day")).toBeVisible();
  await expect(page.getByText("7,061", { exact: true })).toBeVisible();
});

test("a refresh status error is visible and stops polling", async ({ page }) => {
  let statusRequests = 0;
  page.on("request", (request) => {
    if (
      request.method() === "GET" &&
      request.url().includes("/resources/players/%232PY/refresh")
    ) {
      statusRequests += 1;
    }
  });

  await page.goto("/players/%232PY");
  await page.getByRole("button", { name: "Refresh", exact: true }).click();

  await expect(
    page.getByText(
      "Saved data is still available, but the live service is unavailable.",
      {
        exact: true,
      },
    ),
  ).toBeVisible();
  expect(statusRequests).toBeGreaterThan(0);
  const requestsAfterError = statusRequests;
  await page.waitForTimeout(1_200);
  expect(statusRequests).toBe(requestsAfterError);
});

test("refresh can be started from the keyboard", async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(Crypto.prototype, "randomUUID", {
      configurable: true,
      value: undefined,
    });
  });
  await page.goto("/players/%232PL");
  const button = page.getByRole("button", { name: "Refresh", exact: true });
  await button.focus();
  await page.keyboard.press("Enter");

  await expect(page.getByText("Refreshing…", { exact: true })).toBeVisible();
});

test("cross-origin refresh submissions are rejected", async ({ request }) => {
  const response = await request.post("/resources/players/%232PP/refresh", {
    headers: { Origin: "https://evil.example" },
    form: { idempotencyKey: "01890f47-c734-7cc2-9abf-9ce6c64f90c1" },
  });

  expect(response.status()).toBe(403);
  await expect(response.json()).resolves.toEqual({
    error: {
      code: "forbidden",
      message: "This action is not allowed.",
    },
  });
});
