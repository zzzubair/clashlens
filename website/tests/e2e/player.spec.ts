import { randomUUID } from "node:crypto";

import {
  expect,
  test,
  type APIRequestContext,
  type Page,
  type Request,
} from "@playwright/test";
import { UNSAFE_decodeViaTurboStream } from "react-router";

import type { PlayerPage, RankedBattleEvent } from "../../app/lib/contracts";
import { ensureAccount, signIn } from "./helpers/account";
import {
  WORST_SEASON_SUMMARY,
  WORST_SEASONS,
  worstCasePlayer,
} from "../fixtures/player-worst-case";

// Sends Refresh submissions with an invalid key, so the server refuses them
// before they spend either per-visitor allowance.
async function refuseRefreshes(page: Page, allowNext = () => false) {
  const submissions: string[] = [];
  await page.route("**/resources/players/*/refresh*", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    submissions.push(route.request().postData() ?? "");
    if (allowNext()) return route.continue();
    await route.continue({ postData: "idempotencyKey=invalid" });
  });
  return submissions;
}

function refreshSubmitted(page: Page) {
  return page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      response.url().includes("/resources/players/"),
  );
}

// React Router page data is a flat list of values: objects map "_<key index>"
// to value indexes, arrays list value indexes, -5 is null and -7 undefined.
// A streamed value is ["P", its own index], resolved by a later "P<index>:"
// line; it decodes to undefined, so past Seasons are left out.
function decodePageData(text: string) {
  const values: unknown[] = JSON.parse(text.split("\n")[0]);
  const decode = (index: number): unknown => {
    if (index < 0) return index === -5 ? null : undefined;
    const value = values[index];
    if (Array.isArray(value)) return value[0] === "P" ? undefined : value.map(decode);
    if (value === null || typeof value !== "object") return value;
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [
        values[Number(key.slice(1))],
        decode(item as number),
      ]),
    );
  };
  return decode(0) as Record<string, { data?: { player?: PlayerPage } }>;
}

function encodePageData(data: unknown) {
  const values: unknown[] = [];
  const encode = (value: unknown): number => {
    if (value === null) return -5;
    if (value === undefined) return -7;
    if (typeof value !== "object") return values.push(value) - 1;
    const index = values.push(null) - 1;
    values[index] = Array.isArray(value)
      ? value.map(encode)
      : Object.fromEntries(
          Object.entries(value).map(([key, item]) => [`_${encode(key)}`, encode(item)]),
        );
    return index;
  };
  encode(data);
  return `${JSON.stringify(values)}\n`;
}

function pagePlayer(data: ReturnType<typeof decodePageData>) {
  const player = Object.values(data).find((route) => route.data?.player)?.data?.player;
  expect(player?.currentDay, "saved player has a current Legend day").toBeTruthy();
  return player!;
}

// Read the complete response before changing its data. Adding a value to only
// the first chunk shifts the references used by later streamed values.
async function withServerData(
  html: string,
  edit: (saved: { player: PlayerPage; seasons?: unknown }) => void,
) {
  const enqueue =
    /window\.__reactRouterContext\.streamController\.enqueue\(("(?:[^"\\]|\\.)*")\);/g;
  const stream = [...html.matchAll(enqueue)]
    .map((match) => JSON.parse(match[1]) as string)
    .join("");
  const decoded = await UNSAFE_decodeViaTurboStream(
    new Response(stream).body!,
    globalThis,
  );
  await decoded.done;
  const data = decoded.value as {
    loaderData: Record<string, { player: PlayerPage; pastSeasons: unknown }>;
  };
  const saved = data.loaderData["routes/player"];
  edit(saved);
  saved.pastSeasons = await saved.pastSeasons;
  const escaped = JSON.stringify(encodePageData(data)).replace(
    /[&<>\u2028\u2029]/g,
    (character) => `\\u${character.charCodeAt(0).toString(16).padStart(4, "0")}`,
  );
  let first = true;
  const served = html.replace(enqueue, () => {
    if (!first) return "";
    first = false;
    return `window.__reactRouterContext.streamController.enqueue(${escaped});`;
  });
  expect(served).not.toBe(html);
  return served;
}

function withServerAge(html: string, ageSeconds: number) {
  return withServerData(html, (saved) => {
    saved.player.profile.freshness.ageSeconds = ageSeconds;
  });
}

