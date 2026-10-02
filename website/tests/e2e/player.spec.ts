import { expect, test } from "@playwright/test";

for (const ageMs of [30_000, 60_000, 60_001, 120_000]) {
  test(`profile visit refreshes once only when its saved check is older than 60 seconds (${ageMs}ms)`, async ({
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
    await page.route("**/players/%232PP", (route) =>
      route.fulfill({ contentType: "text/html", body: html }),
    );
    await page.clock.setFixedTime(new Date(Date.parse(observedAt) + ageMs));
    const submissions: string[] = [];
    await page.route("**/resources/players/*/refresh*", async (route) => {
      if (route.request().method() !== "POST") return route.continue();
      submissions.push(route.request().postData() ?? "");
      // Exercise the existing refusal display without spending the shared allowance.
      await route.continue({ postData: "idempotencyKey=invalid" });
    });

    await page.goto("/players/%232PP");
    const automaticCount = ageMs > 60_000 ? 1 : 0;
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
    await page.getByRole("button", { name: "Refresh", exact: true }).click();
    await expect(refusal).toBeVisible();
    await page.waitForLoadState("networkidle");
    expect(submissions).toHaveLength(automaticCount + 1);
    const key = await page.locator('input[name="idempotencyKey"]').inputValue();
    for (const submission of submissions) {
      expect(new URLSearchParams(submission).get("idempotencyKey")).toBe(key);
    }

    // A full reload retains its existing unconditional Refresh behavior, and
    // stale data must not add a second request on top of it.
    await page.reload();
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
  let opponent: string;
  let savedData: string;
  let dataType: string;
  try {
    const savedPage = await savedContext.newPage();
    const response = await savedPage.goto("/players/%232PP");
    html = await response!.text();
    observedAt = (await savedPage.locator(".player-updated").getAttribute("datetime"))!;
    opponent = (await savedPage
      .locator(".battle-slot-attack .battle-opponent strong")
      .first()
      .textContent())!;
    const dataResponse = await savedContext.request.get("/players/%232PP.data");
    savedData = await dataResponse.text();
    dataType = dataResponse.headers()["content-type"];
  } finally {
    await savedContext.close();
  }
  expect(savedData).toContain(observedAt);
  const earlierAt = new Date(Date.parse(observedAt) - 1).toISOString();
  const attacks = page.locator(".battle-slot-attack", { hasText: opponent });

  // Before processing, page reads return the earlier check. Completion carries
  // the processed profile without its battles; only later reads include them.
  let completed = false;
  let readsAfterCompletion = 0;
  await page.route("**/players/%232PP", (route) =>
    route.fulfill({
      contentType: "text/html",
      body: html.replaceAll(observedAt, earlierAt),
    }),
  );
  await page.route("**/players/%232PP.data*", (route) => {
    const processed = completed && readsAfterCompletion++ > 0;
    return route.fulfill({
      contentType: dataType,
      body: processed ? savedData : savedData.replaceAll(observedAt, earlierAt),
    });
  });
  await page.route("**/resources/players/*/refresh?workId=*", async (route) => {
    const response = await route.fetch();
    const status = await response.json();
    if (status.state === "complete") {
      status.player.profile.freshness.observedAt = observedAt;
      status.player.currentDay = null;
      status.player.recentDays = [];
      status.player.seasonDays = [];
      completed = true;
    }
    await route.fulfill({ response, json: status });
  });
  await page.clock.setFixedTime(new Date(Date.parse(earlierAt) + 120_000));

  await page.goto("/players/%232PP");
  await expect(attacks).not.toHaveCount(0);
  await expect.poll(() => readsAfterCompletion, { timeout: 30_000 }).toBeGreaterThan(1);
  await expect(page.locator(".player-updated")).toHaveAttribute("datetime", observedAt);
  await expect(attacks).not.toHaveCount(0);
});

test("player page canonicalizes the tag and shows collected profile data", async ({
  page,
}) => {
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
  await page.goto("/players/%232PP");

  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    ),
  ).toBe(0);
});

test("season navigation clears refresh state for the same player", async ({ page }) => {
  await page.goto("/players/%232PP");
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
