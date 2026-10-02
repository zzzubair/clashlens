import { randomUUID } from "node:crypto";

import { expect, test, type Page } from "@playwright/test";

import type { PlayerPage, RankedBattleEvent } from "../../app/lib/contracts";

// Sends Refresh submissions with an invalid key, so the server refuses them
// before they spend the per-visitor allowance (6 a minute) that lookups share.
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
function decodePageData(text: string) {
  const values: unknown[] = JSON.parse(text.split("\n")[0]);
  const decode = (index: number): unknown => {
    if (index < 0) return index === -5 ? null : undefined;
    const value = values[index];
    if (Array.isArray(value)) return value.map(decode);
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

// Sets the server-calculated check age in React Router's serialized page data.
function withServerAge(html: string, ageSeconds: number) {
  const served = html.replace(
    /streamController\.enqueue\(("(?:[^"\\]|\\.)*")\)/g,
    (call, literal: string) => {
      const [head, ...rest] = (JSON.parse(literal) as string).split("\n");
      const values: unknown[] = JSON.parse(head);
      const key = `_${values.indexOf("ageSeconds")}`;
      if (key === "_-1") return call;
      const age = values.push(ageSeconds) - 1;
      for (const value of values) {
        if (value && typeof value === "object" && key in value)
          (value as Record<string, number>)[key] = age;
      }
      const text = [JSON.stringify(values), ...rest].join("\n");
      const escaped = JSON.stringify(text).replace(
        /[&<>\u2028\u2029]/g,
        (character) => `\\u${character.charCodeAt(0).toString(16).padStart(4, "0")}`,
      );
      return `streamController.enqueue(${escaped})`;
    },
  );
  expect(served).not.toBe(html);
  return served;
}

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
    const served = withServerAge(html.toString(), ageSeconds);
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

    // A full reload retains its existing unconditional Refresh behavior, and
    // stale data must not add a second request on top of it.
    const reloaded = refreshSubmitted(page);
    await page.reload();
    await reloaded;
    await expect(refusal).toBeVisible();
    await page.waitForLoadState("networkidle");
    expect(submissions).toHaveLength(automaticCount + 2);
  });
}

test("a battle processed shortly after a completed Refresh reaches the open page", async ({
  page,
  browser,
  baseURL,
}) => {
  const savedContext = await browser.newContext({ javaScriptEnabled: false, baseURL });
  let html: string;
  let observedAt: string;
  let savedData: string;
  let dataType: string;
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
    army: null,
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
  await page.route("**/players/%232PP", (route) =>
    route.fulfill({
      contentType: "text/html",
      body: withServerAge(html.replaceAll(observedAt, earlierAt), 120),
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

test("season navigation clears refresh state for the same player", async ({ page }) => {
  // Only the manual Refresh below may spend the shared allowance.
  let manual = false;
  await refuseRefreshes(page, () => {
    const allowed = manual;
    manual = false;
    return allowed;
  });
  await page.goto("/players/%232PP");
  await page.waitForLoadState("networkidle");
  manual = true;
  await page.getByRole("button", { name: "Refresh", exact: true }).click();
  const refresh = page.getByRole("region", { name: "Player refresh" });
  await expect(refresh).toBeVisible();

  const seasons = page.getByRole("navigation", { name: "Historical seasons" });
  await seasons.getByRole("link").first().click();
  await expect(page).toHaveURL(/\/players\/%232PP\?season=/);
  await expect(refresh).toHaveCount(0);

  await seasons.getByRole("link", { name: "Current season" }).click();
  await expect(page).toHaveURL(/\/players\/%232PP$/);
  await expect(refresh).toHaveCount(0);
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
  await expect(page.getByText("Now tracking in Legend I.", { exact: true })).toBeVisible({
    timeout: 30_000,
  });
  await expect(
    page.getByRole("heading", { name: "Lookup eligible Clasher" }),
  ).toBeVisible();
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
  await expect(page.getByRole("link", { name: "Try again", exact: true })).toBeVisible();
  await page.goto("/players/%23LQQG");
  await expect(page.getByRole("region", { name: "Player lookup" })).toContainText(
    "could not confirm",
    { timeout: 30_000 },
  );
  await expect(page.getByText("Current trophies", { exact: true })).toHaveCount(0);
});

test("first lookup works without JavaScript and exposes a temporary failure with retry", async ({
  browser,
}) => {
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
    }).toPass({ timeout: 30_000, intervals: [1000] });
    await expect(
      page.getByRole("link", { name: "Try again", exact: true }),
    ).toBeVisible();
  } finally {
    await context.close();
  }
});
