import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createMemoryRouter,
  createStaticHandler,
  createStaticRouter,
  StaticRouterProvider,
} from "react-router";
import { beforeEach, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getTrackedLeaderboard: vi.fn(),
  searchLeaderboard: vi.fn(),
  getAccount: vi.fn(),
  getPublicUser: vi.fn(),
  readLoginIdentity: vi.fn(),
}));
vi.mock("../../app/services/python.server", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../app/services/python.server")>()),
  createPythonClient: () => mocks,
}));
// Signed out unless a test signs in.
vi.mock("../../app/server/config.server", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../app/server/config.server")>()),
  getWebsiteConfig: () => ({}),
}));
vi.mock("../../app/server/actions.server", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../app/server/actions.server")>()),
  readLoginIdentity: mocks.readLoginIdentity,
}));

import Leaderboard, { loader } from "../../app/routes/tracked-leaderboard";
import { PythonApiError } from "../../app/services/python.server";
import type { TrackedLeaderboard } from "../../app/lib/contracts";
import { worstCasePlayers } from "../fixtures/worst-case-players";

const board: TrackedLeaderboard = {
  kind: "tracked-leaderboard",
  view: "live",
  entries: [
    {
      rank: 101,
      tag: "#2PP",
      name: "Nova",
      clan: "Northwind",
      trophies: 7211,
      freshness: {
        state: "stale",
        observedAt: "2026-10-02T11:40:00Z",
        ageSeconds: 1200,
      },
      state: "stale",
      confidence: "high",
      resetRank: null,
      todayChange: null,
      recentDays: [],
    },
  ],
  totalTracked: 150,
  totalEntries: 101,
  // 50 ranks a page, so rank 101 opens page 3.
  page: 3,
  pageSize: 50,
  pageCount: 3,
  generatedAt: "2026-10-02T12:00:00Z",
  hasPrevious: true,
  hasNext: false,
  daily: null,
  coverage: { state: "partial", trackedPlayers: 150, measuredPercent: 67, note: "" },
  provenance: {
    source: "current accepted profiles",
    observedAt: "2026-10-02T11:59:00Z",
    freshness: "stale",
    confidence: "partial",
    coverage: "partial",
    version: "tracked-trophies-attack-destruction-v2",
  },
  sourceObservations: {
    newestObservedAt: "2026-10-02T11:59:00Z",
    oldestObservedAt: "2026-10-02T11:30:00Z",
    staleCount: 2,
  },
  qualityStates: ["stale"],
};

async function render(query = "view=live&page=3") {
  const handler = createStaticHandler([
    { path: "/leaderboards/tracked", Component: Leaderboard, loader },
  ]);
  const context = await handler.query(
    new Request(`https://clashlens.example/leaderboards/tracked?${query}`),
  );
  if (context instanceof Response) throw new Error("unexpected redirect");
  const html = renderToString(
    createElement(StaticRouterProvider, {
      router: createStaticRouter(handler.dataRoutes, context),
      context,
      hydrate: false,
    }),
  ).replaceAll("<!-- -->", "");
  return { html, status: context.statusCode };
}

beforeEach(() => {
  mocks.getTrackedLeaderboard.mockReset().mockResolvedValue(structuredClone(board));
  mocks.readLoginIdentity.mockReset().mockResolvedValue(null);
  mocks.getAccount.mockReset().mockResolvedValue({ username: "nova" });
  mocks.getPublicUser.mockReset();
});

// A linked player's card as the account's public page reads it.
const linked = {
  clan: null,
  reason: null,
  trophies: null,
  seasonResetPending: false,
  league: null,
  today: null,
};

it("keeps rank and update explanations off the page but in hover and screen-reader labels", async () => {
  const { html } = await render();
  expect(html).toContain('id="leaderboard-title">Live Leaderboard</h1>');
  expect(html).toContain('<p class="sr-only" id="rank-explanation">Rank among players');
  expect(html).toMatch(
    /<th scope="col" aria-describedby="rank-explanation" title="Rank among players tracked by Clash Lens, not the official global rank/,
  );
  expect(html).toContain(
    "Equal trophies are marked Tie and go to the higher average attack destruction this Season",
  );
  expect(html).toContain(
    'title="When we last confirmed each player&#x27;s profile, even if it was unchanged."',
  );
  expect(html).toMatch(
    /Updated <span class="rankings-stamp"><time[^>]+dateTime="2026-10-02T11:59:00Z"/,
  );
  expect(html).not.toContain("2026-10-02T11:30:00Z");
  expect(html).not.toContain("Tracked player rankings");
  // The board names its ranks at the top, the dash tight between them.
  expect(html).toContain(
    '<h2 id="standings-table-title"><span class="standings-word">Ranks </span>101–101<span class="standings-total"> of 101</span></h2>',
  );
  expect(html).not.toContain("page-arrow");
  expect(html).not.toContain("pagination-status");
  expect(html).toMatch(
    /rel="prev" aria-label="Previous 50" title="Previous 50" href="[^"]*page=2"/,
  );
  expect(html).toContain('<span class="rank-mark">101</span>');
  // The clan has its own column after Player, its tag beside it for wide
  // screens' hover, so each row is one line; a phone shows the line under
  // the name instead.
  expect(html).toContain(
    '<th scope="col" class="player-cell">Player</th><th scope="col" class="clan-cell">Clan</th>',
  );
  expect(html).toContain(
    '<td class="clan-cell"><bdi>Northwind</bdi><span class="clan-tag" aria-hidden="true">#2PP</span></td>',
  );
  expect(html).toContain(
    '<span class="player-sub"><span class="player-clan" data-label="Clan"><bdi>Northwind</bdi></span><details',
  );
  // A row that fell behind the board gets a dot; its age is read out, not shown.
  expect(html).toContain(
    '<summary><span class="sr-only">Last updated </span><span class="update-words">20 min ago</span><span class="sr-only"> · Older than the rest of the board</span></summary>',
  );
  expect(html).toMatch(/<details[^>]*>.*dateTime="2026-10-02T11:40:00Z".*<\/details>/);
  expect(mocks.getTrackedLeaderboard).toHaveBeenCalledWith(
    50,
    "live",
    100,
    undefined,
    undefined,
  );
});

