import { expect, test } from "@playwright/test";

import { expectNoSeriousAccessibilityViolations } from "./helpers/account";

test("army analytics loads the real backend's current honest state", async ({ page }) => {
  const response = await page.goto("/analytics/armies?saved=1&season=current");

  expect([200, 404]).toContain(response?.status());
  await expect(
    page.getByRole("heading", { name: "Analytics", exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("form", { name: "Army analytics filters" })).toBeVisible();
  await expect(
    page.getByRole("region", { name: "Army statistics" }).or(
      page.getByRole("heading", {
        name: /^(A new season is underway|No army stats for this season yet|Army types are being prepared)$/,
      }),
    ),
  ).toBeVisible();
  await expectNoSeriousAccessibilityViolations(page);
});

test("captured preview reconciles counts, updates filters and reverses sorting", async ({
  page,
}) => {
  const response = await page.goto(
    "/analytics/armies?recent=1&population=top-100&category=troops&sort=usage-rate",
  );

  expect(response?.status()).toBe(200);
  const coverage = page.getByLabel("Battle coverage");
  await expect(coverage).toContainText(/Battle records\s*1,600\s*Recorded/);
  await expect(coverage).toContainText(/Records included\s*1,600/);
  await expect(coverage).toContainText(/Records excluded\s*0/);

  await page.getByLabel("Unit Type").selectOption("spells");
  await expect(page).toHaveURL(/category=spells/);
  await expect(page.getByRole("heading", { name: "Spells", exact: true })).toBeVisible();

  await page
    .getByRole("form", { name: "Army analytics filters" })
    .getByLabel("Players")
    .selectOption("top-50");
  await expect(page).toHaveURL(/population=top-50/);

  const table = page.getByRole("table", { name: "Army analytics results" });
  await table.getByRole("button", { name: "Sort by Name, A to Z" }).click();
  await expect(table.getByRole("columnheader", { name: /Name/ })).toHaveAttribute(
    "aria-sort",
    "ascending",
  );
  const ascendingFirst = await table
    .getByRole("row")
    .nth(1)
    .getByRole("rowheader")
    .innerText();

  await table.getByRole("button", { name: "Sort by Name, Z to A" }).click();
  await expect(table.getByRole("columnheader", { name: /Name/ })).toHaveAttribute(
    "aria-sort",
    "descending",
  );
  const descendingFirst = await table
    .getByRole("row")
    .nth(1)
    .getByRole("rowheader")
    .innerText();
  expect(descendingFirst).not.toBe(ascendingFirst);
  await expectNoSeriousAccessibilityViolations(page);
});

test("the saved-data page shows attacks over the whole season with no day range", async ({
  page,
}) => {
  await page.goto("/analytics/armies?saved=1&season=current&lens=defense&start_day=3");
  const form = page.getByRole("form", { name: "Army analytics filters" });
  await expect(form.getByText("Defenses", { exact: true })).toHaveCount(0);
  await expect(form.getByLabel("From Legend day")).toHaveCount(0);
  await expect(form.getByLabel("To Legend day")).toHaveCount(0);
  const season = form.getByRole("combobox", { name: "Season", exact: true });
  await expect(season).toBeVisible();
  await expect(season).toHaveValue("current");
  await expect(form.getByLabel("Unit Type")).toContainText("Hero + Equipment");
  await expect(form.getByLabel("Unit Type")).toContainText("Hero + Both Equipments");
});

test("Clan Castle Troops is a unit type of its own", async ({ page }) => {
  await page.goto("/analytics/armies?recent=1&category=troops");
  const unitType = page.getByLabel("Unit Type");
  const rows = page
    .getByRole("table", { name: "Army analytics results" })
    .getByRole("row");
  await expect(page.getByRole("heading", { name: "Troops", exact: true })).toBeVisible();
  await expect(rows).toHaveCount(32);
  await expect(page.getByRole("checkbox")).toHaveCount(0);

  await unitType.selectOption("cc-troops");
  await expect(page).toHaveURL(/category=cc-troops/);
  await expect(page.getByRole("heading", { name: "Clan Castle Troops" })).toBeVisible();
  await expect(rows).toHaveCount(24);

  await unitType.selectOption("troops");
  await expect(page.getByRole("heading", { name: "Troops", exact: true })).toBeVisible();
  await expect(rows).toHaveCount(32);
});

test("unit types change when JavaScript is off", async ({ browser }) => {
  const context = await browser.newContext({ javaScriptEnabled: false });
  try {
    const page = await context.newPage();
    await page.goto("/analytics/armies?recent=1&category=troops");
    const apply = page.getByRole("button", { name: "Apply filters" });
    await expect(apply).toBeVisible();

    await page.getByLabel("Unit Type").selectOption("cc-troops");
    await apply.click();
    await expect(page).toHaveURL(/category=cc-troops/);
    await expect(page.getByRole("heading", { name: "Clan Castle Troops" })).toBeVisible();
  } finally {
    await context.close();
  }
});

test("large army view keeps every row and shows the browser's local time", async ({
  browser,
}) => {
  const context = await browser.newContext({ timezoneId: "Asia/Kolkata" });
  try {
    const page = await context.newPage();
    await page.goto(
      "/analytics/armies?recent=1&population=top-200&category=cc-composition&sort=usage-rate",
    );
    await expect(
      page.getByRole("table", { name: "Army analytics results" }).getByRole("row"),
    ).toHaveCount(49);

    const timestamp = page.getByLabel("Real battle data").locator("time");
    const value = await timestamp.getAttribute("datetime");
    expect(value).not.toBeNull();
    const expected = await page.evaluate(
      (date) =>
        new Intl.DateTimeFormat(undefined, {
          day: "numeric",
          month: "short",
          year: "numeric",
          hour: "2-digit",
          minute: "2-digit",
          timeZoneName: "short",
        }).format(new Date(date!)),
      value,
    );
    await expect(timestamp).toHaveText(expected);
  } finally {
    await context.close();
  }
});

test("missing historical summary stays unavailable with legacy filters in the URL", async ({
  page,
}) => {
  const response = await page.goto(
    "/analytics/armies?season=missing-history-126&category=troops&start_day=1&end_day=1&population=top-5",
  );
  expect(response?.status()).toBe(404);
  await expect(
    page.getByText("Army analytics are unavailable for the selected Legend days."),
  ).toBeVisible();
  await expect(page.getByRole("table")).toHaveCount(0);

  await page.goto("/analytics/armies?season=missing-history-126&category=troops&cc=1");
  const unitType = page.getByLabel("Unit Type");
  await expect(unitType).toHaveValue("cc-troops");
  await expect(page.getByRole("table")).toHaveCount(0);
  await unitType.selectOption("troops");
  await expect(page).not.toHaveURL(/cc=1/);
  await expectNoSeriousAccessibilityViolations(page);

  // Armies are counted from the current Season on.
  await page.goto("/analytics/armies?season=missing-history-126&category=army-types");
  await expect(
    page.getByRole("heading", { name: "Armies are not available for this season" }),
  ).toBeVisible();
});

test("retired IDs render honest labels, quantities and stars before and after naming", async ({
  page,
}) => {
  const season = process.env.CLASHLENS_E2E_HISTORY_SEASON;
  const unknown = process.env.CLASHLENS_E2E_HISTORY_NAMES === "unknown";
  test.skip(!season, "Requires the retired-history PostgreSQL restore fixture.");
  for (const [category, namespace, id, quantity] of [
    ["troops", "troop", 900, 5],
    ["siege", "siege", 901, 1],
    ["spells", "spell", 900, 1],
    ["heroes", "hero", 900, 1],
    ["pets", "pet", 900, 1],
    ["equipment", "equipment", 900, 1],
  ] as const) {
    const response = await page.goto(
      `/analytics/armies?season=${season}&category=${category}`,
    );
    expect(response?.status()).toBe(200);
    const kind =
      category === "troops" || category === "siege" ? "troop or siege" : namespace;
    const label = unknown ? `Unknown ${kind} (ID ${id})` : `Named ${namespace}`;
    const row = page.getByRole("row").filter({ hasText: label });
    await expect(row).toContainText("1 / 2");
    await expect(row).toContainText("50.0%");
    await expect(row.getByRole("cell").first()).toHaveText(String(quantity));
    await expect(row.getByRole("cell").nth(3)).toContainText("0 battles");
    await expect(row.getByRole("cell").nth(4)).toContainText("1 battle");
    await expect(row.getByRole("cell").nth(5)).toContainText("0 battles");
    await expect(page.getByRole("columnheader", { name: "Quantity" })).toBeVisible();
    await expect(page.getByRole("columnheader", { name: /Avg\. stars/ })).toHaveCount(0);
    await expect(page.getByLabel("From Legend day")).toHaveCount(0);
    if (!unknown && (category === "troops" || category === "siege")) {
      await expect(
        page
          .getByRole("row")
          .filter({ hasText: category === "troops" ? "Named siege" : "Named troop" }),
      ).toHaveCount(0);
    }
    await expectNoSeriousAccessibilityViolations(page);
  }
});
