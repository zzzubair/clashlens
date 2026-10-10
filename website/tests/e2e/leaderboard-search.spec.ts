import { expect, test, type Locator, type Page } from "@playwright/test";

import { worstCasePlayers } from "../fixtures/worst-case-players";

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
  await expect(page.locator('.leaderboard-row[data-selected="true"]')).toBeVisible();
  await page.goBack();
  await expect(page.getByRole("searchbox", { name: "Find your rank" })).toHaveValue(
    "Synthetic",
  );
  await expect(page.locator(".rank-search-results a").first()).toBeVisible();
});

test("empty searches explain that there is no match", async ({ page }) => {
  await page.goto("/leaderboards/tracked?view=live&page=1&q=NoSuchClasherForThisTest");
  await expect(page.getByText(/No players on this board match/)).toBeVisible();
  await expect(page.locator(".rank-search-results a")).toHaveCount(0);
  await expect(page.locator(".leaderboard-row").first()).toBeVisible();
});

test.describe("on a touch phone with worst-case players", () => {
  test.use({ hasTouch: true, isMobile: true });

  async function openBoard(page: Page, view: "live" | "daily") {
    await page.goto(`/leaderboards/tracked?view=${view}&page=1`);
    await expect(page.locator(".leaderboard-row").first()).toBeVisible();
    // Safari before 2026 treats a table row's position as static; act the same here.
    await page.addStyleTag({
      content: ".leaderboard-row { position: static !important; }",
    });
    // Show the longest real names, clans and ranks in the first rows.
    await page.evaluate((players) => {
      const rows = document.querySelectorAll(".leaderboard-row");
      players.forEach((player, index) => {
        const row = rows[index];
        row.querySelector(".rank-mark")!.textContent = player.rank.toLocaleString();
        row.querySelector(".player-name bdi")!.textContent = player.name;
        row.querySelector(".player-tag")!.textContent = player.tag;
        row.querySelector('[data-label="Clan"]')!.textContent = player.clan;
        row.querySelector(".trophy-cell strong")!.textContent =
          player.trophies.toLocaleString();
      });
    }, worstCasePlayers);
  }

  // A finger on the middle of the element, whatever is drawn on top of it.
  async function tapMiddle(page: Page, element: Locator) {
    await element.evaluate((node) => node.scrollIntoView({ block: "center" }));
    const box = (await element.boundingBox())!;
    await page.touchscreen.tap(box.x + box.width / 2, box.y + box.height / 2);
  }

  for (const viewport of [
    { width: 320, height: 568 },
    { width: 844, height: 390 },
  ]) {
    test(`controls and rows keep their own taps at ${viewport.width}x${viewport.height}`, async ({
      page,
    }, testInfo) => {
      await page.setViewportSize(viewport);
      await openBoard(page, "live");
      const table = page.getByRole("region", { name: "Live leaderboard table" });
      expect(
        await table.evaluate((node) => node.scrollWidth - node.clientWidth),
      ).toBeLessThanOrEqual(1);
      await expect(
        page
          .locator(".rank-mark")
          .filter({ hasText: worstCasePlayers[5].rank.toLocaleString() }),
      ).toBeVisible();
      await table.scrollIntoViewIfNeeded();
      await testInfo.attach(`worst-case-${viewport.width}x${viewport.height}`, {
        body: await page.screenshot(),
        contentType: "image/png",
      });

      const row = page.locator(".leaderboard-row").nth(2);
      const rowPlayer = await row.locator(".player-name").getAttribute("href");
      await tapMiddle(page, row.locator(".trophy-cell"));
      await expect(page).toHaveURL(new RegExp(`${rowPlayer}$`));

      await openBoard(page, "live");
      await page.getByRole("searchbox", { name: "Find your rank" }).fill("Synthetic");
      await tapMiddle(page, page.getByRole("button", { name: "Search", exact: true }));
      await expect(page).toHaveURL(/[?&]q=Synthetic/);

      await openBoard(page, "live");
      await tapMiddle(page, page.getByRole("link", { name: "Daily", exact: true }));
      await expect(page).toHaveURL(/view=daily/);
    });
  }
});