test("saved-page age override preserves streamed history and unrelated counts", async () => {
  const values: unknown[] = JSON.parse(
    encodePageData({
      loaderData: {
        "routes/player": {
          player: { profile: { freshness: { ageSeconds: 1 } }, attacks: 1 },
          pastSeasons: [],
        },
      },
    }),
  );
  const historyIndex = values.findIndex((value) => Array.isArray(value));
  values[historyIndex] = ["P", historyIndex];
  const chunks = [
    `${JSON.stringify(values)}\n`,
    `P${historyIndex}:[[${values.length + 1}],42]\n`,
  ];
  const html = chunks
    .map(
      (chunk) =>
        `window.__reactRouterContext.streamController.enqueue(${JSON.stringify(chunk)});`,
    )
    .join("");
  const served = await withServerAge(html, 120);
  const stream = [...served.matchAll(/streamController\.enqueue\(("(?:[^"\\]|\\.)*")\)/g)]
    .map((match) => JSON.parse(match[1]) as string)
    .join("");
  const decoded = await UNSAFE_decodeViaTurboStream(
    new Response(stream).body!,
    globalThis,
  );
  await decoded.done;
  const data = decoded.value as {
    loaderData: Record<
      string,
      {
        player: { profile: { freshness: { ageSeconds: number } }; attacks: number };
        pastSeasons: Promise<number[]> | number[];
      }
    >;
  };
  const saved = data.loaderData["routes/player"];
  expect(saved.player.profile.freshness.ageSeconds).toBe(120);
  expect(saved.player.attacks).toBe(1);
  expect(await saved.pastSeasons).toEqual([42]);
});

for (const ageSeconds of [30, 60, 61, 120]) {
  test(`profile visit refreshes once only when the server says its saved check is older than 60 seconds (${ageSeconds}s)`, async ({
    page,
    browser,
    baseURL,
  }) => {
    // Reuse one saved document so background collection cannot move the boundary.
    // Reading it without JavaScript must not initiate a Refresh.
    const savedContext = await browser.newContext({ javaScriptEnabled: false, baseURL });
    let html: Buffer;
    let observedAt: string;
    try {
      const savedPage = await savedContext.newPage();
      const response = await savedPage.goto("/players/%232PP");
      html = await response!.body();
      observedAt = (await savedPage.locator(".player-updated").getAttribute("datetime"))!;
    } finally {
      await savedContext.close();
    }
    const automaticCount = ageSeconds > 60 ? 1 : 0;
    const served = await withServerAge(html.toString(), ageSeconds);
    await page.route("**/players/%232PP", (route) =>
      route.fulfill({ contentType: "text/html", body: served }),
    );
    // The browser clock disagrees with the server, which must decide. Shift
    // Date.now directly: Playwright's clock also hides reload navigation timing.
    await page.addInitScript(
      (offset) => {
        const now = Date.now;
        Date.now = () => now() + offset;
      },
      Date.parse(observedAt) + (automaticCount ? 0 : 3_600_000) - Date.now(),
    );
    // Exercise the existing refusal display without spending the shared allowance.
    const submissions = await refuseRefreshes(page);

    const automatic = automaticCount ? refreshSubmitted(page) : null;
    await page.goto("/players/%232PP");
    await automatic;
    const refusal = page
      .getByRole("alert")
      .filter({ hasText: "Check the submitted value" });
    if (automaticCount) await expect(refusal).toBeVisible();
    await page.waitForLoadState("networkidle");
    expect(submissions).toHaveLength(automaticCount);
    await expect(page.locator(".player-updated")).toHaveAttribute("datetime", observedAt);
    await expect(page.getByText("Current trophies", { exact: true })).toBeVisible();

    // The rejected automatic request has re-rendered the page. Manual Refresh
    // must still submit the same valid form, without another automatic attempt.
    const manual = refreshSubmitted(page);
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await manual;
    await expect(refusal).toBeVisible();
    await page.waitForLoadState("networkidle");
    expect(submissions).toHaveLength(automaticCount + 1);
    const key = await page.locator('input[name="idempotencyKey"]').inputValue();
    for (const submission of submissions) {
      expect(new URLSearchParams(submission).get("idempotencyKey")).toBe(key);
    }
    if (automaticCount)
      expect(new URLSearchParams(submissions[0]).get("trigger")).toBe("automatic");
    expect(new URLSearchParams(submissions.at(-1)).get("trigger")).toBeNull();

    // A full reload retains its existing unconditional Refresh behavior, and
    // stale data must not add a second request on top of it.
    const reloaded = refreshSubmitted(page);
    await page.reload();
    await reloaded;
    await expect(refusal).toBeVisible();
    await page.waitForLoadState("networkidle");
    expect(submissions).toHaveLength(automaticCount + 2);
    expect(new URLSearchParams(submissions.at(-1)).get("trigger")).toBeNull();
  });
}

test("a skipped automatic refresh leaves saved data and its time without an alert", async ({
  page,
  request,
}) => {
  const saved = await request.get("/players/%232PP");
  const savedHtml = await saved.text();
  await page.route("**/players/%232PP", async (route) =>
    route.fulfill({
      contentType: "text/html",
      body: await withServerAge(savedHtml, 120),
    }),
  );
  let submissions = 0;
  await page.route("**/resources/players/*/refresh*", (route) => {
    submissions++;
    expect(route.request().method()).toBe("POST");
    expect(new URLSearchParams(route.request().postData() ?? "").get("trigger")).toBe(
      "automatic",
    );
    return route.fulfill({
      status: 200,
      contentType: "text/x-script",
      headers: { "X-Remix-Response": "yes" },
      body: encodePageData({ data: null }),
    });
  });
  const automatic = refreshSubmitted(page);
  await page.goto("/players/%232PP");
  await automatic;
  await page.waitForLoadState("networkidle");
  expect(submissions).toBe(1);
  await expect(
    page.getByRole("heading", { name: "Synthetic Clasher 001" }),
  ).toBeVisible();
  await expect(page.getByText("Current trophies", { exact: true })).toBeVisible();
  const updated = page.locator(".player-updated");
  await expect(updated).toBeVisible();
  expect(Date.parse((await updated.getAttribute("datetime"))!)).not.toBeNaN();
  await expect(page.getByRole("alert")).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Player refresh" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Refresh", exact: true })).toBeEnabled();
  await expect(page.getByRole("region", { name: "Save player" })).toHaveCount(0);
});

test("a battle processed shortly after a completed Refresh reaches the open page", async ({
  page,
  browser,
  baseURL,
}) => {
  test.setTimeout(120_000);
  let html = "";
  let observedAt = "";
  let savedData = "";
  let dataType = "";
  // On a fresh stack the worker may not have saved this player's current
  // Legend day yet; it waits its turn behind other Reset and live work.
  await expect(async () => {
    const savedContext = await browser.newContext({ javaScriptEnabled: false, baseURL });
    try {
      const savedPage = await savedContext.newPage();
      const response = await savedPage.goto("/players/%232PP");
      html = await response!.text();
      observedAt = (await savedPage.locator(".player-updated").getAttribute("datetime"))!;
      const dataResponse = await savedContext.request.get("/players/%232PP.data");
      savedData = await dataResponse.text();
      dataType = dataResponse.headers()["content-type"];
    } finally {
      await savedContext.close();
    }
    expect(savedData).toContain(observedAt);
    pagePlayer(decodePageData(savedData));
  }).toPass({ timeout: 90_000, intervals: [1000] });
  const earlierAt = new Date(Date.parse(observedAt) - 1).toISOString();

  // The fake Clash API dates its battles to the previous Legend day, so publish
  // an attack in the current one. The page shows one entry per Legend day,
  // which may come from the season log rather than currentDay.
  const processed = decodePageData(savedData);
  const player = pagePlayer(processed);
  const attack: RankedBattleEvent = {
    battleId: "catch-up-attack",
    battleTimestamp: new Date().toISOString(),
    opponent: { tag: "#PYLQ", name: "Catch-up Clasher" },
    destructionPercentage: 100,
    stars: 3,
    trophyChange: 40,
    perspectiveDisagreement: false,
  };
  for (const day of [player.currentDay!, ...player.recentDays, ...player.seasonDays]) {
    if (day.period === player.currentDay!.period) day.offenseEvents.unshift(attack);
  }
  const processedData = encodePageData(processed);
  const attacks = page.locator(".battle-slot-attack", { hasText: "Catch-up Clasher" });

  // Before processing, page reads return the earlier check. Completion carries
  // the processed profile without its battles; only later reads include them.
  // The Refresh itself is faked, so it spends none of the shared allowance.
  const work = {
    kind: "refresh-work",
    workId: randomUUID(),
    tag: player.tag,
    state: "queued",
    progressPercent: 0,
    message: "Queued.",
    publishedAt: null,
  };
  let completed = false;
  let readsAfterCompletion = 0;
  await page.route("**/players/%232PP", async (route) =>
    route.fulfill({
      contentType: "text/html",
      body: await withServerAge(html.replaceAll(observedAt, earlierAt), 120),
    }),
  );
  await page.route("**/players/%232PP.data*", (route) => {
    const published = completed && readsAfterCompletion++ > 0;
    return route.fulfill({
      contentType: dataType,
      body: published ? processedData : savedData.replaceAll(observedAt, earlierAt),
    });
  });
  await page.route("**/resources/players/*/refresh*", (route) => {
    if (route.request().method() === "POST") {
      return route.fulfill({
        status: 202,
        contentType: "text/x-script",
        headers: { "X-Remix-Response": "yes" },
        body: encodePageData({ data: work }),
      });
    }
    completed = true;
    return route.fulfill({
      json: {
        ...work,
        kind: "refresh-status",
        state: "complete",
        progressPercent: 100,
        message: "Complete.",
        publishedAt: observedAt,
        player: { ...player, currentDay: null, recentDays: [], seasonDays: [] },
      },
    });
  });

  await page.goto("/players/%232PP");
  await expect(page.locator(".player-updated")).toHaveAttribute("datetime", earlierAt);
  await expect(attacks).toHaveCount(0);
  await expect.poll(() => readsAfterCompletion, { timeout: 30_000 }).toBeGreaterThan(1);
  await expect(page.locator(".player-updated")).toHaveAttribute("datetime", observedAt);
  await expect(attacks).not.toHaveCount(0);
});

