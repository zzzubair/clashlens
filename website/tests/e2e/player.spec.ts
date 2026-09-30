import { expect, test } from "@playwright/test";

test("player page canonicalizes the tag and shows collected profile data", async ({
  page,
}) => {
  await page.goto("/players/%232pp");

  await expect(page).toHaveURL(/\/players\/%232PP$/);
  await expect(
    page.getByRole("heading", { name: "Synthetic Clasher 001" }),
  ).toBeVisible();
  await expect(page.getByText("#2PP", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Daily Legend log", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Current trophies", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Refresh", exact: true })).toBeVisible();
});

test("player page stays within a narrow viewport", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 900 });
  await page.goto("/players/%232PP");

  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    ),
  ).toBe(0);
});

test("season navigation clears refresh state for the same player", async ({ page }) => {
  await page.goto("/players/%232PP");
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  const refresh = page.getByRole("region", { name: "Player refresh" });
  await expect(refresh).toBeVisible();

  const seasons = page.getByRole("navigation", { name: "Historical seasons" });
  await seasons.getByRole("link").first().click();
  await expect(page).toHaveURL(/\/players\/%232PP\?season=/);
  await expect(refresh).toHaveCount(0);

  await seasons.getByRole("link", { name: "Current season" }).click();
  await expect(page).toHaveURL(/\/players\/%232PP$/);
  await expect(refresh).toHaveCount(0);
});

test("unknown tag starts anonymously, shows progress, and enters tracking", async ({
  page,
}) => {
  await page.goto("/?q=%23lqqp");
  await expect(page).toHaveURL(/\/players\/%23LQQP$/);
  await expect(page.getByRole("region", { name: "Player lookup" })).toContainText(
    "Checking this tag",
  );
  await expect(page.getByRole("button", { name: "Start tracking" })).toHaveCount(0);
  await expect(page.getByText("Now tracking in Legend I.", { exact: true })).toBeVisible({
    timeout: 30_000,
  });
  await expect(
    page.getByRole("heading", { name: "Lookup eligible Clasher" }),
  ).toBeVisible();
});

test("a real non-Legend player stays accessible without a full profile or name result", async ({
  page,
}) => {
  await page.goto("/players/%23LQQY");
  await expect(page.getByRole("region", { name: "Player lookup" })).toContainText(
    "not in Legend I",
    { timeout: 30_000 },
  );
  await expect(page.getByText("Current trophies", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Saved Legend history" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Refresh", exact: true })).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole("region", { name: "Player lookup" })).toContainText(
    "not in Legend I",
  );
  await page.goto("/?q=Lookup%20ineligible%20Clasher");
  await expect(page.locator(".search-results")).not.toContainText("#LQQY");
});

test("not-found and uncertain eligibility are different outcomes", async ({ page }) => {
  await page.goto("/players/%23LQQV");
  await expect(page.getByRole("region", { name: "Player lookup" })).toContainText(
    "Player not found",
    { timeout: 30_000 },
  );
  await expect(page.getByRole("link", { name: "Try again", exact: true })).toBeVisible();
  await page.goto("/players/%23LQQG");
  await expect(page.getByRole("region", { name: "Player lookup" })).toContainText(
    "could not confirm",
    { timeout: 30_000 },
  );
  await expect(page.getByText("Current trophies", { exact: true })).toHaveCount(0);
});

test("first lookup works without JavaScript and exposes a temporary failure with retry", async ({
  browser,
}) => {
  const context = await browser.newContext({ javaScriptEnabled: false });
  try {
    const page = await context.newPage();
    await page.goto("/?q=%23LQQJ");
    await expect(page.getByRole("region", { name: "Player lookup" })).toContainText(
      "Checking this tag",
    );
    await expect(async () => {
      await page.reload();
      await expect(page.getByRole("region", { name: "Player lookup" })).toContainText(
        "could not finish checking",
      );
    }).toPass({ timeout: 30_000, intervals: [1000] });
    await expect(
      page.getByRole("link", { name: "Try again", exact: true }),
    ).toBeVisible();
  } finally {
    await context.close();
  }
});
