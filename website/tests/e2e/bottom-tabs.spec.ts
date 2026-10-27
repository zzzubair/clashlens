import { expect, test, type Locator, type Page } from "@playwright/test";

import { ensureAccount, signIn } from "./helpers/account";

const tabs = (page: Page) => page.locator("nav.bottom-tabs");

test("on a phone the bottom bar opens each tab and More, and hides nothing", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  const bar = tabs(page);
  await expect(bar).toBeVisible();
  // The top bar keeps the logo, search, the theme switch and Sign in.
  const header = page.getByRole("banner");
  await expect(header.getByRole("button", { name: "Dark mode" })).toBeVisible();
  await expect(header.getByRole("link", { name: "Sign in", exact: true })).toBeVisible();
  await expect(page.locator("details.site-menu > summary")).toHaveText("More");
  for (const item of await bar.locator(":scope > a, summary").all()) {
    const box = (await item.boundingBox())!;
    expect(Math.min(box.width, box.height)).toBeGreaterThanOrEqual(44);
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBe(390);

  await bar.getByRole("link", { name: "Rankings" }).click();
  await expect(page).toHaveURL(/\/leaderboards\/tracked/);
  await expect(bar.getByRole("link", { name: "Rankings" })).toHaveAttribute(
    "aria-current",
    "page",
  );

  // More holds Watchlists, Blog and Join Discord; the dimmed page closes it.
  const more = page.locator("details.site-menu");
  await more.locator("summary").click();
  await expect(more).toHaveAttribute("open", "");
  const sheet = more.locator(".site-menu-panel");
  const watchlists = sheet.getByRole("link", { name: "Watchlists" });
  await expect(watchlists).toBeVisible();
  // Join Discord is a full row in the sheet's own colours, like Watchlists.
  const discord = sheet.getByRole("link", { name: /^Join Discord/ });
  await expect(discord).toBeVisible();
  await expect(discord).toContainText("Join Discord");
  const row = (await discord.boundingBox())!;
  expect(row.height).toBeGreaterThanOrEqual(44);
  expect(row.width).toBe((await watchlists.boundingBox())!.width);
  const colour = (link: Locator) => link.evaluate((el) => getComputedStyle(el).color);
  expect(await colour(discord)).toBe(await colour(watchlists));
  await page.mouse.click(195, 200);
  await expect(more).not.toHaveAttribute("open");
  await more.locator("summary").click();
  await sheet.getByRole("link", { name: "Blog" }).click();
  await expect(page).toHaveURL(/\/blog$/);
  await expect(more).not.toHaveAttribute("open");
  await expect(more.locator("summary")).toHaveAttribute("aria-current", "page");

  // Scrolled to the end, the footer's last link sits above the bar.
  await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
  const last = (await page.getByRole("link", { name: "Privacy" }).boundingBox())!;
  const top = (await bar.boundingBox())!.y;
  expect(last.y + last.height).toBeLessThanOrEqual(top);
});

test("a tablet held sideways keeps the bottom bar; a mouse keeps the top bar", async ({
  browser,
  baseURL,
  page,
}) => {
  const tablet = await browser.newContext({
    baseURL,
    viewport: { width: 1180, height: 820 },
    hasTouch: true,
    isMobile: true,
  });
  const touch = await tablet.newPage();
  await touch.goto("/");
  await expect(tabs(touch)).toBeVisible();
  await expect(touch.locator("nav.primary-nav")).toBeHidden();
  await tablet.close();

  await page.setViewportSize({ width: 1280, height: 800 });
  await page.goto("/");
  await expect(tabs(page)).toBeHidden();
  await expect(page.locator("nav.primary-nav")).toBeVisible();
});