test("player page canonicalizes the tag and shows collected profile data", async ({
  page,
}) => {
  await refuseRefreshes(page);
  await page.goto("/players/%232pp");

  await expect(page).toHaveURL(/\/players\/%232PP$/);
  await expect(
    page.getByRole("heading", { name: "Synthetic Clasher 001" }),
  ).toBeVisible();
  await expect(page.getByText("#2PP", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "Daily Legend log", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Current trophies", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Refresh", exact: true })).toBeVisible();
});

test("player page stays within a narrow viewport", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 900 });
  await refuseRefreshes(page);
  await page.goto("/players/%232PP");

  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    ),
  ).toBe(0);
});

test("battle stats fit at iPhone width", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await refuseRefreshes(page);
  await page.goto("/players/%232PP");
  const stats = page.getByRole("region", { name: "Season summary" });
  await expect(stats.getByText("Hit rate", { exact: true })).toBeVisible();
  await expect(stats.getByRole("rowheader", { name: "Defenses" })).toBeVisible();
  await expect(stats.getByText("Per defense", { exact: true })).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    ),
  ).toBe(0);
});

// Nothing may widen the page, and every visible control must take a tap at its
// own center. Safari once let full-row link overlays cover other controls.
async function expectUsableLayout(page: Page) {
  const problems = await page.locator("main").evaluate((main) => {
    const found: string[] = [];
    const { scrollWidth, clientWidth } = document.documentElement;
    if (scrollWidth > clientWidth)
      found.push(`page is ${scrollWidth - clientWidth}px too wide`);
    for (const control of main.querySelectorAll("a, button, summary")) {
      if (!control.checkVisibility()) continue;
      control.scrollIntoView({ block: "center", inline: "nearest" });
      const box = control.getBoundingClientRect();
      const hit = document.elementFromPoint(
        box.x + box.width / 2,
        box.y + box.height / 2,
      );
      if (!hit || !control.contains(hit))
        found.push(
          `"${control.textContent?.trim()}" is covered by ${hit?.outerHTML.slice(0, 80)}`,
        );
    }
    return found;
  });
  expect(problems).toEqual([]);
}

// Opens #2PP from search with worst-case saved data and one past Season.
// `servesSeason` returning false leaves that read of the past Season unanswered.
async function openWorstCasePlayer(page: Page, servesSeason = () => true) {
  // Day 6 of a Season, so every worst-case day belongs to the current Season.
  const now = Date.parse("2026-10-10T12:00:00Z");
  await page.clock.install({ time: now });
  await page.goto("/blog");
  const saved = await page.request.get("/players/%232PP.data");
  const dataType = saved.headers()["content-type"];
  const decoded = decodePageData(await saved.text());
  const data = Object.values(decoded).find((route) => route.data?.player)!.data!;
  Object.assign(data, { player: worstCasePlayer("#2PP", now), seasons: WORST_SEASONS });
  const current = encodePageData(decoded);
  Object.assign(data, {
    selectedSeason: WORST_SEASON_SUMMARY.seasonId,
    historical: { ...WORST_SEASON_SUMMARY, tag: "#2PP" },
  });
  const season = encodePageData(decoded);
  await page.route("**/players/%232PP.data*", (route) => {
    const past = new URL(route.request().url()).searchParams.has("season");
    if (past && !servesSeason()) return;
    return route.fulfill({ contentType: dataType, body: past ? season : current });
  });

  // Client navigation reads the replaced page data.
  await page.getByRole("button", { name: "Search players" }).click();
  await page.getByRole("searchbox").fill("#2PP");
  await page.getByRole("searchbox").press("Enter");
}

test("player page holds worst-case player data on a phone", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 568 });
  await refuseRefreshes(page);
  const automatic = refreshSubmitted(page);
  await openWorstCasePlayer(page);
  await automatic;
  const seasonMenu = page.getByRole("combobox", { name: "Season", exact: true });
  await seasonMenu.selectOption({ index: 1 });
  const finish = page.locator(".season-summary");
  await expect(finish.getByText("6,498", { exact: true })).toBeVisible();
  await expect(page.getByText("Stars unknown for 1 attack.")).toBeVisible();
  await expectUsableLayout(page);

  await seasonMenu.selectOption("");
  await expect(page.getByRole("heading", { name: "xXDragonSlayerX" })).toBeVisible();
  await expect(page.getByText("Count unknown", { exact: true })).toHaveCount(2);
  await expect(page.getByText("-1,288", { exact: true })).toBeVisible();
  await expect(page.getByText(/1,284 days old/)).toBeVisible();
  // A nameless opponent shows its tag once, as the name.
  const nameless = page.locator(".battle-slot-attack").nth(8);
  await expect(nameless.locator(".battle-opponent strong")).toHaveText("#P0Y");
  await expect(nameless.locator(".player-tag")).toHaveCount(0);
  for (const size of [
    { width: 320, height: 568 },
    { width: 750, height: 342 },
    { width: 1280, height: 900 },
  ]) {
    await page.setViewportSize(size);
    await expectUsableLayout(page);
  }
});

