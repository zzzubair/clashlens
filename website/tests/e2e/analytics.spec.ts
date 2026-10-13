import { gunzipSync } from "node:zlib";

import { expect, test, type Page } from "@playwright/test";

import {
  analyticsFixtureOrigin,
  analyticsFixtureToken,
  blogFixtureOrigin,
} from "../fixtures/test-values";

// The website's fixed PostHog EU host; the tests answer for it in the browser.
const posthogHost = "https://eu.i.posthog.com";
const storedId = `ph_${analyticsFixtureToken}_posthog`;

// PostHog drops events from browsers that call themselves headless.
test.use({
  userAgent:
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
});

interface SentEvent {
  event: string;
  properties: Record<string, unknown>;
}

function decode(body: Buffer): SentEvent[] {
  let text = body.toString("utf8");
  if (body[0] === 0x1f && body[1] === 0x8b) text = gunzipSync(body).toString("utf8");
  else if (text.startsWith("data=")) {
    text = Buffer.from(decodeURIComponent(text.slice(5)), "base64").toString("utf8");
  }
  const parsed = JSON.parse(text) as SentEvent | SentEvent[] | { batch: SentEvent[] };
  return Array.isArray(parsed) ? parsed : "batch" in parsed ? parsed.batch : [parsed];
}

/** Answers for PostHog in the browser and keeps every raw body it was sent. */
async function fakePostHog(page: Page) {
  const bodies: string[] = [];
  const events: SentEvent[] = [];
  // PostHog also drops events from browsers driven by test tools.
  await page.addInitScript(() => {
    Object.defineProperty(Navigator.prototype, "webdriver", { get: () => false });
    Object.defineProperty(Navigator.prototype, "userAgentData", { get: () => undefined });
  });
  await page.route(`${posthogHost}/**`, async (route) => {
    const body = route.request().postDataBuffer();
    if (body) {
      const sent = decode(body);
      events.push(...sent);
      bodies.push(JSON.stringify(sent));
    }
    await route.fulfill({ status: 200, contentType: "application/json", body: "{}" });
  });
  return { bodies, events };
}

async function storedKeys(page: Page) {
  return page.evaluate(() => Object.keys(localStorage).sort());
}

test("with no project token no analytics code loads and nothing is sent", async ({
  page,
  context,
}) => {
  const requests: string[] = [];
  page.on("request", (request) => requests.push(request.url()));
  await page.goto(`${blogFixtureOrigin}/privacy`);
  await expect(page.getByText("Visitor counting is not switched on")).toBeVisible();
  await expect(page.getByText("Clash Lens, run by Zubair Shaik. Contact:")).toBeVisible();
  await expect(
    page.getByRole("main").getByRole("link", { name: "privacy@clashlens.net" }).first(),
  ).toHaveAttribute("href", "mailto:privacy@clashlens.net");
  await expect(page.getByRole("switch")).toHaveCount(0);
  await page.getByRole("contentinfo").getByRole("link", { name: "Blog" }).click();
  await expect(page.getByRole("heading", { name: "Blog", level: 1 })).toBeVisible();

  expect(requests.filter((url) => !url.startsWith(`${blogFixtureOrigin}/`))).toEqual([]);
  expect(requests.filter((url) => /posthog|module\.slim/i.test(url))).toEqual([]);
  expect(await storedKeys(page)).toEqual([]);
  expect(await context.cookies()).toEqual([]);
});