it.each([
  // The board's newest update is 11:59.
  ["2026-10-02T11:49:00Z", "2026-10-02T12:00:00Z", "11 min", false],
  ["2026-10-02T11:48:59Z", "2026-10-02T12:00:00Z", "11 min", true],
  // A whole board that aged together marks no row.
  ["2026-10-02T11:58:00Z", "2026-10-02T13:00:00Z", "1 hour", false],
] as const)(
  "reads out the age at page load of a row updated at %s, and dots it only when over 10 min behind the board",
  async (observedAt, generatedAt, age, old) => {
    const fixture = structuredClone(board);
    fixture.generatedAt = generatedAt;
    fixture.entries[0].freshness = { ...fixture.entries[0].freshness, observedAt };
    mocks.getTrackedLeaderboard.mockResolvedValue(fixture);
    const { html } = await render();
    const note = '<span class="sr-only"> · Older than the rest of the board</span>';
    // Screen readers hear every row's age; sighted Clashers see a dot on old rows.
    expect(html).toContain(
      `<summary><span class="sr-only">Last updated </span><span class="update-words">${age} ago</span>${old ? note : ""}</summary>`,
    );
    expect(html).toContain(
      `<span class="update-words">${age} ago</span>${old ? note : ""}</time>`,
    );
    expect(html.includes("data-old")).toBe(old);
  },
);

it("marks only rows on equal trophies as a tie, keeps each rank its own and reserves the tie slot on every row", async () => {
  const fixture = structuredClone(board);
  const row = fixture.entries[0];
  fixture.entries = [
    { ...row, rank: 101, tag: "#2PP", trophies: 5300, tied: true },
    { ...row, rank: 102, tag: "#9PJYL8RQ0", trophies: 5300, tied: true },
    { ...row, rank: 103, tag: "#LQ2UV9GJC", trophies: 5290, tied: false },
  ];
  mocks.getTrackedLeaderboard.mockResolvedValue(fixture);
  const { html } = await render();
  const rows = html.split('<tr class="leaderboard-row"').slice(1);
  expect(rows.map((item) => item.match(/class="rank-mark">(\d+)</)![1])).toEqual([
    "101",
    "102",
    "103",
  ]);
  expect(rows.map((item) => item.includes('class="tie-pill"'))).toEqual([
    true,
    true,
    false,
  ]);
  expect(rows[0]).toContain(
    '<td class="tie-slot"><span class="tie-pill" title="Same trophies as another player">Tie</span></td><td class="trophy-cell" data-label="Trophies">',
  );
  // Untied rows keep the same empty slot, a column before the trophies, so
  // every row's trophies line up.
  expect(rows[2]).toContain(
    '<td class="tie-slot"></td><td class="trophy-cell" data-label="Trophies">',
  );
});

it("shows each Live row's places moved and trophies won since the last Reset", async () => {
  const fixture = structuredClone(board);
  const row = fixture.entries[0];
  fixture.entries = [
    {
      ...row,
      rank: 101,
      tag: "#2PP",
      trophies: 5340,
      resetRank: 140,
      todayChange: 75,
    },
    {
      ...row,
      rank: 102,
      tag: "#9PJYL8RQ0",
      trophies: 5300,
      resetRank: 90,
      todayChange: -40,
    },
    {
      ...row,
      rank: 103,
      tag: "#LQ2UV9GJC",
      trophies: 5290,
      resetRank: 103,
      // 70 below its Reset board total, all of it the automatic defense loss
      // the Reset reading was taken before: nothing won or lost today.
      todayChange: 0,
    },
    {
      ...row,
      rank: 104,
      tag: "#8QU8J9LP",
      trophies: 5290,
      resetRank: null,
      todayChange: null,
    },
    {
      ...row,
      rank: 105,
      tag: "#QG9U8P2Y",
      trophies: 5280,
      resetRank: 412,
      todayChange: 180,
    },
  ];
  mocks.getTrackedLeaderboard.mockResolvedValue(fixture);
  const { html } = await render();
  const cells = (name: string) =>
    html
      .split('<tr class="leaderboard-row"')
      .slice(1)
      .map((item) => item.match(new RegExp(`<td class="${name}">(.*?)</td>`))![1]);
  // A held place is a quiet dash, so every name starts at one point; a move
  // of over 99 places is marked like any other.
  expect(cells("move-cell")).toEqual([
    '<span class="rank-move" data-up="true"><span aria-hidden="true"><span class="rank-move-arrow">▲</span>39</span><span class="sr-only">Up 39 since Reset</span></span>',
    '<span class="rank-move"><span aria-hidden="true"><span class="rank-move-arrow">▼</span>12</span><span class="sr-only">Down 12 since Reset</span></span>',
    '<span class="rank-move" data-held=""><span aria-hidden="true">–</span><span class="sr-only">No change since Reset</span></span>',
    '<span class="rank-move" data-new="">new<span class="sr-only"> since Reset</span></span>',
    '<span class="rank-move" data-up="true"><span aria-hidden="true"><span class="rank-move-arrow">▲</span>307</span><span class="sr-only">Up 307 since Reset</span></span>',
  ]);
  expect(cells("today-cell")).toEqual([
    '<span class="today-change" data-sign="up">+75</span>',
    '<span class="today-change" data-sign="down">−40</span>',
    '<span class="today-change">0</span>',
    "",
    '<span class="today-change" data-sign="up">+180</span>',
  ]);
  // The heading says once what the arrows count from.
  expect(html).toContain(
    '<th scope="col" class="move-cell"><span class="move-head">Since Reset</span></th>',
  );
  expect(html).toContain(
    '<th scope="col" class="today-cell" title="Trophies won or lost since the last Reset">Today</th>',
  );
  // The old trophy gap to the row above is gone; the change since Reset took its place.
  expect(html).not.toContain("To player above");
});