test("an opponent's Legend day opens in the page at the battle, and Back returns", async ({
  page,
}) => {
  await refuseRefreshes(page);
  await openWorstCasePlayer(page);
  await expect(page.getByRole("heading", { name: "xXDragonSlayerX" })).toBeVisible();
  const link = page.locator(".battle-profile-link").first();
  const href = (await link.getAttribute("href"))!;
  const target = new URL(href, page.url());
  const opponent = decodeURIComponent(target.pathname.split("/").pop()!);
  // The opponent's page data: the same worst-case days, so the battle is there.
  const saved = await page.request.get("/players/%232PP.data");
  const decoded = decodePageData(await saved.text());
  const data = Object.values(decoded).find((route) => route.data?.player)!.data!;
  Object.assign(data, {
    player: worstCasePlayer(opponent, Date.parse("2026-10-10T12:00:00Z")),
    seasons: WORST_SEASONS,
  });
  const opponentData = encodePageData(decoded);
  await page.route(`**${target.pathname}.data*`, (route) =>
    route.fulfill({ contentType: saved.headers()["content-type"], body: opponentData }),
  );
  const documents: string[] = [];
  page.on("request", (request) => {
    if (request.resourceType() === "document") documents.push(request.url());
  });

  await link.click();
  await expect(page).toHaveURL(target.href);
  const battle = page.locator(`[id="${decodeURIComponent(target.hash.slice(1))}"]`);
  await expect(battle).toHaveClass(/battle-arrival/);
  await expect(battle).toBeInViewport();
  await page.goBack();
  await expect(page).toHaveURL(/\/players\/%232PP$/);
  await expect(page.locator(".battle-profile-link").first()).toHaveAttribute(
    "href",
    href,
  );
  // Both moves stayed in the open page: no page was downloaded again.
  expect(documents).toEqual([]);
});

// React leaves a folded day's battles as the server sent them while the page
// starts up, and draws them itself once the day opens or a Refresh changes them.
test("a folded Legend day keeps every battle as the page starts and after a Refresh", async ({
  browser,
}) => {
  test.setTimeout(120_000);
  const battles = "details.legend-day:not([open]) .battle-slot[id]";
  const noScript = await browser.newContext({ javaScriptEnabled: false });
  let sent: string[] = [];
  // On a fresh stack the worker may not have saved the previous Legend day,
  // which holds the fake Clash API's battle, yet.
  try {
    const page = await noScript.newPage();
    await expect(async () => {
      await page.goto("/players/%232PP");
      sent = await page.locator(battles).evaluateAll((rows) => rows.map((row) => row.id));
      expect(sent.length).toBeGreaterThan(0);
    }).toPass({ timeout: 90_000, intervals: [1000] });
  } finally {
    await noScript.close();
  }
  // Times change from UTC to the browser's own zone once the page has started.
  const context = await browser.newContext({ timezoneId: "Asia/Tokyo" });
  try {
    const page = await context.newPage();
    await refuseRefreshes(page);
    await page.goto("/players/%232PP");
    await expect(page.locator("time.local-time").first()).not.toContainText("UTC");
    expect(
      await page.locator(battles).evaluateAll((rows) => rows.map((row) => row.id)),
    ).toEqual(sent);

    // A completed Refresh adds an attack to a folded day.
    const saved = await page.request.get("/players/%232PP.data");
    const later = decodePageData(await saved.text());
    const player = pagePlayer(later);
    const older = player.recentDays.find(
      (day) => day.period !== player.currentDay!.period,
    )!;
    for (const day of [...player.recentDays, ...player.seasonDays]) {
      if (day.period !== older.period) continue;
      day.offenseEvents.unshift({
        battleId: "after-refresh",
        battleTimestamp: new Date().toISOString(),
        opponent: { tag: "#PYLR", name: "After Refresh" },
        destructionPercentage: 100,
        stars: 3,
        trophyChange: 40,
        perspectiveDisagreement: false,
      });
    }
    await page.route("**/players/%232PP.data*", (route) =>
      route.fulfill({
        contentType: saved.headers()["content-type"],
        body: encodePageData(later),
      }),
    );
    const work = {
      kind: "refresh-work",
      workId: randomUUID(),
      tag: player.tag,
      state: "queued",
      progressPercent: 0,
      message: "Queued.",
      publishedAt: null,
    };
    await page.route("**/resources/players/*/refresh*", (route) =>
      route.request().method() === "POST"
        ? route.fulfill({
            status: 202,
            contentType: "text/x-script",
            headers: { "X-Remix-Response": "yes" },
            body: encodePageData({ data: work }),
          })
        : route.fulfill({
            json: {
              ...work,
              kind: "refresh-status",
              state: "complete",
              progressPercent: 100,
              message: "Complete.",
              publishedAt: new Date().toISOString(),
              player: { ...player, currentDay: null, recentDays: [], seasonDays: [] },
            },
          }),
    );
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await expect(
      page.locator("details.legend-day:not([open]) .battle-slot-attack", {
        hasText: "After Refresh",
      }),
    ).toHaveCount(1);

    const folded = page
      .locator("details.legend-day:not([open])")
      .filter({
        has: page.getByRole("button", { name: "Show army", includeHidden: true }),
      })
      .first();
    const day = page.locator(`[id="${await folded.getAttribute("id")}"]`);
    await day.locator("summary").click();
    await day.getByRole("button", { name: "Show army" }).first().click();
    await expect(day.getByRole("button", { name: "Hide army" })).toBeVisible();
    await expect(day.locator(".battle-slot[id]")).not.toHaveCount(0);
  } finally {
    await context.close();
  }
});

// Opens a battle link as a fresh page load, the way a Discord button does. The
// server's page shows `served`; every later read of the page data gets `later`.
async function openBattleLink(
  page: Page,
  link: string,
  now: number,
  served: (saved: Record<string, unknown>) => void,
  later: (saved: Record<string, unknown>) => void,
) {
  await refuseRefreshes(page);
  await page.clock.install({ time: now });
  const url = new URL(link, "http://clashlens.test");
  const html = await (await page.request.get(`${url.pathname}${url.search}`)).text();
  // Only the given player data shows, whatever the local services answered.
  const clean = { error: null, seasonsError: null, lookupError: null };
  const document = await withServerData(html, (saved) =>
    served(Object.assign(saved, clean)),
  );
  const pageData = await page.request.get(`/players/%232PP.data${url.search}`);
  const decoded = decodePageData(await pageData.text());
  later(
    Object.assign(
      Object.values(decoded).find((route) => route.data && "player" in route.data)!.data!,
      clean,
    ),
  );
  const body = encodePageData(decoded);
  await page.route(
    (target) => target.pathname === "/players/%232PP",
    (route) => route.fulfill({ contentType: "text/html", body: document }),
  );
  await page.route("**/players/%232PP.data*", (route) =>
    route.fulfill({ contentType: pageData.headers()["content-type"], body }),
  );
  await page.goto(link);
}

