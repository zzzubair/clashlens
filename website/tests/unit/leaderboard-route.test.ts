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
}));
vi.mock("../../app/services/python.server", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../app/services/python.server")>()),
  createPythonClient: () => mocks,
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
    },
  ],
  totalTracked: 150,
  totalEntries: 101,
  page: 2,
  pageSize: 100,
  pageCount: 2,
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

async function render(query = "view=live&page=2") {
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
});

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
  expect(html).toMatch(/Updated <time[^>]+dateTime="2026-10-02T11:59:00Z"/);
  expect(html).not.toContain("2026-10-02T11:30:00Z");
  expect(html).not.toContain("Tracked player rankings");
  expect(html).toContain("<p>101–101 of 101</p>");
  expect(html).toContain('<span class="pagination-status">101–101 of 101</span>');
  expect(html).toContain(">Previous 100</a>");
  expect(html).toContain('<span class="rank-mark">101</span>');
  expect(html).toMatch(/<summary>Last updated 20 minutes ago/);
  expect(html).toContain("Over 10 min old");
  expect(html).toMatch(/<details[^>]*>.*dateTime="2026-10-02T11:40:00Z".*<\/details>/);
  expect(mocks.getTrackedLeaderboard).toHaveBeenCalledWith(
    100,
    "live",
    100,
    undefined,
    undefined,
  );
});

it.each([
  ["2026-10-02T11:50:00Z", "10 minutes ago", false],
  ["2026-10-02T11:49:59Z", "10 minutes ago", true],
  ["2026-10-02T11:58:00Z", "2 minutes ago", false],
] as const)(
  "measures a row updated at %s from its timestamp at page load",
  async (observedAt, age, stale) => {
    const fixture = structuredClone(board);
    fixture.entries[0].freshness = { ...fixture.entries[0].freshness, observedAt };
    mocks.getTrackedLeaderboard.mockResolvedValue(fixture);
    const { html } = await render();
    expect(html).toContain(`<summary>Last updated ${age}`);
    expect(html).toContain(`${age}${stale ? " · Over 10 min old" : ""}</span>`);
    expect(html.includes("Over 10 min old")).toBe(stale);
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
    '<strong>5,300</strong><span class="tie-slot"><span class="tie-pill" title="Same trophies as another player">Tie</span></span>',
  );
  // Untied rows keep the same empty slot, so every row's trophies line up.
  expect(rows[2]).toContain('<strong>5,290</strong><span class="tie-slot"></span>');
});

it("formats large ranks without explaining unlisted tracked players", async () => {
  mocks.getTrackedLeaderboard.mockResolvedValue({
    ...structuredClone(board),
    entries: [{ ...board.entries[0], rank: 11801 }],
    totalTracked: 13263,
    totalEntries: 11854,
  });
  const { html } = await render();
  expect(html).toContain("11,801–11,801 of 11,854");
  expect(html).not.toContain("13,263");
  expect(html).not.toContain("Tracked players are listed once");
});

it.each([
  [1, 250, 100, "Next 100"],
  [2, 250, 200, "Next 50"],
  [2, 250, 205, "Next 50"],
  [1, 104, 100, "Next 4"],
  [1, 104, 104, "Next 4"],
])(
  "labels the Next button on page %s of %s ranks ending at %s with the next page's rows",
  async (page, total, lastRank, label) => {
    mocks.getTrackedLeaderboard.mockResolvedValue({
      ...structuredClone(board),
      entries: [
        { ...board.entries[0], rank: 1 + (page - 1) * 100 },
        { ...board.entries[0], tag: "#2PY", rank: lastRank },
      ],
      totalEntries: total,
      page,
      pageCount: Math.ceil(total / 100),
      hasPrevious: page > 1,
      hasNext: true,
    });
    const { html } = await render(`view=live&page=${page}`);
    expect(html).toContain(`>${label}</a>`);
    expect(html.includes(">Previous 100</a>")).toBe(page > 1);
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
  expect(html).not.toContain("Go to page 1");
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
  expect(html).toContain('<p class="rankings-kicker">Day 28</p>');
  // The server writes UTC; the browser then switches it to the viewer's time.
  expect(html).toMatch(
    /id="leaderboard-title">Leaderboard at(<!-- -->)? <time class="local-time" datetime="2026-10-05T05:00:00Z"[^>]*>5 Oct 2026, 05:00 UTC<\/time><\/h1>/i,
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
    const links = [...html.matchAll(/<a href="([^"]+)"[^>]*>Go to Day (\d+)/g)];
    expect(links.map(([, , day]) => `Go to Day ${day}`)).toEqual(labels);
    for (const [, href, day] of links) expect(href).toContain(`day=${day}`);
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
  expect(html).toContain('<p class="rankings-kicker">Day 7</p>');
  // The private alert check reads the board's Reset from this heading
  // (website_daily_board in python/src/clashlens/alerts.py).
  expect(html).toContain(
    '<h1 id="leaderboard-title">Leaderboard at <time class="local-time" dateTime="2026-10-12T05:00:00Z"',
  );
  expect(mocks.getTrackedLeaderboard).toHaveBeenCalledTimes(1);
  expect(mocks.getTrackedLeaderboard).toHaveBeenCalledWith(
    100,
    "daily",
    100,
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