it("leaves out the Reset columns when no row has a Reset place", async () => {
  // A Daily board, or the minutes after a Reset before today is saved.
  const { html } = await render();
  expect(html).not.toContain("move-cell");
  expect(html).not.toContain("today-cell");
  expect(html).not.toContain("recent-cell");
});

it("shows Today on a Season's first Legend day, with no Reset place to compare", async () => {
  const fixture = structuredClone(board);
  fixture.entries[0]!.todayChange = -80;
  mocks.getTrackedLeaderboard.mockResolvedValue(fixture);
  const { html } = await render();
  expect(html).not.toContain("move-cell");
  expect(html).toContain(
    '<td class="today-cell"><span class="today-change" data-sign="down">−80</span></td>',
  );
});

/** Finished Legend days by their start, oldest first, from `first`. */
function days(first: string, ...changes: Array<number | null>) {
  return changes.map((trophyChange, index) => ({
    start: new Date(Date.parse(first) + index * 86_400_000).toISOString(),
    trophyChange,
  }));
}

it("draws each Live row's last 7 finished days as bars, each named by its own day", async () => {
  const fixture = structuredClone(board);
  const row = fixture.entries[0]!;
  fixture.entries = [
    {
      ...row,
      tag: "#2PP",
      trophies: 5340,
      resetRank: 101,
      // Mon 28 Sep has no result, and Thu 1 Oct's is not saved yet, as in the
      // minutes after its Reset.
      recentDays: days("2026-09-25T05:00:00Z", 40, -20, 30, null, 60, 60, null),
    },
    {
      ...row,
      rank: 102,
      tag: "#9PJYL8RQ0",
      trophies: 5290,
      resetRank: 102,
      // A newer player: only the last 2 days.
      recentDays: days("2026-09-25T05:00:00Z", null, null, null, null, null, 40, 20),
    },
  ];
  mocks.getTrackedLeaderboard.mockResolvedValue(fixture);
  const { html } = await render();
  // The heading is two words; its tooltip names the rows' own dates. Today
  // is the Today column's, not a bar.
  expect(html).toMatch(
    /<th scope="col" class="recent-cell" title="25 Sep – 1 Oct, oldest first: [^"]+">Last 7 days<\/th>/,
  );
  expect(html).not.toContain("recent-scale");
  const charts = [...html.matchAll(/<span class="recent-days"(.*?)<\/span><\/td>/g)].map(
    (match) => match[1],
  );
  expect(charts).toHaveLength(2);
  // Read out by each day's own date, the days without a result left out; a
  // readout per bar waits for a point or a tap.
  expect(charts[0]).toContain(
    'aria-label="Daily trophies: Fri 25 Sep +40, Sat 26 Sep −20, Sun 27 Sep +30, Tue 29 Sep +60, Wed 30 Sep +60"',
  );
  expect(charts[1]).toContain(
    'aria-label="Daily trophies: Wed 30 Sep +40, Thu 1 Oct +20"',
  );
  expect(html).not.toContain("recent-tip");
  // Every row has exactly 7 day slots, the last for yesterday, and no bar
  // for today.
  for (const chart of charts) expect(chart.match(/class="recent-day"/g)).toHaveLength(7);
  expect(html).not.toContain("data-today");
  // Each row's biggest day fills its half of the chart, either side of a
  // zero line across the middle: the +60 days on the first row, the +40 day
  // on the second. So the same +40 stands two thirds as tall on the first.
  expect(charts[0]).toContain('data-index="4" data-sign="up" style="--h:50%"');
  expect(charts[0]).toContain(
    'data-index="0" data-sign="up" style="--h:33.33333333333333%"',
  );
  expect(charts[1]).toContain('data-index="5" data-sign="up" style="--h:50%"');
  expect(charts[1]).toContain('data-index="6" data-sign="up" style="--h:25%"');
  expect(html).not.toContain("--zero");
  expect(html).not.toContain("data-latest");
  // A loss hangs below the zero line; a day without a result is an empty
  // slot in its own place, not merged with another.
  expect(charts[0]).toContain('data-sign="down" style="--h:16.666666666666664%"');
  expect(charts[0]).toMatch(/<span class="recent-day" data-index="3"><\/span>/);
  expect(charts[0]).toMatch(/<span class="recent-day" data-index="6"><\/span>/);
});