test("a tapped tab is chosen at once and loads once, however often it is tapped", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/blog");
  const bar = tabs(page);
  const analytics = bar.getByRole("link", { name: "Analytics" });
  const more = page.locator("details.site-menu > summary");
  const filled = (tab: Locator) =>
    tab.evaluate((el) => getComputedStyle(el).backgroundImage !== "none");
  const loading = (tab: Locator) =>
    tab.evaluate((el) => getComputedStyle(el, "::after").content !== "none");
  await expect(more).toHaveAttribute("aria-current", "page");

  // Hold the page's data, as a slow server or phone signal would.
  let loads = 0;
  let release = () => {};
  const held = new Promise<void>((resolve) => (release = resolve));
  await page.route(
    (url) => url.pathname === "/analytics/armies.data",
    async (route) => {
      loads += 1;
      await held;
      await route.continue();
    },
  );
  await analytics.click();
  await expect.poll(() => filled(analytics)).toBe(true);
  expect(await loading(analytics)).toBe(true);
  expect(await filled(more)).toBe(false);
  await analytics.click();
  await expect(page).toHaveURL(/\/blog$/);
  // Ctrl/Cmd and middle clicks are left to the browser, which opens the tab's
  // page in a new tab. Headless Chromium does not open one every time, so
  // this checks the page did not cancel the click.
  await page.evaluate(() => {
    const kept: boolean[] = [];
    Object.assign(window, { kept });
    for (const type of ["click", "auxclick"])
      window.addEventListener(type, (event) => kept.push(!event.defaultPrevented));
  });
  await analytics.click({ modifiers: ["ControlOrMeta"] });
  await analytics.click({ button: "middle" });
  expect(
    await page.evaluate(() => (window as unknown as { kept: boolean[] }).kept),
  ).toEqual([true, true]);
  await expect(page).toHaveURL(/\/blog$/);
  release();

  await expect(page).toHaveURL(/\/analytics\/armies$/);
  await expect(analytics).toHaveAttribute("aria-current", "page");
  expect(await loading(analytics)).toBe(false);
  expect(loads).toBe(1);
});

test("tapping the open tab again shows it loading, also with reduced motion", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.goto("/leaderboards/tracked?view=daily");
  const rankings = tabs(page).getByRole("link", { name: "Rankings" });
  await expect(rankings).toHaveAttribute("aria-current", "page");
  const loading = () =>
    rankings.evaluate((el) => getComputedStyle(el, "::after").content !== "none");
  expect(await loading()).toBe(false);

  let release = () => {};
  const held = new Promise<void>((resolve) => (release = resolve));
  await page.route(
    (url) => url.pathname === "/leaderboards/tracked.data",
    async (route) => {
      await held;
      await route.continue();
    },
  );
  await rankings.click();
  await expect.poll(loading).toBe(true);
  release();
  await expect(page).toHaveURL(/view=live/);
  await expect.poll(loading).toBe(false);
});

test("signed in on a phone, every bottom tab keeps the account in the top bar", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await signIn(page);
  await ensureAccount(page, "lensscout", "Lens Scout");
  await page.goto("/");
  const header = page.getByRole("banner");
  const name = header.locator(".nav-account-name");
  await expect(name).toBeVisible();
  const shown = (await name.textContent())!;
  const bar = tabs(page);
  const more = page.locator("details.site-menu");
  const stillSignedIn = async (path: RegExp) => {
    await expect(page).toHaveURL(path);
    await expect(header.getByRole("link", { name: "Sign in", exact: true })).toHaveCount(
      0,
    );
    await expect(name).toHaveText(shown);
    await expect(bar.locator(":scope > a").first()).toHaveText("Dashboard");
  };
  for (const [tab, path] of [
    ["Rankings", /\/leaderboards\/tracked\?view=live/],
    ["Dashboard", /\/$/],
    ["Analytics", /\/analytics\/armies$/],
    ["Crews", /\/crews$/],
  ] as const) {
    await bar.getByRole("link", { name: tab }).click();
    await stillSignedIn(path);
  }
  for (const [item, path] of [
    ["Watchlists", /\/account\/watchlists$/],
    ["Blog", /\/blog$/],
  ] as const) {
    await more.locator("summary").click();
    await more.getByRole("link", { name: item }).click();
    await stillSignedIn(path);
  }
});