test("counts pages without player tags and keeps the browser ID until switched off", async ({
  page,
  context,
}) => {
  const posthog = await fakePostHog(page);
  const outside: string[] = [];
  page.on("request", (request) => {
    const url = request.url();
    if (!url.startsWith(`${analyticsFixtureOrigin}/`) && !url.startsWith(posthogHost)) {
      outside.push(url);
    }
  });
  const sent = (name: string) => posthog.events.filter(({ event }) => event === name);

  await page.goto(`${analyticsFixtureOrigin}/players/%232PP?season=2026-09`);
  await expect.poll(() => sent("$pageview").length).toBe(1);
  const [player] = sent("$pageview");
  expect(player.properties).toMatchObject({
    route: "player",
    signed_in: false,
    $current_url: `${analyticsFixtureOrigin}/players/:tag`,
    $pathname: "/players/:tag",
    $geoip_disable: true,
  });
  expect(player.properties.$cookieless_mode).toBeUndefined();
  for (const detail of [
    "$browser",
    "$os",
    "$screen_height",
    "$timezone",
    "$raw_user_agent",
  ]) {
    expect(player.properties).not.toHaveProperty(detail);
  }
  const browserId = player.properties.distinct_id;
  expect(browserId).toEqual(expect.any(String));
  expect(browserId).not.toBe("$posthog_cookieless");
  expect(await storedKeys(page)).toEqual(
    [storedId, `__ph_opt_in_out_${analyticsFixtureToken}`].sort(),
  );
  expect(await context.cookies()).toEqual([]);

  // Only the Home and Rankings searches are counted, never the header's.
  const banner = page.getByRole("banner");
  await banner.getByRole("button", { name: "Search players" }).click();
  const headerSearch = banner.getByRole("searchbox", {
    name: "Search players and Clash Lens profiles",
  });
  await headerSearch.fill("Secret Name");
  await headerSearch.press("Enter");

  await page.goto(`${analyticsFixtureOrigin}/`);
  const search = page.getByRole("searchbox", {
    name: "Search players and Clash Lens profiles",
  });
  await search.fill("Secret Name");
  await search.press("Enter");
  await expect
    .poll(() => sent("search_submitted").map(({ properties }) => properties))
    .toEqual([expect.objectContaining({ source: "home", input: "name" })]);
  await expect.poll(() => sent("$pageview").length).toBe(2);
  expect(sent("$pageview")[1].properties.distinct_id).toBe(browserId);

  // The footer switch is on every page.
  const toggle = page.getByRole("contentinfo").getByRole("switch", {
    name: "Count return visits",
  });
  await expect(toggle).toHaveAttribute("aria-checked", "true");
  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-checked", "false");
  await expect.poll(() => storedKeys(page)).not.toContain(storedId);
  expect(await page.evaluate(() => localStorage.getItem("clashlens-return-visits"))).toBe(
    "off",
  );

  // Remembered on the next visit, which is counted without an ID.
  await page.goto(`${analyticsFixtureOrigin}/privacy`);
  await expect(
    page.getByRole("main").getByRole("switch", { name: "Count return visits" }),
  ).toHaveAttribute("aria-checked", "false");
  await expect.poll(() => sent("$pageview").length).toBe(3);
  // PostHog makes the day's code from the browser name and version, then deletes them.
  expect(sent("$pageview")[2].properties).toMatchObject({
    route: "privacy",
    distinct_id: "$posthog_cookieless",
    $cookieless_mode: true,
    $raw_user_agent: expect.stringContaining("Chrome/"),
  });
  expect(await storedKeys(page)).not.toContain(storedId);
  expect(await context.cookies()).toEqual([]);

  for (const body of posthog.bodies) {
    expect(body).not.toMatch(/2PP|2026-09|Secret Name|season=/i);
  }
  expect(posthog.events.map(({ event }) => event)).not.toContain("$opt_in");
  expect(outside).toEqual([]);
});

test("switching return visits off stops a held, preparing or retried event carrying the ID", async ({
  page,
}) => {
  await page.clock.install();
  await fakePostHog(page);
  // Marks each send made while the switch is off, and lets any background
  // preparation of the Blog page view finish only once the switch is off.
  await page.addInitScript((host) => {
    const switchedOff = () => localStorage.getItem("clashlens-return-visits") === "off";
    const send = window.fetch.bind(window);
    window.fetch = (input, init) =>
      send(
        String(input).startsWith(host) && switchedOff()
          ? `${String(input)}&switched_off`
          : input,
        init,
      );
    const Compress = window.CompressionStream;
    window.CompressionStream = class extends Compress {
      constructor(format: CompressionFormat) {
        super(format);
        const writer = super.writable.getWriter();
        const writable = new WritableStream<BufferSource>({
          async write(chunk) {
            if (new TextDecoder().decode(chunk).includes('"route":"blog"')) {
              while (!switchedOff()) await new Promise((wait) => setTimeout(wait, 20));
            }
            await writer.write(chunk);
          },
          close: () => writer.close(),
          abort: (reason) => writer.abort(reason),
        });
        Object.defineProperty(this, "writable", { value: writable });
      }
    };
  }, posthogHost);
  const sentWhileOff: SentEvent[] = [];
  let answered = 0;
  await page.route(`${posthogHost}/**`, async (route) => {
    const body = route.request().postDataBuffer();
    if (body && route.request().url().endsWith("&switched_off")) {
      sentWhileOff.push(...decode(body));
    }
    // The first send fails, so PostHog would try it again later.
    if (answered++ === 0) await route.fulfill({ status: 503 });
    else await route.fallback();
  });

  await page.goto(`${analyticsFixtureOrigin}/privacy`);
  await expect.poll(() => answered).toBeGreaterThan(0);
  const browserId = await page.evaluate(
    (key) => JSON.parse(localStorage.getItem(key) ?? "{}").distinct_id,
    storedId,
  );
  expect(browserId).toEqual(expect.any(String));
  await page.getByRole("banner").getByRole("link", { name: "Blog" }).click();
  await expect(page.getByRole("heading", { name: "Blog", level: 1 })).toBeVisible();
  await page
    .getByRole("contentinfo")
    .getByRole("switch", { name: "Count return visits" })
    .click();
  // Long past any wait before sending and any retry of the failed send.
  await page.clock.runFor(120_000);
  await page.getByRole("banner").getByRole("link", { name: "Home", exact: true }).click();
  await expect
    .poll(() => sentWhileOff.filter(({ event }) => event === "$pageview").at(-1))
    .toMatchObject({ properties: { route: "home", distinct_id: "$posthog_cookieless" } });
  expect(sentWhileOff.map(({ properties }) => properties.distinct_id)).not.toContain(
    browserId,
  );
});

