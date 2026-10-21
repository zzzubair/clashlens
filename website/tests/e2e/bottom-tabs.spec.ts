import { expect, test, type Page } from "@playwright/test";

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
  await expect(sheet.getByRole("link", { name: "Watchlists" })).toBeVisible();
  await expect(sheet.getByRole("link", { name: /^Join Discord/ })).toBeVisible();
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
