import { expect, test, type Locator, type Page } from "@playwright/test";

// Real Clash names at their limits: styled letters, right-to-left, wide scripts,
// Thai marks above and below, emoji and one letter.
const worstCaseNames = [
  "ᴷᴵᴺᴳ ᴼᶠ ᴸᴱᴳᴱᴺᴰ",
  "محمد الأسطورة 👑",
  "王者荣耀最强部落战神无敌",
  "ผู้เล่นเทพตำนานี้",
  "J",
];

// Suggestions and submitted searches come from the real backend; only the saved
// synthetic names and clan are swapped for worst-case ones and a worst-case
// profile is added on the way to the browser.
async function serveWorstCaseSearch(page: Page) {
  await page.route(/\.data\?/, async (route) => {
    const response = await route.fetch();
    let index = 0;
    // React Router packs the response into one flat array: each object maps
    // "_<key position>" to its value's position. New values go on the end.
    const values: unknown[] = JSON.parse(
      (await response.text())
        .replace(/"Synthetic Clasher \d{3}"/g, () =>
          JSON.stringify(worstCaseNames[index++ % worstCaseNames.length]),
        )
        .replaceAll('"Synthetic Clan"', JSON.stringify("中华联盟总部永远第一名")),
    );
    const add = (value: unknown) => values.push(value) - 1;
    const usersKey = `_${values.indexOf("users")}`;
    const search = values.find(
      (value): value is Record<string, number> =>
        typeof value === "object" && value !== null && usersKey in value,
    )!;
    // Usernames allow 32 characters with no spaces.
    (values[search[usersKey]] as number[]).unshift(
      add({
        [`_${add("username")}`]: add("m".repeat(32)),
        [`_${add("displayName")}`]: add("M".repeat(80)),
        [`_${add("linkedPlayerCount")}`]: add(1),
      }),
    );
    await route.fulfill({ response, body: JSON.stringify(values) });
  });
}

async function typeSearch(page: Page, input: Locator) {
  const suggestions = page.getByRole("region", {
    name: "Player and profile search suggestions",
  });
  // Typing before the page has started up is lost, so type again until it lands.
  await expect(async () => {
    await input.fill("Synthetic Clasher 00");
    await expect(suggestions).toContainText(worstCaseNames[1], { timeout: 2_000 });
  }).toPass();
  await expect(suggestions).toContainText("@" + "m".repeat(32));
  // Nothing inside the suggestions scrolls sideways either.
  expect(await suggestions.evaluate((list) => list.scrollWidth - list.clientWidth)).toBe(
    0,
  );
}

async function expectNoSidewaysScroll(page: Page) {
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
    ),
  ).toBeLessThanOrEqual(0);
}

// Scroll to the last suggestion like a finger would, then check a tap on its
// middle lands on it rather than off screen or on something drawn over it.
async function expectLastSuggestionTappable(page: Page) {
  const reached = await page
    .getByTestId("search-suggestion")
    .last()
    .evaluate((link) => {
      link.scrollIntoView({ block: "nearest" });
      const box = link.getBoundingClientRect();
      const hit = document.elementFromPoint(
        box.left + box.width / 2,
        box.top + box.height / 2,
      );
      return hit !== null && link.contains(hit);
    });
  expect(reached).toBe(true);
}

test("home suggestions fit a 320 px phone with worst-case names", async ({ page }) => {
  await serveWorstCaseSearch(page);
  await page.setViewportSize({ width: 320, height: 568 });
  await page.goto("/");
  await typeSearch(
    page,
    page.getByRole("searchbox", { name: "Search players and Clash Lens profiles" }),
  );
  await expectLastSuggestionTappable(page);
  await expectNoSidewaysScroll(page);
});

for (const viewport of [
  { width: 750, height: 342 },
  { width: 640, height: 400 },
]) {
  test(`header search stays on screen at ${viewport.width}x${viewport.height}`, async ({
    page,
  }) => {
    await serveWorstCaseSearch(page);
    await page.setViewportSize(viewport);
    await page.goto("/about");
    const input = page.getByRole("searchbox", {
      name: "Search players and Clash Lens profiles",
    });
    await expect(async () => {
      if (!(await input.isVisible()))
        await page.getByRole("button", { name: "Search players" }).click();
      await expect(input).toBeFocused({ timeout: 1_000 });
    }).toPass();
    await typeSearch(page, input);
    await expectLastSuggestionTappable(page);
    // The list scrolls inside the panel; the panel and its input stay on screen.
    const panel = (await page.locator("#header-search-panel").boundingBox())!;
    expect(panel.y + panel.height).toBeLessThanOrEqual(viewport.height);
    const field = (await input.boundingBox())!;
    expect(field.y).toBeGreaterThanOrEqual(0);
    expect(field.y + field.height).toBeLessThanOrEqual(viewport.height);
    await expectNoSidewaysScroll(page);
  });
}

test("a long profile name wraps in search results on a 320 px phone", async ({
  page,
}) => {
  await serveWorstCaseSearch(page);
  await page.setViewportSize({ width: 320, height: 568 });
  await page.goto("/");
  const input = page.getByRole("searchbox", {
    name: "Search players and Clash Lens profiles",
  });
  // Suggestions showing means the page is ready to search without reloading.
  await typeSearch(page, input);
  await input.press("Enter");
  const result = page.locator(".search-result-profile");
  await expect(result).toContainText("@" + "m".repeat(32));
  await expectNoSidewaysScroll(page);
  const card = (await result.boundingBox())!;
  expect(card.x + card.width).toBeLessThanOrEqual(320);
});