for (const [device, size] of [
  ["phone", { width: 375, height: 812 }],
  ["desktop", { width: 1280, height: 900 }],
] as const) {
  for (const theme of ["light", "dark"] as const) {
    test.describe(`battle links on ${device} in ${theme}`, () => {
      test.beforeEach(async ({ page }) => {
        await page.setViewportSize(size);
        await page.addInitScript(
          (value) => localStorage.setItem("clashlens-theme", value),
          theme,
        );
      });

      test("a battle saved before its day updates is highlighted once it arrives", async ({
        page,
      }) => {
        const now = Date.parse("2026-10-10T12:00:00Z");
        const player = worstCasePlayer("#2PP", now);
        const battle = player.currentDay!.offenseEvents[0];
        const link = `/players/%232PP?day=2026-10-10#battle-${encodeURIComponent(battle.battleId)}`;
        const withoutBattle = {
          ...player,
          currentDay: {
            ...player.currentDay!,
            offenseEvents: player.currentDay!.offenseEvents.slice(1),
          },
        };
        await openBattleLink(
          page,
          link,
          now,
          (saved) =>
            Object.assign(saved, { player: withoutBattle, seasons: WORST_SEASONS }),
          (saved) => Object.assign(saved, { player, seasons: WORST_SEASONS }),
        );
        await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
        const waiting = page.getByRole("status").filter({
          hasText: "Still adding this battle to the Legend log.",
        });
        await expect(waiting).toBeVisible();
        await expectUsableLayout(page);
        // The page reads the player again within five seconds and finds the battle.
        const slot = page.locator(`[id="battle-${battle.battleId}"]`);
        await expect(slot).toHaveClass(/battle-arrival/, { timeout: 10_000 });
        await expect(slot).toBeInViewport();
        await expect(waiting).toHaveCount(0);
        await expect(page).toHaveURL(new URL(link, page.url()).href);

        // A card replaced as the highlight starts, as when Reset moves its day, is
        // the one highlighted.
        await expect(slot).not.toHaveClass(/battle-arrival/);
        await page.evaluate((id) => {
          window.dispatchEvent(new Event("pageshow"));
          const card = document.getElementById(id)!;
          card.replaceWith(card.cloneNode(true));
        }, `battle-${battle.battleId}`);
        await expect(slot).toHaveClass(/battle-arrival/);
      });

      test("a battle the page never gets is named, not silently missed", async ({
        page,
      }) => {
        const now = Date.parse("2026-10-10T12:00:00Z");
        const player = worstCasePlayer("#2PP", now);
        const data = (saved: Record<string, unknown>) =>
          Object.assign(saved, { player, seasons: WORST_SEASONS });
        await openBattleLink(
          page,
          "/players/%232PP?day=2026-10-10#battle-not-saved",
          now,
          data,
          data,
        );
        await expect(page.getByText("Still adding this battle")).toBeVisible();
        // The page stops checking after two minutes and says so.
        const missing = page
          .getByRole("status")
          .filter({ hasText: "This battle isn't on this page." });
        await expect
          .poll(
            async () => {
              await page.clock.runFor(60_000);
              return missing.count();
            },
            { timeout: 15_000 },
          )
          .toBe(1);
        await expectUsableLayout(page);
        await page.getByRole("button", { name: "Check again" }).click();
        await expect(page.getByText("Still adding this battle")).toBeVisible();

        // An older day's battle gets the same checks: a late report can still add it.
        await page.goto("/players/%232PP?day=2026-10-06#battle-not-saved");
        await expect(page.getByText("Still adding this battle")).toBeVisible();
      });

      test("a last-day battle linked after its Season ended opens in that Season", async ({
        page,
      }) => {
        // Just after the Reset that ended the Season on 30 Aug.
        const now = Date.parse("2026-08-30T06:00:00Z");
        const player = worstCasePlayer("#2PP", now);
        const lastDay = player.seasonDays[0];
        const battle = lastDay.defenseEvents[0];
        const link = `/players/%232PP?day=2026-08-29#battle-${encodeURIComponent(battle.battleId)}`;
        const ended = (saved: Record<string, unknown>) =>
          Object.assign(saved, {
            player,
            seasons: WORST_SEASONS,
            selectedSeason: WORST_SEASON_SUMMARY.seasonId,
            historical: { ...WORST_SEASON_SUMMARY, tag: "#2PP" },
          });
        await openBattleLink(page, link, now, ended, ended);
        await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
        await expect(
          page.getByRole("heading", { name: "Battles on 29 Aug 2026" }),
        ).toBeVisible();
        const slot = page.locator(`[id="battle-${battle.battleId}"]`);
        await expect(slot).toHaveClass(/battle-arrival/);
        await expect(slot).toBeInViewport();
        await expect(page.locator('[aria-current="date"]')).toHaveCount(1);
        await expect(page.getByText("Daily Legend log")).toHaveCount(0);
        await expect(page).toHaveURL(new URL(link, page.url()).href);
        await expectUsableLayout(page);

        // A day of that Season whose battles the page no longer has says so.
        await page.goto("/players/%232PP?day=2026-08-10#battle-not-saved");
        await expect(
          page.getByRole("heading", { name: "Battles on 10 Aug 2026" }),
        ).toBeVisible();
        await expect(
          page.getByText("Attacks and defenses from this day are no longer shown here."),
        ).toBeVisible();
        await expectUsableLayout(page);
      });
    });
  }
}