test("a sampled load time names the page that was loading", async ({ page }) => {
  const posthog = await fakePostHog(page);
  // Every load is sampled.
  await page.addInitScript(() => {
    const random = Math.random;
    Math.random = () => random() * 0.1;
  });
  // The logo keeps the privacy page loading until the test lets it finish.
  let finishLoading = () => {};
  const loading = new Promise<void>((resolve) => (finishLoading = resolve));
  await page.route("**/images/clashlens-wordmark.svg", async (route) => {
    await loading;
    await route.continue();
  });

  await page.goto(`${analyticsFixtureOrigin}/privacy`, { waitUntil: "domcontentloaded" });
  await expect.poll(() => posthog.events.length).toBeGreaterThan(0);
  await page.getByRole("banner").getByRole("link", { name: "Blog" }).click();
  await expect(page.getByRole("heading", { name: "Blog", level: 1 })).toBeVisible();
  finishLoading();
  await expect
    .poll(() => posthog.events.find(({ event }) => event === "page_performance"))
    .toMatchObject({
      properties: {
        route: "privacy",
        $current_url: `${analyticsFixtureOrigin}/privacy`,
        $pathname: "/privacy",
      },
    });
});

test("switching return visits off in one tab stops the ID in every tab", async ({
  context,
}) => {
  const first = await context.newPage();
  const second = await context.newPage();
  const posthog = await fakePostHog(first);
  const other = await fakePostHog(second);
  for (const tab of [first, second]) {
    await tab.goto(`${analyticsFixtureOrigin}/privacy`);
  }
  await expect.poll(() => other.events.length).toBeGreaterThan(0);
  const toggle = (tab: Page) =>
    tab.getByRole("main").getByRole("switch", { name: "Count return visits" });

  await toggle(first).click();
  await expect(toggle(second)).toHaveAttribute("aria-checked", "false");
  // An in-page move, so the second tab still holds the ID it loaded with.
  await second.getByRole("banner").getByRole("link", { name: "Blog" }).click();
  await expect
    .poll(() => other.events.filter(({ event }) => event === "$pageview").at(-1))
    .toMatchObject({ properties: { route: "blog", distinct_id: "$posthog_cookieless" } });
  expect(await storedKeys(second)).not.toContain(storedId);
  expect(posthog.events.map(({ event }) => event)).not.toContain("$opt_in");
});

test("the switch turns off for the page even when the browser cannot save it", async ({
  page,
}) => {
  const posthog = await fakePostHog(page);
  await page.addInitScript(() => {
    const save = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key: string, value: string) {
      if (key === "clashlens-return-visits")
        throw new DOMException("full", "QuotaExceededError");
      return save.call(this, key, value);
    };
  });
  await page.goto(`${analyticsFixtureOrigin}/privacy`);
  await expect.poll(() => posthog.events.length).toBeGreaterThan(0);
  const toggle = page
    .getByRole("main")
    .getByRole("switch", { name: "Count return visits" });

  await toggle.click();
  await expect(toggle).toHaveAttribute("aria-checked", "false");
  await page.getByRole("banner").getByRole("link", { name: "Blog" }).click();
  await expect
    .poll(() => posthog.events.filter(({ event }) => event === "$pageview").at(-1))
    .toMatchObject({ properties: { route: "blog", distinct_id: "$posthog_cookieless" } });
  expect(await storedKeys(page)).not.toContain(storedId);
});
