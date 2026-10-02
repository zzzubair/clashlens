import { expect, test } from "@playwright/test";

test("name search opens the player in the board with neighbors at 375px", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto("/leaderboards/tracked?view=live&page=1");
  await page
    .getByRole("searchbox", { name: "Find your rank" })
    .fill("Synthetic Clasher 001");
  await page.getByRole("button", { name: "Search", exact: true }).click();
  const match = page.locator(".rank-search-results a").filter({ hasText: "#2PP" });
  await expect(match).toContainText(/Rank [\d,]+/);
  await expect(match).toContainText(/trophies/);
  const rank = Number(
    (await match.innerText()).match(/Rank ([\d,]+)/)![1].replaceAll(",", ""),
  );
  await testInfo.attach("name-search-375px", {
    body: await page.screenshot(),
    contentType: "image/png",
  });
  await match.click();
  const selected = page.locator('.leaderboard-row[data-selected="true"]');
  await expect(selected).toContainText("#2PP");
  await expect(selected.locator(".rank-mark")).toHaveText(String(rank));
  await expect(selected).toBeFocused();
  await expect(selected).toBeInViewport();
  expect(await page.locator(".leaderboard-row").count()).toBeGreaterThan(1);
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth),
  ).toBe(true);
  await testInfo.attach("selected-rank-375px", {
    body: await page.screenshot(),
    contentType: "image/png",
  });
  await page.reload();
  await expect(selected).toBeInViewport();
});

test("only hash tags jump directly; back restores search", async ({ page }) => {
  await page.goto("/leaderboards/tracked?view=live&page=1&q=2pp");
  await expect(page).not.toHaveURL(/player=/);
  await expect(
    page.locator(".rank-search-results a").filter({ hasText: "#2PP" }),
  ).toBeVisible();
  for (const tag of ["#2pp", "#2PP"]) {
    await page.goto("/leaderboards/tracked?view=live&page=1");
    await page.getByRole("searchbox", { name: "Find your rank" }).fill(tag);
    await page.getByRole("button", { name: "Search", exact: true }).click();
    await expect(page).toHaveURL(/player=%232PP/);
    await expect(page.locator('.leaderboard-row[data-selected="true"]')).toContainText(
      "#2PP",
    );
    await expect(page.locator(".rank-search-results")).toHaveCount(0);
  }
  await page.goto("/leaderboards/tracked?view=live&page=1&q=Synthetic");
  await page.locator(".rank-search-results a").first().click();
  await page.goBack();
  await expect(page.getByRole("searchbox", { name: "Find your rank" })).toHaveValue(
    "Synthetic",
  );
  await expect(page.locator(".rank-search-results a").first()).toBeVisible();
});

test("empty searches explain that there is no match", async ({ page }) => {
  await page.goto("/leaderboards/tracked?view=live&page=1&q=NoSuchClasherForThisTest");
  await expect(page.getByText(/No tracked players matching/)).toBeVisible();
  await expect(page.locator(".rank-search-results a")).toHaveCount(0);
  await expect(page.locator(".leaderboard-row").first()).toBeVisible();
});
