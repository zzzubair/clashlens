import { expect, test, type Page } from "@playwright/test";

import { ensureAccount, signIn } from "./helpers/account";

// The trophy count and the buttons beside it must never cover each other on a
// phone, signed out (Refresh) or signed in (Refresh and Add to watchlist), with
// a usual 4-digit count or a 5-digit one.
async function expectTrophiesClear(page: Page, buttons: number) {
  const count = page.locator(".player-trophy-count");
  await expect(page.locator(".player-actions").locator("button, a")).toHaveCount(buttons);
  for (const width of [360, 390, 430]) {
    await page.setViewportSize({ width, height: 844 });
    for (const trophies of ["5,107", "10,000"]) {
      // Only the number's width matters; the fake players stay below 10,000.
      const problems = await count.evaluate((element, text) => {
        element.lastChild!.textContent = text;
        const found: string[] = [];
        const number = element.getBoundingClientRect();
        for (const button of document.querySelectorAll(
          ".player-actions :is(button, a)",
        )) {
          const box = button.getBoundingClientRect();
          if (
            box.left < number.right &&
            number.left < box.right &&
            box.top < number.bottom &&
            number.top < box.bottom
          )
            found.push(`${button.textContent?.trim()} covers ${text}`);
        }
        const { scrollWidth, clientWidth } = document.documentElement;
        if (scrollWidth > clientWidth)
          found.push(`page is ${scrollWidth - clientWidth}px too wide`);
        return found;
      }, trophies);
      expect(problems, `at ${width}px`).toEqual([]);
    }
  }
}

test("a player's trophies and buttons never overlap on a phone", async ({ page }) => {
  // Refuse Refresh before it spends either per-visitor allowance.
  await page.route("**/resources/players/*/refresh*", (route) =>
    route.request().method() === "POST"
      ? route.continue({ postData: "idempotencyKey=invalid" })
      : route.continue(),
  );
  await page.goto("/players/%232PP");
  await expectTrophiesClear(page, 1);

  await signIn(page);
  await ensureAccount(page, "trophyfit", "Trophy Fit");
  await page.goto("/players/%232PP");
  await expectTrophiesClear(page, 2);
});