it("charts only the Season's own days in its first week", async () => {
  const fixture = structuredClone(board);
  // Day 4 of a Season that began on Tue 29 Sep: three finished days.
  fixture.entries[0]!.recentDays = days("2026-09-29T05:00:00Z", 40, -20, 60);
  mocks.getTrackedLeaderboard.mockResolvedValue(fixture);
  const { html } = await render();
  expect(html).toMatch(/<th scope="col" class="recent-cell" title="29 Sep – 1 Oct, /);
  const chart = html.match(/<span class="recent-days"(.*?)<\/span><\/td>/)![1]!;
  expect(chart).toContain(
    'aria-label="Daily trophies: Tue 29 Sep +40, Wed 30 Sep −20, Thu 1 Oct +60"',
  );
  // The chart keeps its 7 slots; the 4 before the Season are no day at all.
  expect(chart.match(/class="recent-day"/g)).toHaveLength(7);
  expect(chart.match(/<span class="recent-day"><\/span>/g)).toHaveLength(4);
  expect(chart).toContain('data-index="6" data-sign="up" style="--h:50%"');
});

it("keeps every row's shape when another row has a far bigger day", async () => {
  const fixture = structuredClone(board);
  const row = fixture.entries[0]!;
  const day = (tag: string, changes: number[], trophies: number) => ({
    ...row,
    tag,
    trophies,
    resetRank: 101,
    recentDays: days("2026-09-28T05:00:00Z", ...changes),
  });
  fixture.entries = [
    // Ten ordinary rows, then one with a big day and a bigger today.
    ...Array.from({ length: 10 }, (_, index) =>
      day(`#2P${"289PYLQGRJ"[index]}`, [40, -40, 40, -40], 5040),
    ),
    day("#2PP", [40, -40, 212, -40], 5387),
  ];
  mocks.getTrackedLeaderboard.mockResolvedValue(fixture);
  const { html } = await render();
  // The +212 day fills only its own row's chart (today's +215 has no bar).
  // The ten ordinary rows still draw their ±40 days at full height, not as
  // stubs under the one big day.
  const charts = [...html.matchAll(/<span class="recent-days"(.*?)<\/span><\/td>/g)].map(
    (match) => match[1],
  );
  expect(charts).toHaveLength(11);
  for (const chart of charts.slice(0, 10)) {
    expect(chart).toContain('data-sign="up" style="--h:50%"');
    expect(chart).toContain('data-sign="down" style="--h:50%"');
  }
  expect(charts.at(-1)).toContain('data-sign="up" style="--h:50%"');
  expect(charts.at(-1)).toContain('data-sign="down" style="--h:9.433962264150944%"');
});

it("opens the page holding a rank asked for, with that row picked", async () => {
  const load = (query: string) =>
    loader({
      request: new Request(`https://clashlens.example/leaderboards/tracked?${query}`),
    } as Parameters<typeof loader>[0]);
  const jump = await load("view=live&rank=101").catch((cause: unknown) => cause);
  expect((jump as Response).headers.get("Location")).toBe(
    "/leaderboards/tracked?view=live&page=3&rank=101",
  );
  const { html } = await render("view=live&page=3&rank=101");
  expect(html).toMatch(
    /<tr class="leaderboard-row" data-selected="true" aria-label="Selected player Nova, rank 101"/,
  );
  for (const bad of ["rank=0", "rank=-3", "rank=abc", "rank=1e3", "rank=5&q=Nova"])
    await expect(load(`view=live&page=1&${bad}`)).rejects.toMatchObject({ status: 422 });
});

it("takes a plain number in Live's search as a rank, and #130 as a tag", async () => {
  const load = (query: string) =>
    loader({
      request: new Request(`https://clashlens.example/leaderboards/tracked?${query}`),
    } as Parameters<typeof loader>[0]);
  for (const [typed, location] of [
    ["101", "/leaderboards/tracked?view=live&page=3&rank=101"],
    ["1%2C250", "/leaderboards/tracked?view=live&page=25&rank=1250"],
    ["%2042%20", "/leaderboards/tracked?view=live&page=1&rank=42"],
  ]) {
    const jump = await load(`view=live&page=1&q=${typed}`).catch(
      (cause: unknown) => cause,
    );
    expect((jump as Response).headers.get("Location")).toBe(location);
  }
  expect(mocks.searchLeaderboard).not.toHaveBeenCalled();
  // "#130" starts a player tag, and 0 is no rank: both are searched for.
  mocks.searchLeaderboard.mockResolvedValue({
    exactTag: null,
    hasMore: false,
    results: [],
  });
  for (const typed of ["%23130", "0"]) await load(`view=live&page=1&q=${typed}`);
  expect(mocks.searchLeaderboard.mock.calls).toEqual([["#130"], ["0"]]);
});

it("says plainly when a rank asked for is past the board's end", async () => {
  mocks.getTrackedLeaderboard.mockRejectedValue(new PythonApiError(404, {}));
  const { html, status } = await render("view=live&page=200&rank=9999");
  expect(status).toBe(404);
  expect(html).toContain("<h2>No rank 9,999 on this board</h2>");
  expect(html).toContain("Go to page 1");
  expect(html).not.toContain("This standings page is unavailable");
});

it("keeps Go to rank on an At Reset board, which has no player search", async () => {
  const fixture = structuredClone(board);
  fixture.view = "daily";
  fixture.daily = {
    officialSeasonId: "1788757200",
    dayNumber: 26,
    resetAt: "2026-10-03T05:00:00Z",
    seasonStartAt: "2026-09-07T05:00:00Z",
    seasonEndAt: "2026-10-05T05:00:00Z",
    previousSnapshot: null,
    nextSnapshot: null,
  };
  mocks.getTrackedLeaderboard.mockResolvedValue(fixture);
  const { html } = await render("view=daily&season=1788757200&day=26&page=1");
  expect(html).toContain('<form class="rank-jump" aria-label="Go to rank"');
  expect(html).toMatch(
    /<input id="rank-jump-input" type="number" min="1" max="101" required=""[^>]* name="rank"/,
  );
  expect(html).not.toContain('id="rank-query"');
});

it("shows all 50 rows of a page under one range, with a small pager there and the pager in words at the foot", async () => {
  const fixture = structuredClone(board);
  const row = fixture.entries[0];
  fixture.totalEntries = 9000;
  fixture.page = 1;
  fixture.pageCount = 180;
  fixture.hasPrevious = false;
  fixture.hasNext = true;
  fixture.entries = Array.from({ length: 50 }, (_, index) => ({
    ...row,
    rank: index + 1,
    tag: `#2${"289PYLQGRJ"[Math.floor(index / 10)]}${"289PYLQGRJ"[index % 10]}`,
  }));
  mocks.getTrackedLeaderboard.mockResolvedValue(fixture);
  const { html } = await render("view=live&page=1");
  expect(html.match(/<tr class="leaderboard-row"/g)).toHaveLength(50);
  expect(html).toContain(
    '<span class="standings-word">Ranks </span>1–50<span class="standings-total"> of 9,000</span>',
  );
  // Live and At Reset head the board, beside its range; Live's search takes
  // a rank, so it has no Go to rank box of its own.
  expect(html).toContain(
    '<div class="standings-toolbar"><nav aria-label="Leaderboard views" class="leaderboard-view-switch">',
  );
  expect(html.match(/aria-label="Leaderboard views"/g)).toHaveLength(1);
  expect(html).not.toContain('class="rank-jump"');
  expect(html).toContain('placeholder="Name, #tag or rank"');
  // Beside the range: page 1 has no previous page, so only Next works.
  expect(html).toMatch(
    /<nav aria-label="Previous and next page" class="standings-steps"><span class="button button-secondary" aria-hidden="true">‹<\/span><a class="button button-secondary" rel="next" aria-label="Next 50" title="Next 50" href="[^"]*page=2"[^>]*>›<\/a><\/nav>/,
  );
  // The foot has the same pager, with the range between its steps.
  expect(html.match(/aria-label="Leaderboard pages"/g)).toHaveLength(1);
  expect(html).toMatch(
    /<nav aria-label="Leaderboard pages" class="standings-pagination"><span class="button button-secondary" aria-hidden="true">‹<\/span><span class="standings-range">1–50<span class="standings-total"> of 9,000<\/span><\/span><a class="button button-secondary" rel="next" aria-label="Next 50" title="Next 50" href="[^"]*page=2"[^>]*>›<\/a><\/nav>/,
  );
  expect(html).not.toMatch(/rel="prev"|>Previous|>Next/);
  for (const gone of ["data-folded", "Show all", "Back to top", "page-arrow"])
    expect(html).not.toContain(gone);
});

