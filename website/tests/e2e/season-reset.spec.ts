import { expect, test } from "@playwright/test";
import type { GroupsLoaderData } from "../../app/routes/account.groups";
import type { GroupCompareLoaderData } from "../../app/routes/account.groups.$groupId";
import type { HomeLoaderData } from "../../app/routes/home";
import type { PlayerLoaderData } from "../../app/routes/player";
import { worstComparison } from "../fixtures/worst-case-accounts";
import { expectNoPageErrors, trackPageErrors } from "./helpers/account";

// Decode and encode the actual React Router single-fetch response. Streamed
// past Seasons are omitted; these checks concern saved current data only.
function decode(text: string) {
  const values: unknown[] = JSON.parse(text.split("\n")[0]);
  const item = (index: number): unknown => {
    if (index < 0) return index === -5 ? null : undefined;
    const value = values[index];
    if (Array.isArray(value)) return value[0] === "P" ? undefined : value.map(item);
    if (!value || typeof value !== "object") return value;
    return Object.fromEntries(
      Object.entries(value).map(([key, index]) => [
        values[Number(key.slice(1))],
        item(index as number),
      ]),
    );
  };
  return item(0) as Record<string, { data: HomeLoaderData & PlayerLoaderData }>;
}

test("Live keeps its way to page 1 when Reset makes the open page unavailable", async ({
  page,
}) => {
  const response = await page.request.get("/leaderboards/tracked.data?view=live&page=1");
  expect(response.ok()).toBe(true);
  const saved = decode(await response.text());
  const route = Object.values(saved).find(({ data }) => data?.leaderboard)!;
  route.data.leaderboard!.generatedAt = BEFORE;
  route.data.leaderboard!.entries = [
    { ...route.data.leaderboard!.entries[0], trophies: 6400 },
  ];
  let expired = false;
  await page.route("**/leaderboards/tracked.data*", (request) =>
    request.fulfill({
      status: expired ? 404 : 200,
      // React Router treats an error status without this header as a missing route.
      headers: { "X-Remix-Response": "yes" },
      contentType: "text/x-script",
      body: encode(
        expired
          ? Object.fromEntries(
              Object.entries(saved).map(([key, value]) => [
                key,
                value === route
                  ? {
                      data: {
                        ...value.data,
                        leaderboard: null,
                        pageUnavailableUrl: "/leaderboards/tracked?view=live&page=1",
                      },
                    }
                  : value,
              ]),
            )
          : saved,
      ),
    }),
  );
  await page.goto("/about");
  await page.clock.install({ time: new Date("2030-01-01T00:00:00Z") });
  await page.evaluate(() => {
    history.pushState(null, "", "/leaderboards/tracked?view=live&page=2");
    dispatchEvent(new PopStateEvent("popstate"));
  });
  await expect(page.locator(".rankings-page")).toContainText("6,400");
  expired = true;
  await page.clock.runFor(30_000);
  await expect(page.getByText("This standings page is unavailable")).toBeVisible();
  await expect(page.getByRole("link", { name: "Go to page 1" })).toHaveAttribute(
    "href",
    "/leaderboards/tracked?view=live&page=1",
  );
  await expect(page.getByText("Loading the new Season's rankings…")).not.toBeVisible();
});

function encode(data: unknown) {
  const values: unknown[] = [];
  const item = (value: unknown): number => {
    if (value === null) return -5;
    if (value === undefined) return -7;
    if (typeof value !== "object") return values.push(value) - 1;
    const index = values.push(null) - 1;
    values[index] = Array.isArray(value)
      ? value.map(item)
      : Object.fromEntries(
          Object.entries(value).map(([key, value]) => [`_${item(key)}`, item(value)]),
        );
    return index;
  };
  item(data);
  return `${JSON.stringify(values)}\n`;
}

const BEFORE = "2026-10-05T04:59:30Z";
const AFTER = "2026-10-05T05:00:30Z";