for (const status of [400, 429]) {
  test(`a Season switch shows that Season when the automatic refresh gets ${status}`, async ({
    page,
  }) => {
    let switchStarted!: () => void;
    const switching = new Promise<void>((resolve) => (switchStarted = resolve));
    await page.route("**/resources/players/*/refresh*", async (route) => {
      await switching;
      if (status === 400) return route.continue({ postData: "idempotencyKey=invalid" });
      return route.fulfill({
        status,
        contentType: "text/x-script",
        headers: { "X-Remix-Response": "yes" },
        body: encodePageData({
          data: { error: { code: "rate_limited", message: "Too many requests." } },
        }),
      });
    });
    // The refused refresh answers while the switch's first read is still
    // waiting, and that read is never answered. Only reading the page data
    // again for the chosen Season can show it.
    let seasonReads = 0;
    await openWorstCasePlayer(page, () => {
      if (seasonReads++ > 0) return true;
      switchStarted();
      return false;
    });
    const refused = refreshSubmitted(page);
    await page
      .getByRole("combobox", { name: "Season", exact: true })
      .selectOption({ index: 1 });
    expect((await refused).status()).toBe(status);
    await expect(page).toHaveURL(/\?season=/);
    await expect(
      page.locator(".season-summary").getByText("6,498", { exact: true }),
    ).toBeVisible();
  });
}

test("season navigation clears refresh state for the same player", async ({ page }) => {
  // Only the manual Refresh below may spend the shared allowance.
  let manual = false;
  await refuseRefreshes(page, () => {
    const allowed = manual;
    manual = false;
    return allowed;
  });
  // Fake players have no ended Season with Clash Lens days to list, so the
  // past Season is opened by its address and left through the Season menu.
  await page.goto("/players/%232PP?season=1788757200");
  await page.waitForLoadState("networkidle");
  manual = true;
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  const refresh = page.getByRole("region", { name: "Player refresh" });
  await expect(refresh).toContainText(/Refreshing…|Updated\./);

  await page.getByRole("combobox", { name: "Season", exact: true }).selectOption("");
  await expect(page).toHaveURL(/\/players\/%232PP$/);
  await expect(refresh).toHaveCount(0);
});

test("Back after switching Seasons returns to Your watchlists in one step", async ({
  page,
}) => {
  await refuseRefreshes(page);
  await signIn(page);
  await ensureAccount(page, "lensscout", "Lens Scout");
  await page.goto("/account/watchlists");
  const group = page.locator(".group-card").filter({
    has: page.getByRole("heading", { name: "War plan" }),
  });
  if ((await group.count()) === 0) {
    await page.getByLabel("Watchlist name").first().fill("War plan");
    await page.getByRole("button", { name: "Create watchlist" }).click();
  }
  const members = group.getByRole("list", { name: "Players in War plan" });
  if ((await members.filter({ hasText: "#2PP" }).count()) === 0) {
    await group.getByRole("link", { name: "Add player" }).click();
    await group.getByLabel("Player tag").fill("#2PP");
    await group.getByRole("button", { name: "Add", exact: true }).click();
  }
  const groupPlayer = members.getByRole("listitem").filter({ hasText: "#2PP" });
  await expect(groupPlayer).toBeVisible();

  // Give the saved player past Seasons to switch between.
  const html = await (await page.request.get("/players/%232PP")).text();
  const served = await withServerData(html, (saved) => {
    saved.seasons = WORST_SEASONS;
  });
  await page.route("**/players/%232PP", (route) =>
    route.fulfill({ contentType: "text/html", body: served }),
  );
  const saved = await page.request.get("/players/%232PP.data");
  const decoded = decodePageData(await saved.text());
  const data = Object.values(decoded).find((route) => route.data?.player)!.data!;
  Object.assign(data, { seasons: WORST_SEASONS });
  const current = encodePageData(decoded);
  Object.assign(data, {
    selectedSeason: WORST_SEASON_SUMMARY.seasonId,
    historical: { ...WORST_SEASON_SUMMARY, tag: "#2PP" },
  });
  const season = encodePageData(decoded);
  await page.route("**/players/%232PP.data*", (route) =>
    route.fulfill({
      contentType: saved.headers()["content-type"],
      body: new URL(route.request().url()).searchParams.has("season") ? season : current,
    }),
  );

  await groupPlayer.getByRole("link").first().click();
  await expect(page).toHaveURL(/\/players\/%232PP$/);
  const seasonMenu = page.getByRole("combobox", { name: "Season", exact: true });
  await seasonMenu.selectOption({ index: 1 });
  await expect(page).toHaveURL(/\/players\/%232PP\?season=/);
  await seasonMenu.selectOption("");
  await expect(page).toHaveURL(/\/players\/%232PP$/);

  await page.getByRole("link", { name: "Back to Your watchlists" }).click();
  await expect(page).toHaveURL(/\/account\/watchlists$/);
  await expect(page.getByRole("heading", { name: "Your watchlists" })).toBeVisible();
  // The groups list sits in the main menu, so it has no Back.
  await expect(page.locator(".back-link")).toHaveCount(0);
});

test("unknown tag starts anonymously, shows progress, and enters tracking", async ({
  page,
}) => {
  await refuseRefreshes(page);
  await page.goto("/?q=%23lqqp");
  await expect(page).toHaveURL(/\/players\/%23LQQP$/);
  await expect(page.getByRole("region", { name: "Player lookup" })).toContainText(
    "Checking this tag",
  );
  await expect(page.getByRole("button", { name: "Start tracking" })).toHaveCount(0);
  // Tracking shows in place, as the tracked page with current trophies; no
  // line pushes the page down to say so.
  await expect(page.getByText("Current trophies", { exact: true })).toBeVisible({
    timeout: 30_000,
  });
  await expect(
    page.getByRole("heading", { name: "Lookup eligible Clasher" }),
  ).toBeVisible();
  await expect(page.getByText("Now tracking in Legend I.", { exact: true })).toHaveCount(
    0,
  );
});

