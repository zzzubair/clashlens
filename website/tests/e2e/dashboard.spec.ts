import { expect, test, type Page, type Route } from "@playwright/test";

import {
  ensureAccount,
  expectNoPageErrors,
  expectNoSeriousAccessibilityViolations,
  signIn,
  trackPageErrors,
} from "./helpers/account";

// A fake Clash API player in Legend League that no other browser test links.
const TAG = "#Q00VJ";

const ALL_CARDS = [
  "Legend day",
  "Legend clock",
  "Bases you attacked",
  "Goal",
  "Armies on defense",
  "Rivals watch",
  "Season heat map",
  "Cutoffs",
  "Season graph",
  "Recent form",
  "Offense consistency",
  "Group watch",
  "Armies on offense",
];
const DEFAULT_CARDS = ALL_CARDS.slice(0, 7);

/** The card titles on the page, in order. */
const cardOrder = (page: Page) =>
  page
    .locator(".dash-grid > [data-card]")
    .evaluateAll((cards) => cards.map((card) => card.getAttribute("aria-label")));

test("a Clasher builds a dashboard, keeps it after a reload, and reads it on a phone", async ({
  page,
}) => {
  test.setTimeout(150_000);
  const errors = trackPageErrors(page);
  await page.route("**/authorize?*", async (route) => {
    const url = new URL(route.request().url());
    url.searchParams.set("login_hint", "fixture-google-subject-3201");
    await route.continue({ url: url.href });
  });
  await signIn(page);
  await ensureAccount(page, "lensboard", "Lens Board");

  // The game's verify call has a small shared budget a second, so try again.
  await page.goto("/account/verify-player");
  await expect(async () => {
    await page.getByLabel("Player tag").fill(TAG);
    await page.getByLabel("API token", { exact: true }).fill(`VERIFY-${TAG.slice(1)}`);
    await page.getByRole("button", { name: "Link player" }).click();
    await expect(page).toHaveURL(/\?linked=/, { timeout: 3_000 });
  }).toPass({ intervals: [1_000, 2_000], timeout: 30_000 });

  // A first visit offers our layout or an empty one, once the player is tracked.
  const welcome = page.getByRole("dialog", { name: "Your dashboard" });
  await expect(async () => {
    await page.goto("/dashboard");
    await expect(welcome).toBeVisible({ timeout: 3_000 });
  }).toPass({ intervals: [2_000], timeout: 60_000 });
  await welcome.getByRole("button", { name: /Use our layout/ }).click();
  await expect(welcome).toBeHidden();
  await expect.poll(() => cardOrder(page)).toEqual(DEFAULT_CARDS);

  // Customise: move, remove and add; Done saves it as the user's own layout.
  // Customise opens once the first pick has saved.
  const customise = page.getByRole("button", { name: "Customise dashboard" });
  await expect(customise).toBeEnabled();
  await customise.click();
  const bar = page.getByRole("region", { name: "Customise dashboard" });
  await expect(bar.getByText("Default layout")).toBeVisible();
  await page.getByRole("button", { name: "Move Goal earlier" }).click();
  await page.getByRole("button", { name: "Remove Season heat map" }).click();
  await expect(bar.getByText("Your own layout")).toBeVisible();
  await bar.getByRole("button", { name: "Add a card" }).click();
  const sheet = page.getByRole("dialog", { name: "Add a card" });
  for (const card of [...ALL_CARDS.slice(7), "Season heat map"])
    await sheet.getByRole("button", { name: `Add ${card}`, exact: true }).click();
  await expect(sheet.getByText("Every card is already on your dashboard.")).toBeVisible();
  await sheet.getByRole("button", { name: "Done" }).click();
  await bar.getByRole("button", { name: "Done" }).click();
  await expect(bar).toBeHidden();
  const own = [
    "Legend day",
    "Legend clock",
    "Goal",
    "Bases you attacked",
    "Armies on defense",
    "Rivals watch",
    ...ALL_CARDS.slice(7),
    "Season heat map",
  ];
  await expect.poll(() => cardOrder(page)).toEqual(own);
  await page.reload();
  await expect.poll(() => cardOrder(page)).toEqual(own);
  // Every card reads from the real stack without failing.
  for (const card of ALL_CARDS) {
    const region = page.getByRole("region", { name: card, exact: true });
    await expect(region).toBeVisible();
    await expect(region).not.toContainText("could not be loaded");
  }

  // An army card's day switch reads the chosen days without leaving the page,
  // keeping the numbers already shown until they arrive.
  const armies = page.getByRole("region", { name: "Armies on defense", exact: true });
  const shown = await armies.evaluateHandle((card) => {
    const texts: string[] = [];
    new MutationObserver(() => texts.push(card.textContent ?? "")).observe(card, {
      childList: true,
      characterData: true,
      subtree: true,
    });
    return texts;
  });
  await armies.getByRole("button", { name: "3 days" }).click();
  await expect(armies.getByRole("button", { name: "3 days" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  await expect(armies.locator("[aria-busy]")).toHaveAttribute("aria-busy", "false");
  await expect(armies).not.toContainText("aren't available");
  expect(
    (await shown.jsonValue()).filter((text) => text.includes("aren't available")),
  ).toEqual([]);

  // A day switch whose read fails on the way, a dropped connection or a 502
  // from the server in front of the website, says so in that card alone.
  const armiesRead = /\/resources\/players\/[^/]+\/armies\?/;
  const logged = errors.length;
  for (const fail of [
    (route: Route) => route.abort(),
    (route: Route) => route.fulfill({ status: 502, body: "Bad Gateway" }),
  ]) {
    await page.route(armiesRead, fail);
    await armies.getByRole("button", { name: "Custom" }).click();
    await armies.getByLabel("Number of days").fill("5");
    await armies.getByRole("button", { name: "Show" }).click();
    await expect(armies).toContainText("Army numbers aren't available right now.");
    await expect.poll(() => cardOrder(page)).toEqual(own);
    await page.unroute(armiesRead);
    await armies.getByRole("button", { name: "3 days" }).click();
    await expect(armies.locator("[aria-busy]")).toHaveAttribute("aria-busy", "false");
    await expect(armies).not.toContainText("aren't available");
  }
  // The browser logs the two failed reads; nothing else went wrong.
  await expect
    .poll(() => errors.slice(logged))
    .toEqual([
      expect.stringContaining("net::ERR_FAILED"),
      expect.stringContaining("502"),
    ]);
  errors.splice(logged);

  // A card setting saves at once: the Goal becomes Top 50.
  await page.getByRole("button", { name: "Customise dashboard" }).click();
  await page.getByRole("button", { name: "Goal settings" }).click();
  const settings = page.getByRole("dialog", { name: "Goal settings" });
  await settings.getByRole("spinbutton").fill("50");
  await settings.getByRole("button", { name: "Save" }).click();
  await expect(settings).toBeHidden();
  await expect(page.getByRole("region", { name: "Goal" })).toContainText("Top 50");

  // Reset to default puts the default layout back, and it stays after a reload.
  await bar.getByRole("button", { name: "Reset to default" }).click();
  await expect(bar.getByText("Default layout", { exact: true })).toBeVisible();
  await bar.getByRole("button", { name: "Done" }).click();
  await expect(bar).toBeHidden();
  await page.reload();
  await expect.poll(() => cardOrder(page)).toEqual(DEFAULT_CARDS);
  await expect(page.getByRole("region", { name: "Goal" })).toContainText("Top 50");
  await expectNoSeriousAccessibilityViolations(page);

  // On a phone the cards stack full width, with nothing wider than the screen.
  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload();
  await expect.poll(() => cardOrder(page)).toEqual(DEFAULT_CARDS);
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);
  const boxes = await page
    .locator(".dash-grid > [data-card]")
    .evaluateAll((cards) => cards.map((card) => card.getBoundingClientRect().toJSON()));
  for (const [index, box] of boxes.entries()) {
    expect(box.width).toBe(boxes[0]!.width);
    if (index > 0) expect(box.top).toBeGreaterThan(boxes[index - 1]!.bottom);
  }
  await page.getByRole("button", { name: "Customise dashboard" }).click();
  await expect(bar).toBeInViewport();
  await bar.getByRole("button", { name: "Done" }).click();

  expectNoPageErrors(errors);
});
