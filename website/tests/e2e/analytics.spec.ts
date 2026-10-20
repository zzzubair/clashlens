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
    // No load is sampled for its load time unless a test asks, so event counts
    // never depend on chance or on when the page finished loading.
    const random = Math.random;
    Math.random = () => 0.1 + random() * 0.9;
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

const choiceKey = "clashlens-analytics";

function banner(page: Page) {
  return page.getByRole("region", { name: /May we use PostHog/ });
}

/** Opens the question again from the site footer. */
async function reopenFromFooter(page: Page) {
  await page
    .getByRole("contentinfo")
    .getByRole("button", { name: "Analytics choices" })
    .click();
}

/** Every page load starts as a visitor who already accepted. */
async function acceptedBeforeLoad(page: Page) {
  await page.addInitScript((key) => localStorage.setItem(key, "accepted"), choiceKey);
}

test("with no project token no analytics code loads, nothing is sent and nothing is asked", async ({
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
  await expect(page.getByRole("button", { name: /analytics choice/i })).toHaveCount(0);
  await expect(banner(page)).toHaveCount(0);
  await page.getByRole("contentinfo").getByRole("link", { name: "Blog" }).click();
  await expect(page.getByRole("heading", { name: "Blog", level: 1 })).toBeVisible();

  expect(requests.filter((url) => !url.startsWith(`${blogFixtureOrigin}/`))).toEqual([]);
  expect(requests.filter((url) => /posthog|module\.slim/i.test(url))).toEqual([]);
  expect(await storedKeys(page)).toEqual([]);
  expect(await context.cookies()).toEqual([]);
});

test("asks first, counts only after Accept without player tags, and remembers the choice", async ({
  page,
  context,
}) => {
  const posthog = await fakePostHog(page);
  const requests: string[] = [];
  page.on("request", (request) => requests.push(request.url()));
  const sent = (name: string) => posthog.events.filter(({ event }) => event === name);

  // Before a choice: no PostHog code, no requests and nothing kept, across pages.
  await page.goto(`${analyticsFixtureOrigin}/players/%232PP?season=2026-09`);
  await expect(banner(page)).toBeVisible();
  await page.getByRole("banner").getByRole("link", { name: "Blog" }).click();
  await expect(page.getByRole("heading", { name: "Blog", level: 1 })).toBeVisible();
  await page.goto(`${analyticsFixtureOrigin}/players/%232PP?season=2026-09`);
  await expect(banner(page)).toBeVisible();
  expect(requests.filter((url) => !url.startsWith(`${analyticsFixtureOrigin}/`))).toEqual(
    [],
  );
  expect(requests.filter((url) => /posthog|module\.slim/i.test(url))).toEqual([]);
  expect(await storedKeys(page)).toEqual([]);

  // Accepting counts the page it was made on, and the lookup it already showed,
  // with a browser ID kept here.
  await banner(page).getByRole("button", { name: "Accept" }).click();
  await expect(banner(page)).toHaveCount(0);
  await expect.poll(() => sent("$pageview").length).toBe(1);
  await expect
    .poll(() => sent("player_lookup_result").map(({ properties }) => properties.route))
    .toEqual(["player"]);
  const [player] = sent("$pageview");
  expect(player.properties).toMatchObject({
    route: "player",
    signed_in: false,
    $current_url: `${analyticsFixtureOrigin}/players/:tag`,
    $pathname: "/players/:tag",
    $geoip_disable: true,
  });
  for (const detail of [
    "$browser",
    "$os",
    "$screen_height",
    "$timezone",
    "$raw_user_agent",
    "$cookieless_mode",
  ]) {
    expect(player.properties).not.toHaveProperty(detail);
  }
  const browserId = player.properties.distinct_id;
  expect(browserId).toEqual(expect.any(String));
  expect(browserId).not.toBe("$posthog_cookieless");
  expect(await storedKeys(page)).toEqual(
    [choiceKey, storedId, `__ph_opt_in_out_${analyticsFixtureToken}`].sort(),
  );
  expect(await context.cookies()).toEqual([]);

  // Only the Home and Rankings searches are counted, never the header's.
  const header = page.getByRole("banner");
  await header.getByRole("button", { name: "Search players" }).click();
  const headerSearch = header.getByRole("searchbox", {
    name: "Search players and Clash Lens profiles",
  });
  await headerSearch.fill("Secret Name");
  await headerSearch.press("Enter");

  // Remembered on the next visit, which is counted with the same ID.
  await page.goto(`${analyticsFixtureOrigin}/`);
  await expect.poll(() => sent("$pageview").length).toBe(2);
  expect(sent("$pageview")[1].properties.distinct_id).toBe(browserId);
  await expect(banner(page)).toHaveCount(0);
  const search = page.getByRole("searchbox", {
    name: "Search players and Clash Lens profiles",
  });
  await search.fill("Secret Name");
  await search.press("Enter");
  await expect
    .poll(() => sent("search_submitted").map(({ properties }) => properties))
    .toEqual([expect.objectContaining({ source: "home", input: "name" })]);

  for (const body of posthog.bodies) {
    expect(body).not.toMatch(/2PP|2026-09|Secret Name|season=/i);
  }
  expect(posthog.events.map(({ event }) => event)).not.toContain("$opt_in");
  expect(
    requests.filter(
      (url) =>
        !url.startsWith(`${analyticsFixtureOrigin}/`) && !url.startsWith(posthogHost),
    ),
  ).toEqual([]);
});

test("accepting again after Decline counts the page it is made on afresh", async ({
  page,
}) => {
  const posthog = await fakePostHog(page);
  const lookups = () =>
    posthog.events.filter(({ event }) => event === "player_lookup_result").length;
  await page.goto(`${analyticsFixtureOrigin}/players/%232PP`);
  await banner(page).getByRole("button", { name: "Accept" }).click();
  await expect.poll(lookups).toBe(1);

  await reopenFromFooter(page);
  await banner(page).getByRole("button", { name: "Decline" }).click();
  await page.getByRole("banner").getByRole("link", { name: "Blog" }).click();
  await expect(page.getByRole("heading", { name: "Blog", level: 1 })).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL(/\/players\/%232PP$/);
  await reopenFromFooter(page);
  await banner(page).getByRole("button", { name: "Accept" }).click();
  await expect.poll(lookups).toBe(2);
});

test("Decline loads nothing, is remembered, and can be changed on the privacy page", async ({
  page,
  context,
}) => {
  const posthog = await fakePostHog(page);
  const requests: string[] = [];
  page.on("request", (request) => requests.push(request.url()));

  await page.goto(`${analyticsFixtureOrigin}/privacy`);
  await expect(page.getByText("You have not chosen yet.")).toBeVisible();
  await banner(page).getByRole("button", { name: "Decline" }).click();
  await expect(banner(page)).toHaveCount(0);
  await expect(page.getByText("You declined visitor counting")).toBeVisible();

  await page.reload();
  await expect(page.getByText("You declined visitor counting")).toBeVisible();
  await expect(banner(page)).toHaveCount(0);
  await page.getByRole("banner").getByRole("link", { name: "Blog" }).click();
  await expect(page.getByRole("heading", { name: "Blog", level: 1 })).toBeVisible();
  expect(requests.filter((url) => /posthog|module\.slim/i.test(url))).toEqual([]);
  expect(await storedKeys(page)).toEqual([choiceKey]);
  expect(await page.evaluate((key) => localStorage.getItem(key), choiceKey)).toBe(
    "declined",
  );
  expect(await context.cookies()).toEqual([]);

  // The privacy page's button reopens the same question.
  await page.goto(`${analyticsFixtureOrigin}/privacy`);
  await page
    .getByRole("main")
    .getByRole("button", { name: "Change your analytics choice" })
    .click();
  await expect(banner(page)).toBeFocused();
  await banner(page).getByRole("button", { name: "Accept" }).click();
  await expect(page.getByText("You accepted visitor counting")).toBeVisible();
  await expect
    .poll(() => posthog.events.filter(({ event }) => event === "$pageview").length)
    .toBe(1);
});

test("changing to Decline deletes the ID and stops a held, preparing or retried event", async ({
  page,
}) => {
  await page.clock.install();
  await fakePostHog(page);
  await acceptedBeforeLoad(page);
  // Marks each send made after Decline, and lets any background preparation
  // of the Blog page view finish only once the visitor has declined.
  await page.addInitScript(
    ([host, key]) => {
      const declined = () => localStorage.getItem(key) === "declined";
      const send = window.fetch.bind(window);
      window.fetch = (input, init) =>
        send(
          String(input).startsWith(host) && declined()
            ? `${String(input)}&declined`
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
                while (!declined()) await new Promise((wait) => setTimeout(wait, 20));
              }
              await writer.write(chunk);
            },
            close: () => writer.close(),
            abort: (reason) => writer.abort(reason),
          });
          Object.defineProperty(this, "writable", { value: writable });
        }
      };
    },
    [posthogHost, choiceKey],
  );
  const sentAfterDecline: SentEvent[] = [];
  let answered = 0;
  await page.route(`${posthogHost}/**`, async (route) => {
    const body = route.request().postDataBuffer();
    if (body && route.request().url().endsWith("&declined")) {
      sentAfterDecline.push(...decode(body));
    }
    // The first send fails, so PostHog would try it again later.
    if (answered++ === 0) await route.fulfill({ status: 503 });
    else await route.fallback();
  });

  await page.goto(`${analyticsFixtureOrigin}/privacy`);
  await expect.poll(() => answered).toBeGreaterThan(0);
  expect(await storedKeys(page)).toContain(storedId);
  await page.getByRole("banner").getByRole("link", { name: "Blog" }).click();
  await expect(page.getByRole("heading", { name: "Blog", level: 1 })).toBeVisible();
  await reopenFromFooter(page);
  await banner(page).getByRole("button", { name: "Decline" }).click();
  // Focus goes back to where the question was opened from.
  await expect(
    page.getByRole("contentinfo").getByRole("button", { name: "Analytics choices" }),
  ).toBeFocused();
  // Long past any wait before sending and any retry of the failed send.
  await page.clock.runFor(120_000);
  await page
    .getByRole("banner")
    .getByRole("link", { name: "Clash Lens", exact: true })
    .click();
  await expect(page.getByRole("searchbox").first()).toBeVisible();
  await page.clock.runFor(120_000);
  expect(sentAfterDecline).toEqual([]);
  expect(await storedKeys(page)).toEqual([choiceKey]);
});