it("formats large ranks without explaining unlisted tracked players", async () => {
  mocks.getTrackedLeaderboard.mockResolvedValue({
    ...structuredClone(board),
    entries: [{ ...board.entries[0], rank: 11801 }],
    totalTracked: 13263,
    totalEntries: 11854,
  });
  const { html } = await render();
  expect(html).toContain(
    '<span class="standings-word">Ranks </span>11,801–11,801<span class="standings-total"> of 11,854</span>',
  );
  expect(html).not.toContain("13,263");
  expect(html).not.toContain("Tracked players are listed once");
});

it.each([
  [1, 230, 50, "Next 50"],
  [4, 230, 200, "Next 30"],
  // A page opened at a player near its end also lists the 5 ranks after them.
  [4, 230, 205, "Next 30"],
  [1, 54, 50, "Next 4"],
  [1, 54, 54, "Next 4"],
])(
  "labels both Next steps on page %s of %s ranks ending at %s with the next page's rows",
  async (page, total, lastRank, label) => {
    mocks.getTrackedLeaderboard.mockResolvedValue({
      ...structuredClone(board),
      entries: [
        { ...board.entries[0], rank: 1 + (page - 1) * 50 },
        { ...board.entries[0], tag: "#2PY", rank: lastRank },
      ],
      totalEntries: total,
      page,
      pageCount: Math.ceil(total / 50),
      hasPrevious: page > 1,
      hasNext: true,
    });
    const { html } = await render(`view=live&page=${page}`);
    expect(html.match(new RegExp(`aria-label="${label}"`, "g"))).toHaveLength(2);
    expect(html.includes('aria-label="Previous 50"')).toBe(page > 1);
  },
);