test("a real non-Legend player stays accessible without a full profile or name result", async ({
  page,
}) => {
  await page.goto("/players/%23LQQY");
  await expect(page.getByRole("region", { name: "Player lookup" })).toContainText(
    "not in Legend I",
    { timeout: 30_000 },
  );
  await expect(page.getByText("Current trophies", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("region", { name: "Saved Legend history" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Refresh", exact: true })).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole("region", { name: "Player lookup" })).toContainText(
    "not in Legend I",
  );
  await page.goto("/?q=Lookup%20ineligible%20Clasher");
  await expect(page.locator(".search-results")).not.toContainText("#LQQY");
});

test("not-found and uncertain eligibility are different outcomes", async ({ page }) => {
  await page.goto("/players/%23LQQV");
  await expect(page.getByRole("region", { name: "Player lookup" })).toContainText(
    "Player not found",
    { timeout: 30_000 },
  );
  // Checking the same tag again cannot help; the box holds it to fix a typo.
  await expect(page.getByRole("link", { name: "Try again", exact: true })).toHaveCount(0);
  const again = page.getByRole("searchbox", { name: "Search for the right tag" });
  await expect(again).toHaveValue("#LQQV");
  await again.fill("#LQQG");
  await again.press("Enter");
  await expect(page).toHaveURL(/\/players\/%23LQQG$/);
  await expect(page.getByRole("region", { name: "Player lookup" })).toContainText(
    "could not confirm",
    { timeout: 30_000 },
  );
  await expect(page.getByText("Current trophies", { exact: true })).toHaveCount(0);
});

test("first lookup works without JavaScript and exposes a temporary failure with retry", async ({
  browser,
}) => {
  // A lookup failing only with server errors runs three more times, five
  // seconds apart, before it fails; each run waits on the fixture's slow 503s.
  test.setTimeout(120_000);
  const context = await browser.newContext({ javaScriptEnabled: false });
  try {
    const page = await context.newPage();
    await page.goto("/?q=%23LQQJ");
    await expect(page.getByRole("region", { name: "Player lookup" })).toContainText(
      "Checking this tag",
    );
    await expect(async () => {
      await page.reload();
      await expect(page.getByRole("region", { name: "Player lookup" })).toContainText(
        "could not finish checking",
      );
    }).toPass({ timeout: 100_000, intervals: [1000] });
    await expect(
      page.getByRole("link", { name: "Try again", exact: true }),
    ).toBeVisible();
  } finally {
    await context.close();
  }
});

test("a Legend I player without a Season is explained, not prepared forever", async ({
  page,
}) => {
  // Earlier tests spend all six lookup starts one address gets per minute, so
  // this last one waits for the next minute's allowance.
  test.setTimeout(150_000);
  const lookup = page.getByRole("region", { name: "Player lookup" });
  const headline =
    "Lookup Season 0 Clasher is in Legend League but hasn't played a Legend League battle this Season.";
  await expect(async () => {
    await page.goto("/players/%23LQQC");
    await expect(lookup).not.toContainText("Waiting to check");
  }).toPass({ timeout: 70_000, intervals: [5_000] });
  await expect(lookup).toContainText(headline, { timeout: 30_000 });

  // A later visit shows it at once, never reads saved data every second or
  // calls the check slow, but rereads it about once a minute.
  const reloads: Request[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/players/%23LQQC.data")) reloads.push(request);
  });
  await page.clock.install();
  await page.goto("/players/%23LQQC");
  await expect(lookup).toContainText(headline);
  // Finish loading and each reread before advancing time: while a response is
  // in flight, the page correctly skips the next minute's check.
  await page.waitForLoadState("networkidle");
  await page.clock.runFor(10_000);
  expect(reloads).toEqual([]);
  await page.clock.runFor(51_000);
  await expect.poll(() => reloads.length).toBe(1);
  await expect.poll(() => reloads.at(-1)!.timing().responseEnd).toBeGreaterThanOrEqual(0);
  await expect(lookup).toContainText(headline);
  await page.clock.runFor(61_000);
  await expect.poll(() => reloads.length).toBe(2);
  await expect.poll(() => reloads.at(-1)!.timing().responseEnd).toBeGreaterThanOrEqual(0);
  await expect(lookup).not.toContainText("taking longer");

  // A hidden tab pauses the rereads.
  await page.evaluate(() =>
    Object.defineProperty(document, "hidden", { configurable: true, value: true }),
  );
  await page.clock.runFor(125_000);
  expect(reloads).toHaveLength(2);
  await page.evaluate(() =>
    Object.defineProperty(document, "hidden", { configurable: true, value: false }),
  );
  await page.clock.runFor(61_000);
  await expect.poll(() => reloads.length).toBe(3);
  await expect.poll(() => reloads.at(-1)!.timing().responseEnd).toBeGreaterThanOrEqual(0);
  await expect(
    page.getByRole("heading", { name: "Lookup Season 0 Clasher" }),
  ).toBeVisible();
  await expect(page.locator(".player-identity")).toHaveText("#LQQCSynthetic Clan");
  await expect(page.locator(".player-trophy-count")).toHaveText("5,123");
  await expect(page.getByText("Current trophies", { exact: true })).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "Daily Legend log" })).toHaveCount(0);

  // Its unconfirmed trophies stay out of name search.
  await page.goto("/?q=Lookup%20Season%200%20Clasher");
  await expect(page.locator(".search-results")).not.toContainText("#LQQC");
});

const TIMED_OUT =
  "Couldn't refresh within one minute, so the page stopped checking. Showing saved results.";
const NOT_REFRESHED = "Couldn't refresh right now. Showing saved results.";
const UNAVAILABLE = "Saved data is still available, but the live service is unavailable.";

function refreshWork() {
  return {
    kind: "refresh-work",
    workId: randomUUID(),
    tag: "#2PP",
    state: "queued",
    progressPercent: 0,
    message: "Queued.",
    publishedAt: null,
  };
}

// Serves the saved page with a chosen check age, so only an old one refreshes
// automatically.
async function serveWithAge(page: Page, request: APIRequestContext, ageSeconds: number) {
  const html = await (await request.get("/players/%232PP")).text();
  await page.route("**/players/%232PP", async (route) =>
    route.fulfill({
      contentType: "text/html",
      body: await withServerAge(html, ageSeconds),
    }),
  );
}

for (const trigger of ["automatic", "manual"] as const) {
  test(`a ${trigger} Refresh whose submission hangs stops at the one-minute deadline`, async ({
    page,
    request,
  }) => {
    await serveWithAge(page, request, trigger === "automatic" ? 120 : 0);
    // Submissions never answer, so none spends the shared allowance.
    await page.addInitScript(() => {
      const realFetch = window.fetch;
      const submissions: AbortSignal[] = [];
      Object.assign(window, { submissions });
      window.fetch = (input, init) => {
        if (init?.method !== "POST" || !String(input).includes("/refresh")) {
          return realFetch(input, init);
        }
        const signal = init.signal!;
        submissions.push(signal);
        return new Promise((_, reject) =>
          signal.addEventListener("abort", () => reject(signal.reason)),
        );
      };
    });
    const submissions = () =>
      page.evaluate(() =>
        (window as unknown as { submissions: AbortSignal[] }).submissions.map(
          (signal) => signal.aborted,
        ),
      );
    const button = page.locator(".player-refresh-form button");
    const stopped = page.getByRole("alert").filter({ hasText: TIMED_OUT });

    await page.clock.install();
    await page.goto("/players/%232PP");
    if (trigger === "manual") {
      await page.waitForLoadState("networkidle");
      await button.click();
    }
    await expect.poll(submissions).toEqual([false]);
    await expect(button).toBeDisabled();
    await page.clock.runFor(59_000);
    await expect(stopped).toHaveCount(0);
    await page.clock.runFor(2_000);
    await expect(stopped).toBeVisible();
    await expect(button).toHaveText("Refresh");
    await expect(button).toBeEnabled();
    expect(await submissions()).toEqual([true]);
  });
}