test("a sampled load time names the page that was loading", async ({ page }) => {
  const posthog = await fakePostHog(page);
  await acceptedBeforeLoad(page);
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

test("declining in one tab stops counting and deletes the ID in every tab", async ({
  context,
}) => {
  const first = await context.newPage();
  const second = await context.newPage();
  await fakePostHog(first);
  const other = await fakePostHog(second);
  await first.goto(`${analyticsFixtureOrigin}/privacy`);
  await expect(banner(second)).toHaveCount(0);
  await banner(first).getByRole("button", { name: "Accept" }).click();
  await second.goto(`${analyticsFixtureOrigin}/privacy`);
  await expect.poll(() => other.events.length).toBeGreaterThan(0);
  const before = other.events.length;

  await reopenFromFooter(first);
  await banner(first).getByRole("button", { name: "Decline" }).click();
  await expect(second.getByText("You declined visitor counting")).toBeVisible();
  // An in-page move, so the second tab still has PostHog loaded.
  await second.getByRole("banner").getByRole("link", { name: "Blog" }).click();
  await expect(second.getByRole("heading", { name: "Blog", level: 1 })).toBeVisible();
  await second.waitForTimeout(500);
  expect(other.events.length).toBe(before);
  expect(await storedKeys(second)).toEqual([choiceKey]);
});

test("a Decline another tab made is applied even if it accepted again before this tab heard", async ({
  page,
}) => {
  const posthog = await fakePostHog(page);
  const ids = () =>
    posthog.events
      .filter(({ event }) => event === "$pageview")
      .map(({ properties }) => properties.distinct_id);
  await page.goto(`${analyticsFixtureOrigin}/privacy`);
  await banner(page).getByRole("button", { name: "Accept" }).click();
  await expect.poll(() => ids().length).toBe(1);

  await page.evaluate((key) => {
    for (const newValue of ["declined", "accepted"]) {
      window.dispatchEvent(new StorageEvent("storage", { key, newValue }));
    }
  }, choiceKey);
  await page.getByRole("banner").getByRole("link", { name: "Blog" }).click();
  await expect.poll(() => ids().length).toBe(2);
  expect(ids()[1]).not.toBe(ids()[0]);
});

/** This page can never save the choice itself. */
async function choiceCannotBeSaved(page: Page) {
  await page.addInitScript((key) => {
    const save = Storage.prototype.setItem;
    Storage.prototype.setItem = function (name: string, value: string) {
      if (name === key) throw new DOMException("full", "QuotaExceededError");
      return save.call(this, name, value);
    };
  }, choiceKey);
}

test("a choice applies to the page even when the browser cannot save it", async ({
  page,
}) => {
  const posthog = await fakePostHog(page);
  await choiceCannotBeSaved(page);
  await page.goto(`${analyticsFixtureOrigin}/privacy`);
  await banner(page).getByRole("button", { name: "Accept" }).click();
  await expect.poll(() => posthog.events.length).toBeGreaterThan(0);
  expect(await storedKeys(page)).toContain(storedId);

  await reopenFromFooter(page);
  await banner(page).getByRole("button", { name: "Decline" }).click();
  const before = posthog.events.length;
  await page.getByRole("banner").getByRole("link", { name: "Blog" }).click();
  await expect(page.getByRole("heading", { name: "Blog", level: 1 })).toBeVisible();
  await page.waitForTimeout(500);
  expect(posthog.events.length).toBe(before);
  expect(await storedKeys(page)).toEqual([]);
});

test("a Decline saved in another tab wins over an Accept this page could not save", async ({
  page,
}) => {
  const posthog = await fakePostHog(page);
  await choiceCannotBeSaved(page);
  await page.goto(`${analyticsFixtureOrigin}/privacy`);
  await banner(page).getByRole("button", { name: "Accept" }).click();
  await expect.poll(() => posthog.events.length).toBeGreaterThan(0);

  await page.evaluate((key) => {
    localStorage[key] = "declined";
    window.dispatchEvent(new StorageEvent("storage", { key, newValue: "declined" }));
  }, choiceKey);
  await expect(page.getByText("You declined visitor counting")).toBeVisible();
  const before = posthog.events.length;
  await page.getByRole("banner").getByRole("link", { name: "Blog" }).click();
  await expect(page.getByRole("heading", { name: "Blog", level: 1 })).toBeVisible();
  await page.waitForTimeout(500);
  expect(posthog.events.length).toBe(before);
});