it("keeps worst-case names readable and separates five-digit ranks", async () => {
  mocks.getTrackedLeaderboard.mockResolvedValue({
    ...structuredClone(board),
    entries: worstCasePlayers.map((player) => ({ ...board.entries[0], ...player })),
  });
  const { html } = await render();
  for (const player of worstCasePlayers) {
    expect(html).toContain(
      `<span class="rank-mark">${player.rank.toLocaleString()}</span>`,
    );
    // Each name sets its own direction, so a right-to-left name keeps its symbols in place.
    expect(html).toContain(`<bdi>${player.name}</bdi>`);
  }
});

it("shows an empty message without a table or impossible pagination", async () => {
  mocks.getTrackedLeaderboard.mockResolvedValue({
    ...board,
    entries: [],
    totalEntries: 0,
    page: 1,
    pageCount: 0,
    hasPrevious: false,
    sourceObservations: null,
  });
  const { html, status } = await render("view=live&page=1");
  expect(status).toBe(200);
  expect(html).toContain("No standings available yet");
  expect(html).not.toContain("<table");
  expect(html).not.toContain(" of 0");
  expect(html).not.toContain('aria-label="Leaderboard pages"');
  expect(html).not.toContain("Updated <time");
});

it.each([
  ["view=live&page=999", "/leaderboards/tracked?view=live&amp;page=1"],
  [
    "view=daily&season=1785714000&day=21&page=999",
    "/leaderboards/tracked?view=daily&amp;season=1785714000&amp;day=21&amp;page=1",
  ],
])(
  "offers page one for an unavailable page, preserving its view: %s",
  async (query, href) => {
    mocks.getTrackedLeaderboard.mockRejectedValue(new PythonApiError(404, {}));
    const { html, status } = await render(query);
    expect(status).toBe(404);
    expect(html).toContain("This standings page is unavailable");
    expect(html).toContain(`href="${href}"`);
    expect(html).toContain("Go to page 1");
    expect(html).not.toContain("<table");
  },
);

it("does not mislabel service failures as missing standings", async () => {
  mocks.getTrackedLeaderboard.mockRejectedValue(new PythonApiError(503, {}));
  const { html } = await render();
  expect(html).toContain("Leaderboard unavailable");
  expect(html).not.toContain("No standings available yet");
  expect(html).not.toContain("This standings page is unavailable");
  // A way back: this page again, or page 1 from this later page.
  expect(html).toMatch(
    /href="\/leaderboards\/tracked\?view=live&amp;page=3"[^>]*>Try again</,
  );
  expect(html).toMatch(
    /href="\/leaderboards\/tracked\?view=live&amp;page=1"[^>]*>Go to page 1</,
  );
});

it.each([
  ["2026-10-03T04:55:00Z", false],
  ["2026-10-03T04:30:00Z", false],
  ["2026-10-03T00:00:19Z", true],
] as const)(
  "marks a Daily board whose newest input is %s as incomplete: %s",
  async (newestInput, incomplete) => {
    const fixture = structuredClone(board);
    fixture.view = "daily";
    fixture.daily = {
      officialSeasonId: "1788757200",
      dayNumber: 26,
      resetAt: "2026-10-03T05:00:00Z",
      seasonStartAt: "2026-09-07T05:00:00Z",
      seasonEndAt: "2026-10-05T05:00:00Z",
      previousSnapshot: null,
      nextSnapshot: null,
    };
    fixture.provenance.observedAt = newestInput;
    fixture.entries[0].freshness.observedAt = "2026-10-02T23:59:00Z";
    mocks.getTrackedLeaderboard.mockResolvedValue(fixture);
    const { html } = await render("view=daily&season=1788757200&day=26&page=1");
    expect(html.includes("<strong>Incomplete:</strong>")).toBe(incomplete);
    if (incomplete) {
      expect(html).toContain(
        "no player updates were saved in the 4 hours before this Reset",
      );
      expect(html).toMatch(/Newest: <time[^>]+dateTime="2026-10-03T00:00:19Z"/);
      expect(html.match(/5 hours before Reset/g)).toHaveLength(2);
    } else expect(html).not.toContain("before Reset");
  },
);

it.each([
  [13263, "13,263 tracked players are waiting for their Season reset"],
  [1, "1 tracked player is waiting for their Season reset"],
  [0, null],
] as const)(
  "says how many players wait off the Live board for their Season reset: %s",
  async (seasonResetPending, text) => {
    mocks.getTrackedLeaderboard.mockResolvedValue({
      ...structuredClone(board),
      entries: [],
      totalEntries: 0,
      page: 1,
      pageCount: 0,
      hasPrevious: false,
      sourceObservations: null,
      seasonResetPending,
    });
    const { html } = await render("view=live&page=1");
    expect(html.includes("waiting for their Season reset")).toBe(text !== null);
    if (text) expect(html).toContain(text);
  },
);