for (const [stall, read] of [
  ["headers", "never sends headers"],
  ["body", "never finishes its body"],
  ["late", "answers after the deadline but before its timer runs"],
] as const) {
  test(`a Refresh status read that ${read} stops at the one-minute deadline`, async ({
    page,
    request,
  }) => {
    await serveWithAge(page, request, 0);
    const work = refreshWork();
    // The Refresh itself is faked, so it spends none of the shared allowance.
    await page.route("**/resources/players/*/refresh*", (route) =>
      route.fulfill({
        status: 202,
        contentType: "text/x-script",
        headers: { "X-Remix-Response": "yes" },
        body: encodePageData({ data: work }),
      }),
    );
    // Status reads never send headers, never finish their body, or answer a
    // completed Refresh only after the deadline.
    await page.addInitScript(
      ({ stall, complete }) => {
        const realFetch = window.fetch;
        const reads: AbortSignal[] = [];
        Object.assign(window, { statusReads: reads });
        window.fetch = (input, init) => {
          if (!String(input).includes("/refresh?workId=")) return realFetch(input, init);
          const signal = init!.signal!;
          reads.push(signal);
          const stop = (fail: (reason: unknown) => void) =>
            signal.addEventListener("abort", () => fail(signal.reason));
          if (stall === "headers") return new Promise((_, reject) => stop(reject));
          if (stall === "body") {
            const body = new ReadableStream({
              start: (stream) => stop((r) => stream.error(r)),
            });
            return Promise.resolve(new Response(body));
          }
          return new Promise((resolve) =>
            Object.assign(window, {
              answerLate: () => {
                const response = new Response(JSON.stringify(complete));
                const json = response.json.bind(response);
                response.json = () =>
                  json().finally(() => Object.assign(window, { answered: true }));
                resolve(response);
              },
            }),
          );
        };
      },
      {
        stall,
        complete: {
          ...work,
          kind: "refresh-status",
          state: "complete",
          progressPercent: 100,
          message: "Complete.",
          player: null,
        },
      },
    );
    const reads = () =>
      page.evaluate(() =>
        (window as unknown as { statusReads: AbortSignal[] }).statusReads.map(
          (signal) => signal.aborted,
        ),
      );
    const stopped = page.getByRole("alert").filter({ hasText: TIMED_OUT });
    const refresh = page.getByRole("region", { name: "Player refresh" });

    await page.clock.install();
    await page.goto("/players/%232PP");
    await page.waitForLoadState("networkidle");
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await expect.poll(reads).toEqual([false]);
    if (stall === "late") {
      await page.clock.runFor(30_000);
      const now = await page.evaluate(() => Date.now());
      await page.clock.setSystemTime(now + 31_000);
      await page.evaluate(() =>
        (window as unknown as { answerLate(): void }).answerLate(),
      );
      await expect.poll(() => page.evaluate(() => "answered" in window)).toBe(true);
      await expect(refresh).toContainText("Refreshing…");
      await expect(refresh).not.toContainText("Updated.");
      await page.clock.runFor(31_000);
    } else {
      await page.clock.runFor(59_000);
      await expect(stopped).toHaveCount(0);
      await page.clock.runFor(2_000);
    }
    await expect(stopped).toBeVisible();
    await expect(refresh).toContainText(NOT_REFRESHED);
    await expect(refresh.getByRole("progressbar")).toHaveCount(0);

    await page.clock.runFor(240_000);
    await expect(stopped).toBeVisible();
    await expect(refresh).not.toContainText("Updated.");
    expect(await reads()).toEqual([stall !== "late"]);
  });
}

test("a failed Refresh status read replaces Refreshing… with saved results", async ({
  page,
  request,
}) => {
  await serveWithAge(page, request, 0);
  // The Refresh itself is faked, so it spends none of the shared allowance.
  await page.route("**/resources/players/*/refresh*", (route) =>
    route.request().method() === "POST"
      ? route.fulfill({
          status: 202,
          contentType: "text/x-script",
          headers: { "X-Remix-Response": "yes" },
          body: encodePageData({ data: refreshWork() }),
        })
      : route.fulfill({
          status: 503,
          json: { error: { code: "unavailable", message: UNAVAILABLE } },
        }),
  );
  const refresh = page.getByRole("region", { name: "Player refresh" });

  await page.goto("/players/%232PP");
  await page.waitForLoadState("networkidle");
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  await expect(page.getByRole("alert").filter({ hasText: UNAVAILABLE })).toBeVisible();
  await expect(refresh).toContainText(NOT_REFRESHED);
  await expect(refresh).not.toContainText("Refreshing…");
  await expect(refresh.getByRole("progressbar")).toHaveCount(0);
});

test("a Refresh that fails after an earlier one completed does not say Updated.", async ({
  page,
  request,
}) => {
  await serveWithAge(page, request, 0);
  const work = refreshWork();
  let submissions = 0;
  // Both Refreshes are faked, so they spend none of the shared allowance.
  await page.route("**/resources/players/*/refresh*", (route) => {
    if (route.request().method() !== "POST") {
      return route.fulfill({
        json: {
          ...work,
          kind: "refresh-status",
          state: "complete",
          progressPercent: 100,
          message: "Complete.",
          player: null,
        },
      });
    }
    submissions++;
    return route.fulfill({
      status: submissions === 1 ? 202 : 503,
      contentType: "text/x-script",
      headers: { "X-Remix-Response": "yes" },
      body: encodePageData({
        data:
          submissions === 1
            ? work
            : { error: { code: "unavailable", message: UNAVAILABLE } },
      }),
    });
  });
  const refresh = page.getByRole("region", { name: "Player refresh" });
  const button = page.getByRole("button", { name: "Refresh", exact: true });

  await page.goto("/players/%232PP");
  await page.waitForLoadState("networkidle");
  await button.click();
  await expect(refresh).toContainText("Updated.");
  await button.click();
  await expect(page.getByRole("alert").filter({ hasText: UNAVAILABLE })).toBeVisible();
  await expect(refresh).toHaveCount(0);
  expect(submissions).toBe(2);
});