for (const view of ["list", "comparison"] as const) {
  test(`an open group ${view} stops showing trophies when their Season ends`, async ({
    page,
  }) => {
    const errors = trackPageErrors(page);
    const comparison = worstComparison(7);
    comparison.season = String(Date.parse("2026-09-07T05:00:00Z") / 1000);
    comparison.players = [
      {
        ...comparison.players[0],
        tag: "#2PP",
        trophies: 6400,
        seasonResetPending: false,
      },
    ];
    const path =
      view === "list" ? "/account/groups" : `/account/groups/${comparison.groupId}`;
    const routeId =
      view === "list" ? "routes/account.groups" : "routes/account.groups.$groupId";
    const key = "3be934b5-68fa-4741-8c7b-e03592e4ad70";
    const data: GroupsLoaderData | GroupCompareLoaderData =
      view === "list"
        ? {
            season: comparison.season,
            groups: [
              {
                groupId: comparison.groupId,
                name: "Season watch",
                tags: ["#2PP"],
                players: [{ ...comparison.players[0], state: "tracking" }],
              },
            ],
            createIdempotencyKey: key,
            updateIdempotencyKeys: { [comparison.groupId]: key },
            deleteIdempotencyKeys: { [comparison.groupId]: key },
            addIdempotencyKeys: { [comparison.groupId]: key },
            removeIdempotencyKeys: { [comparison.groupId]: { "#2PP": key } },
            error: null,
          }
        : {
            comparison,
            days: 7,
            sort: "trophies",
            notFound: false,
            tooLarge: null,
            error: null,
          };
    // These checks concern the browser's expiry, not when the collector finishes
    // accepting a fake player's opening-day battles. Keep the saved Season on reread.
    let reads = 0;
    const saved = {
      root: {
        data: {
          loggedIn: false,
          accountLabel: null,
          accountUsername: null,
          logoutIdempotencyKey: null,
          updateStatus: null,
        },
      },
      [routeId]: { data },
    };
    await page.route(`**${path}.data*`, (route) => {
      reads++;
      return route.fulfill({
        contentType: "text/x-script",
        body: encode(saved),
      });
    });
    await page.goto("/about");
    await page.clock.install({ time: new Date("2026-09-07T05:00:00Z") });
    await page.evaluate((path) => {
      history.pushState(null, "", path);
      dispatchEvent(new PopStateEvent("popstate"));
    }, path);
    const trophies =
      view === "list"
        ? page
            .getByRole("list", { name: "Players in Season watch" })
            .getByRole("listitem")
            .filter({ hasText: "#2PP" })
            .locator(".group-member-detail")
        : page
            .getByRole("row")
            .filter({ hasText: "#2PP" })
            .locator('[data-label="Trophies now"]');
    await expect(trophies).toContainText("6,400");
    expect(reads).toBe(1);

    // Each jump stays below the browser clock's 2**31 - 1 ms limit.
    await page.clock.fastForward(14 * 86_400_000);
    await expect(trophies).toContainText("6,400");
    expect(reads).toBe(1);
    await page.clock.fastForward(14 * 86_400_000);
    await expect(trophies).toHaveText("Waiting for this player's Season reset");
    await expect.poll(() => reads).toBe(2);
    await expect(trophies).toHaveText("Waiting for this player's Season reset");
    expectNoPageErrors(errors);
  });
}

for (const path of ["/", "/leaderboards/tracked?view=live&page=1", "/players/%232PP"]) {
  test(`${path} withholds expired current trophies during Reset and follows October recovery`, async ({
    page,
  }) => {
    const dataPath =
      path === "/" ? "/_.data" : path.replace(/^([^?]*)(.*)$/, "$1.data$2");
    const response = await page.request.get(dataPath);
    expect(response.ok()).toBe(true);
    const saved = decode(await response.text());
    const data = Object.values(saved).find(
      ({ data }) => data?.leaderboard || data?.player,
    )!.data;
    const player = data.player;
    const board = data.leaderboard;
    const originalEntry = board?.entries[0];
    if (player) {
      player.profile.trophies = 6400;
      player.profile.freshness = { state: "fresh", observedAt: BEFORE, ageSeconds: 0 };
      player.profile.seasonResetPending = false;
      data.lookup = { tag: player.tag, state: "tracking", reason: "pending" };
      data.refreshStatus = null;
    } else {
      expect(board?.entries.length).toBeGreaterThan(0);
      board!.generatedAt = BEFORE;
      board!.entries = [board!.entries[0]];
      board!.entries[0].trophies = 6400;
      board!.seasonResetPending = 0;
    }
    const initial = encode(saved);
    let stage = "before";
    let reads = 0;
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    await page.route(`**${dataPath.split("?")[0]}*`, async (route) => {
      if (stage === "before")
        return route.fulfill({ contentType: "text/x-script", body: initial });
      reads++;
      if (stage === "held") await held;
      const waiting = stage !== "recovered";
      if (player) {
        player.profile.freshness.observedAt = AFTER;
        player.profile.seasonResetPending = waiting;
        player.profile.trophies = waiting ? 6400 : 5000;
      } else {
        board!.generatedAt = AFTER;
        board!.seasonResetPending = waiting ? 1 : 0;
        board!.entries = waiting ? [] : [{ ...originalEntry!, trophies: 5000 }];
      }
      return route.fulfill({ contentType: "text/x-script", body: encode(saved) });
    });
    let refreshes = 0;
    await page.route("**/resources/players/*/refresh*", (route) => {
      if (route.request().method() === "POST") refreshes++;
      return route.fulfill({
        status: 503,
        json: { error: { code: "unavailable", message: "Unavailable" } },
      });
    });
    await page.goto("/about");
    await page.clock.install({ time: new Date("2030-01-01T00:00:00Z") });
    // Enter through client navigation so the controlled loader data mounts fresh.
    await page.evaluate((path) => {
      history.pushState(null, "", path);
      dispatchEvent(new PopStateEvent("popstate"));
    }, path);
    const trophies = player
      ? page.locator(".player-trophy-count")
      : page.locator(path === "/" ? ".home-leaderboard" : ".rankings-page");
    await expect(trophies).toContainText("6,400");
    stage = "held";
    await page.clock.runFor(30_000);
    await expect.poll(() => reads).toBe(1);
    await expect(trophies).not.toContainText("6,400");
    stage = "waiting";
    const waitingResponse = page.waitForResponse((response) =>
      response.url().includes(dataPath.split("?")[0]),
    );
    release();
    await waitingResponse;
    if (player) {
      await expect(trophies).toContainText("Waiting for this player's Season reset");
      await expect(page.locator(".player-updated")).toHaveAttribute("datetime", AFTER);
    } else
      await expect(
        page.getByText("Loading the new Season's rankings…"),
      ).not.toBeVisible();
    stage = "recovered";
    await page.clock.runFor(90_000);
    await expect(trophies).toContainText("5,000");
    expect(reads).toBe(2);
    await page.clock.runFor(180_000);
    expect(reads).toBe(2);
    expect(refreshes).toBe(0);
  });
}
