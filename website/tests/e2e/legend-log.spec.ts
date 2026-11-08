import { expect, test, type Page } from "@playwright/test";

import { openWorstCasePlayer, refuseRefreshes } from "./helpers/worst-case-player";

// Each visible run of text in a Legend log row, and every two that cross.
// Labels kept for screen readers sit in 1px boxes and are skipped.
function overlappingText(page: Page) {
  return page.locator(".legend-log").evaluate((log) => {
    const found: string[] = [];
    for (const row of log.querySelectorAll<HTMLElement>("summary.log-row")) {
      if (!row.checkVisibility()) continue;
      const runs: Array<{ text: string; box: DOMRect }> = [];
      const walker = document.createTreeWalker(row, NodeFilter.SHOW_TEXT);
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const text = node.textContent!.trim();
        let hidden = !text || !node.parentElement!.checkVisibility();
        for (let box = node.parentElement; box && box !== row; box = box.parentElement) {
          const { width, height } = box.getBoundingClientRect();
          if (width <= 1 && height <= 1) hidden = true;
        }
        if (hidden) continue;
        const range = document.createRange();
        range.selectNodeContents(node);
        runs.push({ text, box: range.getBoundingClientRect() });
      }
      runs.forEach((a, index) => {
        for (const b of runs.slice(index + 1)) {
          const across =
            Math.min(a.box.right, b.box.right) - Math.max(a.box.left, b.box.left);
          const down =
            Math.min(a.box.bottom, b.box.bottom) - Math.max(a.box.top, b.box.top);
          if (across > 1 && down > 1)
            found.push(
              `${row.querySelector("strong")?.textContent}: "${a.text}" over "${b.text}"`,
            );
        }
      });
    }
    return found;
  });
}

test("no two numbers in a Legend log row overlap, from a small phone to a wide screen", async ({
  page,
}) => {
  await refuseRefreshes(page);
  // The longest Reset ranks and moves a row can hold: a podium plate, then a
  // five-figure fall and rise beside the day's change.
  await openWorstCasePlayer(page, undefined, (player) => {
    const ranks = new Map([
      [25, 1],
      [26, 99_999],
      [27, 10_000],
    ]);
    for (const day of player.seasonDays)
      day.resetRank = day.dayNumber === null ? null : ranks.get(day.dayNumber);
  });
  const log = page.getByRole("region", { name: "Daily Legend log" });
  await expect(log.getByText("99,999", { exact: true })).toBeVisible();
  await expect(log.locator(".log-rank-move", { hasText: "99,998" })).toBeVisible();
  // Phones upright and on their side, iPads both ways, and desktops.
  for (const [width, height] of [
    [320, 568],
    [390, 844],
    [844, 390],
    [932, 430],
    [768, 1024],
    [820, 1180],
    [1024, 768],
    [1180, 820],
    [1280, 900],
    [1920, 1080],
  ]) {
    await page.setViewportSize({ width, height });
    expect(await overlappingText(page), `at ${width}x${height}`).toEqual([]);
  }
});