it("heads a Daily board with its Reset time and, at the Season end, its automatic loss line", async () => {
  const fixture = structuredClone(board);
  fixture.view = "daily";
  fixture.seasonResetPending = 40;
  fixture.daily = {
    officialSeasonId: "1788757200",
    dayNumber: 28,
    resetAt: "2026-10-05T05:00:00Z",
    seasonStartAt: "2026-09-07T05:00:00Z",
    seasonEndAt: "2026-10-05T05:00:00Z",
    previousSnapshot: null,
    nextSnapshot: null,
  };
  fixture.provenance.observedAt = "2026-10-05T04:59:00Z";
  fixture.provenance.version = "tracked-player-order-v3";
  mocks.getTrackedLeaderboard.mockResolvedValue(fixture);
  const { html } = await render("view=daily&season=1788757200&day=28&page=1");
  // The server writes UTC; the browser then switches it to the viewer's time.
  expect(html).toMatch(
    /id="leaderboard-title">At Reset<\/h1><p class="rankings-date">Day 28 · <time class="local-time board-reset" datetime="2026-10-05T05:00:00Z"[^>]*>5 Oct 2026, 05:00 UTC<\/time><\/p>/i,
  );
  expect(html).toContain(
    '<p class="rankings-context">Complete days show trophies after the automatic defense loss.</p>',
  );
  expect(html).not.toContain("not rebuilt since");
  expect(html).not.toContain("Legend season");
  expect(html).toContain("7,211");
  expect(html).toContain(
    "the player whose trophies changed most recently that Legend day",
  );
  expect(html).not.toContain("Incomplete:");
  // The Live board's Season-reset rule never applies to a frozen day.
  expect(html).not.toContain("waiting for their Season reset");
});

it.each([
  [
    "tracked-player-order-v3",
    "go to the player whose trophies changed most recently that Legend day, then",
  ],
  [
    "tracked-player-order-v2",
    "go to the higher average attack destruction this Season, then more attacks, then",
  ],
  ["tracked-player-order-v1", "follow a fixed order based on player tags."],
])(
  "explains ties on a Daily board saved with %s by the order it kept",
  async (version, order) => {
    const fixture = structuredClone(board);
    fixture.view = "daily";
    fixture.daily = {
      officialSeasonId: "1788757200",
      dayNumber: 2,
      resetAt: "2026-09-09T05:00:00Z",
      seasonStartAt: "2026-09-07T05:00:00Z",
      seasonEndAt: "2026-10-05T05:00:00Z",
      previousSnapshot: null,
      nextSnapshot: null,
    };
    fixture.provenance.observedAt = "2026-09-09T04:59:00Z";
    fixture.provenance.version = version;
    mocks.getTrackedLeaderboard.mockResolvedValue(fixture);
    const { html } = await render("view=daily&season=1788757200&day=2&page=1");
    const explanation = `Equal trophies are marked Tie and ${order}`;
    expect(html).toContain(`<p class="sr-only" id="rank-explanation">Rank among players`);
    expect(html.match(new RegExp(explanation, "g"))).toHaveLength(2);
    // Only the Season's last board takes off the automatic loss.
    expect(html).not.toContain("automatic defense loss");
  },
);

it.each([
  ["q=%232pp", "#2PP isn&#x27;t on this board.", '<a href="/players/%232PP">Open their'],
  ["q=Nova", "No players on this board match “Nova”.", "Enter a full #tag"],
  [
    "q=%23ZZZZZZZZ",
    "No players on this board match “#ZZZZZZZZ”. #ZZZZZZZZ is not a Clash of Clans player tag.",
    "Tags use only 0, 2, 8, 9",
  ],
])(
  "offers a next step when a board search for %s finds nobody",
  async (q, text, next) => {
    mocks.searchLeaderboard.mockResolvedValue({
      exactTag: null,
      hasMore: false,
      results: [],
    });
    const { html } = await render(`view=live&page=1&${q}`);
    expect(html).toContain(text);
    expect(html).toContain(next);
    expect(html).not.toContain("Try another name or an exact tag");
  },
);

it.each([
  [{ dayNumber: 1 }, null, ["Go to Day 1"]],
  [null, { dayNumber: 3 }, ["Go to Day 3"]],
  [{ dayNumber: 1 }, { dayNumber: 3 }, ["Go to Day 1", "Go to Day 3"]],
  [null, null, []],
] as const)(
  "names the Season day each day button opens, above the table: %o %o",
  async (previous, next, labels) => {
    const fixture = structuredClone(board);
    fixture.view = "daily";
    fixture.daily = {
      officialSeasonId: "1788757200",
      dayNumber: 2,
      resetAt: "2026-09-09T05:00:00Z",
      seasonStartAt: "2026-09-07T05:00:00Z",
      seasonEndAt: "2026-10-05T05:00:00Z",
      previousSnapshot: previous && { officialSeasonId: "1788757200", ...previous },
      nextSnapshot: next && { officialSeasonId: "1788757200", ...next },
    };
    fixture.provenance.observedAt = "2026-09-09T04:59:00Z";
    mocks.getTrackedLeaderboard.mockResolvedValue(fixture);
    const { html } = await render("view=daily&season=1788757200&day=2&page=1");
    const links = [
      ...html.matchAll(/<a aria-label="Go to Day (\d+)" href="([^"]+)"/g),
    ].map(([, day, href]) => ({ day, href }));
    expect(links.map(({ day }) => `Go to Day ${day}`)).toEqual(labels);
    for (const { day, href } of links) expect(href).toContain(`day=${day}`);
    expect(html).not.toMatch(/>(Older|Newer)</);
    if (labels.length) {
      expect(html.indexOf('aria-label="Other days"')).toBeLessThan(
        html.indexOf("<table"),
      );
    } else expect(html).not.toContain('aria-label="Other days"');
  },
);

