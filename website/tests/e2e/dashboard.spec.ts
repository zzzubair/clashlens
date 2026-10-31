import { expect, test, type Page, type Route } from "@playwright/test";

import {
  ensureAccount,
  expectNoPageErrors,
  linkPlayer,
  expectNoSeriousAccessibilityViolations,
  signIn,
  trackPageErrors,
} from "./helpers/account";

// A fake Clash API player in Legend League that no other browser test links.
const TAG = "#Q00VJ";

// These Clashers browse from their own address, so their links do not share the
// website's six a minute per address with other browser tests.
test.use({ extraHTTPHeaders: { "CF-Connecting-IP": "203.0.113.201" } });

const ALL_CARDS = [
  "Legend day",
  "Legend clock",
  "Bases you attacked",
  "Rivals watch",
  "Season heat map",
  "Goal",
  "Armies on defense",
  "Cutoffs",
  "Season graph",
  "Recent form",
  "Offense consistency",
  "Watchlist",
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

  await page.goto("/account/verify-player");
  await linkPlayer(page, TAG);

  // A first visit offers our layout or an empty one, once the player is tracked.
  const welcome = page.getByRole("dialog", { name: "Your dashboard" });
  await expect(async () => {
    await page.goto("/");
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
  await expect(bar.getByText("Default layout", { exact: true })).toBeVisible();
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
    "Bases you attacked",
    "Rivals watch",
    "Goal",
    "Armies on defense",
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

  // The two offense cards also show the whole Season so far.
  for (const card of ["Armies on offense", "Offense consistency"]) {
    const region = page.getByRole("region", { name: card, exact: true });
    await region.getByRole("button", { name: "Season" }).click();
    await expect(region.getByRole("button", { name: "Season" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await expect(region.locator("[aria-busy=true]")).toHaveCount(0);
    await expect(region).not.toContainText(/aren't available|could not be loaded/);
  }

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

  // Bases you attacked's day button reads an earlier day and comes back to
  // today, without leaving the page. On Day 1 there is no earlier day. Quick
  // clicks read only the day the clicking stops on.
  const bases = page.getByRole("region", { name: "Bases you attacked", exact: true });
  const earlier = bases.getByRole("button", { name: "Earlier Legend day" });
  const later = bases.getByRole("button", { name: "Later Legend day" });
  await expect(bases).toContainText("· today");
  if (await earlier.isEnabled()) {
    const asked: string[] = [];
    page.on("request", (request) => {
      const day = /\/bases\?day=(\d+)/.exec(request.url())?.[1];
      if (day) asked.push(day);
    });
    // Browser time stands still for both clicks, however slow the runner,
    // then moves past the pause before a read.
    await page.clock.install();
    await page.clock.pauseAt(Date.now() + 5_000);
    await earlier.click();
    const twice = await earlier.isEnabled();
    if (twice) await earlier.click();
    await page.clock.runFor(1_000);
    await page.clock.resume();
    await expect(bases).not.toContainText("· today");
    await expect(bases).not.toContainText("Loading Day");
    await expect(bases).not.toContainText("couldn't be read");
    expect(asked).toHaveLength(1);
    await later.click();
    if (twice) await later.click();
    await expect(bases).toContainText("· today");
  }

  // A card setting saves at once: the Goal becomes Top 50.
  await page.getByRole("button", { name: "Customise dashboard" }).click();
  await page.getByRole("button", { name: "Goal settings" }).click();
  const settings = page.getByRole("dialog", { name: "Goal settings" });
  // The Goal takes a rank from 1 to 10,000.
  await settings.getByRole("spinbutton").fill("10001");
  await expect(settings).toContainText("Enter a whole number from 1 to 10,000");
  await expect(settings.getByRole("button", { name: "Save" })).toBeDisabled();
  await settings.getByRole("spinbutton").fill("10000");
  await expect(settings).toContainText("Any rank from 1 to 10,000");
  await expect(settings.getByRole("button", { name: "Save" })).toBeEnabled();
  await settings.getByRole("spinbutton").fill("50");
  await settings.getByRole("button", { name: "Save" }).click();
  await expect(settings).toBeHidden();
  await expect(page.getByRole("region", { name: "Goal" })).toContainText("Top 50");

  // Use default layout puts the default layout back, and it stays after a reload.
  await bar.getByRole("button", { name: "Use default layout" }).click();
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
  // The phone clock starts small; Show full clock opens the dial and back again.
  const clock = page.getByRole("region", { name: "Legend clock" });
  const dial = clock.locator(".legend-clock-face");
  const smallHeight = (await dial.boundingBox())!.height;
  await clock.getByRole("button", { name: "Show full clock" }).click();
  await expect
    .poll(async () => (await dial.boundingBox())!.height)
    .toBeGreaterThan(smallHeight);
  await clock.getByRole("button", { name: "Show small clock" }).click();
  await expect.poll(async () => (await dial.boundingBox())!.height).toBe(smallHeight);
  // A phone keeps Customise in the toolbar's Options menu.
  await page.getByRole("button", { name: "Dashboard options" }).click();
  await page.getByRole("button", { name: "Customise dashboard" }).click();
  await expect(bar).toBeInViewport();
  await bar.getByRole("button", { name: "Done" }).click();

  expectNoPageErrors(errors);
});

/** Sign in as the fixture's Google user `subject`, with an account and these players linked. */
async function signInWithPlayers(
  page: Page,
  subject: string,
  username: string,
  tags: string[],
) {
  await page.route("**/authorize?*", async (route) => {
    const url = new URL(route.request().url());
    url.searchParams.set("login_hint", subject);
    await route.continue({ url: url.href });
  });
  await signIn(page);
  await ensureAccount(page, username, "Lens Switch");
  for (const tag of tags) {
    await page.goto("/account/verify-player");
    await linkPlayer(page, tag);
  }
}

test("a switch shows the chosen player at once, never the last one's numbers, and a save reads only what it changes", async ({
  page,
}) => {
  test.setTimeout(180_000);
  const errors = trackPageErrors(page);
  // Two fake Clash API players in Legend League that no other browser test links.
  const [first, second] = ["#Q00VC", "#Q00VU"] as const;
  await signInWithPlayers(page, "fixture-google-subject-3202", "lensswitch", [
    first,
    second,
  ]);
  const welcome = page.getByRole("dialog", { name: "Your dashboard" });
  await expect(async () => {
    await page.goto(`/?player=${encodeURIComponent(first)}`);
    await expect(welcome).toBeVisible({ timeout: 3_000 });
  }).toPass({ intervals: [2_000], timeout: 60_000 });
  await welcome.getByRole("button", { name: /Use our layout/ }).click();
  await expect.poll(() => cardOrder(page)).toEqual(DEFAULT_CARDS);
  // On a 360px phone two players' tabs scroll in their row, not the page.
  await page.setViewportSize({ width: 360, height: 740 });
  await expect
    .poll(() => page.evaluate(() => document.documentElement.scrollWidth))
    .toBe(360);
  await page.setViewportSize({ width: 1280, height: 720 });

  const tab = (tag: string) =>
    page.locator(`.dash-account[href$="player=${encodeURIComponent(tag)}"]`);
  const name = async (tag: string) =>
    (await tab(tag).locator(".dash-name").textContent())!;
  const legendDay = page.getByRole("region", { name: "Legend day", exact: true });
  await expect(legendDay).toContainText(await name(first));
  // Every change on the page: the selected tab's name and the Legend day card.
  await page.evaluate(() => {
    const shown: [string | null, string][] = [];
    const record = () =>
      shown.push([
        document.querySelector('.dash-account[aria-current="page"] .dash-name')
          ?.textContent ?? null,
        document.querySelector('[data-card="legend-day"]')?.textContent ?? "",
      ]);
    new MutationObserver(record).observe(document.querySelector("main")!, {
      subtree: true,
      childList: true,
      characterData: true,
      attributes: true,
    });
    Object.assign(window, { shown });
  });
  const shownNames = () =>
    page.evaluate(() => (window as unknown as { shown: [string, string][] }).shown);

  // A slow answer: the new tab and empty cards show before it arrives.
  const slow = /\/_\.data/;
  await page.route(slow, async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 1_500));
    await route.fallback();
  });
  await tab(second).click();
  await expect(tab(second)).toHaveAttribute("aria-current", "page", { timeout: 500 });
  await expect(legendDay).toContainText("Loading…", { timeout: 500 });
  await expect(legendDay).toContainText(await name(second));
  // Quick switches back and forth end on the last one picked.
  await tab(first).click();
  await tab(second).click();
  await tab(first).click();
  await expect(tab(first)).toHaveAttribute("aria-current", "page");
  await expect(legendDay).toContainText(await name(first));
  await expect(legendDay).not.toContainText("Loading…");
  await page.unroute(slow);
  // The card never showed one player's name under another's tab.
  for (const [selected, card] of await shownNames())
    if (card.includes("Synthetic Clasher")) expect(card).toContain(selected);

  // Saves answer with the settings as saved: no reload of the page's data.
  const reads: string[] = [];
  page.on("request", (request) => {
    const url = new URL(request.url());
    if (url.pathname.endsWith(".data"))
      reads.push(`${request.method()} ${url.pathname}${url.search}`);
  });
  const kept = page.locator('[data-card="legend-clock"] > :last-child');
  await kept.evaluate((element) => element.setAttribute("data-kept", ""));
  const bar = page.getByRole("region", { name: "Customise dashboard" });

  // Dragging a card by its grip moves it, as it always has; both cards in view.
  await page.setViewportSize({ width: 1280, height: 2000 });
  await page.getByRole("button", { name: "Customise dashboard" }).click();
  const grip = await page
    .getByRole("button", { name: "Drag to move Goal" })
    .boundingBox();
  const onto = await page.locator('[data-card="legend-day"]').boundingBox();
  await page.mouse.move(grip!.x + grip!.width / 2, grip!.y + grip!.height / 2);
  await page.mouse.down();
  await page.mouse.move(onto!.x + 5, onto!.y + 5, { steps: 10 });
  await page.mouse.up();
  await expect(bar.getByRole("status")).toHaveText("Moved Goal");
  const moved = ["Goal", ...DEFAULT_CARDS.filter((card) => card !== "Goal")];
  await expect.poll(() => cardOrder(page)).toEqual(moved);
  await bar.getByRole("button", { name: "Done" }).click();
  await expect(bar).toBeHidden();
  await expect.poll(() => cardOrder(page)).toEqual(moved);
  // Cards already shown stay as they were, without being read again.
  await expect(kept).toHaveAttribute("data-kept", "");
  expect(reads.filter((read) => read.startsWith("GET"))).toEqual([]);

  // Rating bases by held share changes the card in place.
  await page.getByRole("button", { name: "Customise dashboard" }).click();
  await page.getByRole("button", { name: "Bases you attacked settings" }).click();
  const bases = page.getByRole("dialog", { name: "Bases you attacked settings" });
  await bases.getByRole("button", { name: "Held %" }).click();
  await bases.getByRole("button", { name: "Save" }).click();
  await expect(bases).toBeHidden();
  expect(reads.filter((read) => read.startsWith("GET"))).toEqual([]);

  // A new Goal reads the Goal card alone.
  await page.getByRole("button", { name: "Goal settings" }).click();
  const goal = page.getByRole("dialog", { name: "Goal settings" });
  await goal.getByRole("spinbutton").fill("50");
  await goal.getByRole("button", { name: "Save" }).click();
  await expect(goal).toBeHidden();
  await expect(page.getByRole("region", { name: "Goal" })).toContainText("Top 50");
  const gets = reads.filter((read) => read.startsWith("GET"));
  expect(gets).toHaveLength(1);
  expect(gets[0]).toContain(`player=${encodeURIComponent(first)}`);
  expect(gets[0]).toContain("cards=goal");
  await expect(kept).toHaveAttribute("data-kept", "");

  // A slow Goal read never holds up the next save: Bases closes on its answer.
  const slowGoal = async (route: Route) => {
    const request = route.request();
    if (request.method() === "GET" && new URL(request.url()).searchParams.get("cards"))
      await new Promise((resolve) => setTimeout(resolve, 4_000));
    await route.fallback();
  };
  await page.route(slow, slowGoal);
  await page.getByRole("button", { name: "Goal settings" }).click();
  await goal.getByRole("spinbutton").fill("40");
  await goal.getByRole("button", { name: "Save" }).click();
  await expect(goal).toBeHidden();
  await page.getByRole("button", { name: "Bases you attacked settings" }).click();
  await bases.getByRole("button", { name: "Trophies" }).click();
  await bases.getByRole("button", { name: "Save" }).click();
  await expect(bases).toBeHidden({ timeout: 1_500 });
  await expect(page.getByRole("region", { name: "Goal" })).toContainText("Top 40", {
    timeout: 10_000,
  });
  await page.unroute(slow, slowGoal);

  // A save that lands after a switch shows on the new player's dashboard.
  const slowSave = async (route: Route) => {
    if (route.request().method() === "POST")
      await new Promise((resolve) => setTimeout(resolve, 2_000));
    await route.fallback();
  };
  await page.route(slow, slowSave);
  await page.getByRole("button", { name: "Move Goal later" }).click();
  await bar.getByRole("button", { name: "Done" }).click();
  await tab(second).click();
  await expect(tab(second)).toHaveAttribute("aria-current", "page");
  const later = ["Legend day", "Goal", ...moved.slice(2)];
  await expect.poll(() => cardOrder(page)).toEqual(later);
  await expect(legendDay).toContainText(await name(second));
  await expect(legendDay).not.toContainText("Loading…");
  await page.unroute(slow, slowSave);

  // A card read again that comes back for another player is never shown.
  const otherPlayer = async (route: Route) => {
    const url = new URL(route.request().url());
    if (route.request().method() !== "GET" || !url.searchParams.get("cards"))
      return route.fallback();
    url.searchParams.set("player", first);
    await route.continue({ url: url.href });
  };
  await page.route(slow, otherPlayer);
  await page.getByRole("button", { name: "Customise dashboard" }).click();
  await page.getByRole("button", { name: "Goal settings" }).click();
  await goal.getByRole("spinbutton").fill("30");
  await goal.getByRole("button", { name: "Save" }).click();
  await expect(goal).toBeHidden();
  await expect(page.getByRole("region", { name: "Goal" })).toContainText(
    "This card could not be loaded right now.",
  );
  await page.unroute(slow, otherPlayer);
  await bar.getByRole("button", { name: "Done" }).click();

  // Everything saved is still there after a reload.
  await page.reload();
  await expect.poll(() => cardOrder(page)).toEqual(later);
  await expect(page.getByRole("region", { name: "Goal" })).toContainText("Top 30");
  expectNoPageErrors(errors);
});