it("shows the newest Daily board from one read, then names its Season and day without reading it again", async () => {
  const fixture = structuredClone(board);
  fixture.view = "daily";
  fixture.daily = {
    officialSeasonId: "1791176400",
    dayNumber: 7,
    resetAt: "2026-10-12T05:00:00Z",
    seasonStartAt: "2026-10-05T05:00:00Z",
    seasonEndAt: "2026-11-02T05:00:00Z",
    previousSnapshot: null,
    nextSnapshot: null,
  };
  fixture.provenance.observedAt = "2026-10-12T04:59:00Z";
  mocks.getTrackedLeaderboard.mockResolvedValue(fixture);
  const explicit = "/leaderboards/tracked?view=daily&season=1791176400&day=7&page=2";

  // The board renders at once, as it does without JavaScript, not after a redirect.
  const { html, status } = await render("view=daily&page=2");
  expect(status).toBe(200);
  // The private alert check reads the board's Reset from this time
  // (website_daily_board in python/src/clashlens/alerts.py).
  expect(html).toContain(
    '<p class="rankings-date">Day 7 · <time class="local-time board-reset" dateTime="2026-10-12T05:00:00Z"',
  );
  expect(mocks.getTrackedLeaderboard).toHaveBeenCalledTimes(1);
  expect(mocks.getTrackedLeaderboard).toHaveBeenCalledWith(
    50,
    "daily",
    50,
    undefined,
    undefined,
  );

  // In the browser the address then names the board's day, with no new read.
  const rootLoader = vi.fn(() => null);
  const router = createMemoryRouter(
    [
      {
        id: "root",
        path: "/",
        loader: rootLoader,
        children: [{ path: "leaderboards/tracked", loader }],
      },
    ],
    { initialEntries: ["/leaderboards/tracked?view=daily&page=2"] },
  );
  await vi.waitFor(() => expect(router.state.initialized).toBe(true));
  const data = router.state.loaderData["0-0"] as { latestDailyUrl: string | null };
  expect(data.latestDailyUrl).toBe(explicit);
  await router.navigate(data.latestDailyUrl, {
    replace: true,
    preventScrollReset: true,
    defaultShouldRevalidate: false,
  });
  expect(router.state.location.pathname + router.state.location.search).toBe(explicit);
  expect(router.state.historyAction).toBe("REPLACE");
  expect(mocks.getTrackedLeaderboard).toHaveBeenCalledTimes(2);
  expect(rootLoader).toHaveBeenCalledTimes(1);
  router.dispose();

  // An address that already names the day needs nothing more.
  const named = createMemoryRouter([{ path: "/leaderboards/tracked", loader }], {
    initialEntries: [explicit],
  });
  await vi.waitFor(() => expect(named.state.initialized).toBe(true));
  expect(
    (named.state.loaderData["0"] as { latestDailyUrl: string | null }).latestDailyUrl,
  ).toBeNull();
  named.dispose();
});

it("gives a signed-in Clasher one tap to the place of the player their dashboard leads with, and marks their row", async () => {
  mocks.readLoginIdentity.mockResolvedValue({ provider: "google", providerSubject: "1" });
  mocks.getPublicUser.mockResolvedValue({
    username: "nova",
    displayName: "Nova",
    // The dashboard leads with the first one being tracked, so Jump to me
    // goes there too, whichever is the Discord bot's default.
    verifiedPlayers: [
      { ...linked, tag: "#8QU8J9LP", name: "Gone", state: "not_in_legend", rank: null },
      { ...linked, tag: "#2PP", name: "Nova", state: "tracking", rank: 101 },
      { ...linked, tag: "#9PJYL8RQ0", name: "Alt", state: "tracking", rank: 102 },
    ],
  });
  const { html } = await render();
  expect(mocks.getPublicUser).toHaveBeenCalledWith("nova");
  expect(html).toMatch(
    /<a class="button secondary jump-to-me" aria-label="Jump to me, rank 101" href="\/leaderboards\/tracked\?view=live&amp;page=3&amp;player=%232PP"/,
  );
  expect(html).toMatch(/<tr class="leaderboard-row" data-you="true"[^>]*>/);
  expect(html).toContain('<span class="you-badge">You</span>');
});

it("shows no Jump to me when signed out, or when their player is off the board", async () => {
  expect((await render()).html).not.toContain("Jump to me");
  expect(mocks.getAccount).not.toHaveBeenCalled();
  mocks.readLoginIdentity.mockResolvedValue({ provider: "google", providerSubject: "1" });
  mocks.getPublicUser.mockResolvedValue({
    username: "nova",
    displayName: "Nova",
    verifiedPlayers: [
      { ...linked, tag: "#9PJYL8RQ0", name: "Gone", state: "not_in_legend", rank: null },
    ],
  });
  const { html, status } = await render();
  expect(status).toBe(200);
  expect(html).toContain("Live Leaderboard</h1>");
  expect(html).not.toContain("Jump to me");
  // A failed read of their players never stops the board.
  mocks.getPublicUser.mockRejectedValue(new PythonApiError(503, {}));
  const failed = await render();
  expect(failed.status).toBe(200);
  expect(failed.html).not.toContain("Jump to me");
});
