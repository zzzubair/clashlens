import { beforeEach, describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  StaticRouterProvider,
} from "react-router";

const mocks = vi.hoisted(() => ({
  createPythonClient: vi.fn(),
  getPlayerLookup: vi.fn(),
}));

vi.mock("../../app/services/player-lookup.server", () => ({
  getPlayerLookup: mocks.getPlayerLookup,
  startPlayerLookup: vi.fn(),
}));

vi.mock("../../app/services/python.server", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../app/services/python.server")>();
  return { ...actual, createPythonClient: mocks.createPythonClient };
});

import type {
  HistoricalSeasonDayEntry,
  HistoricalSeasonSummary,
  PlayerPage,
  RankedDaySummary,
} from "../../app/lib/contracts";
import { selectPlayerHistory } from "../../app/lib/player-lookup-text";
import { PythonApiError } from "../../app/services/python.server";
import PlayerRoute, { loader as playerLoader } from "../../app/routes/player";

const TAG = "#2PP";
const SEASON = "1788757200";

const DAY: HistoricalSeasonDayEntry = {
  dayNumber: 24,
  period: "2026-09-30T05:00:00Z",
  state: "Complete",
  coverage: "complete",
  startTrophies: 5500,
  endTrophies: 5470,
  eodState: null,
  eodChange: null,
  eodChangeState: null,
  attacks: 8,
  defenses: 8,
  attackGain: 30,
  defenseLoss: 20,
  netChange: -30,
  hasAdjustment: false,
  adjustmentTotal: null,
  flags: [],
};

// Five of 28 days; the -30 daily change includes a 40-trophy automatic loss.
const SUMMARY: HistoricalSeasonSummary = {
  kind: "player-season-summary",
  tag: TAG,
  seasonId: SEASON,
  seasonStart: null,
  seasonEnd: "2026-10-05T05:00:00Z",
  startTrophies: null,
  endTrophies: 5800,
  finalRank: 321,
  attackCount: 40,
  attackGain: 1600,
  defenseCount: 40,
  defenseLoss: 1300,
  netTrophyChange: 300,
  attackStars: { "0": 0, "1": 0, "2": 20, "3": 20 },
  defenseStars: { "0": 20, "1": 20, "2": 0, "3": 0 },
  attackStarsUnknown: 0,
  defenseStarsUnknown: 0,
  daysObserved: 5,
  daysMissing: Array.from({ length: 23 }, (_, index) => index + 1),
  coverageState: "partial",
  unresolvedFlags: ["missing_days"],
  dailyEntries: [DAY],
  publishedAt: "2026-10-05T05:08:00Z",
  source: "tracked_summary",
  officialHistory: null,
};

const PLAYER = {
  kind: "player-page",
  tag: TAG,
  trackingState: "tracking",
  profile: {
    tag: TAG,
    name: "Nova",
    clan: "Example",
    trophies: 5000,
    freshness: { state: "fresh", observedAt: "2026-10-05T05:10:00Z", ageSeconds: 0 },
    confidence: "high",
    coverage: "complete",
    eligibility: "legend-i",
  },
  season: null,
  currentDay: null,
  recentDays: [],
  seasonDays: [],
  dataQuality: [],
  provenance: {
    source: "api_player_daily_logs",
    observedAt: "2026-10-05T05:10:00Z",
    freshness: "fresh",
    confidence: "high",
    coverage: "complete",
    version: "v1",
  },
} satisfies PlayerPage;

const CURRENT_LINK = '<a href="/players/%232PP" data-discover="true">Current Season</a>';

async function loadAndRender(
  client: Record<string, unknown>,
  season: string | null,
  day?: string,
) {
  mocks.createPythonClient.mockReturnValue(client);
  const params = new URLSearchParams({
    ...(season !== null && { season }),
    ...(day !== undefined && { day }),
  });
  const search = params.size === 0 ? "" : `?${params}`;
  const request = new Request(`https://clashlens.example/players/%232PP${search}`);
  const data = await playerLoader({ request, params: { tag: TAG } } as never);
  const handler = createStaticHandler([
    { path: "/players/:tag", Component: PlayerRoute, loader: () => data },
  ]);
  const context = await handler.query(request);
  if (context instanceof Response) throw new Error("unexpected route response");
  const router = createStaticRouter(handler.dataRoutes, context);
  return renderToString(createElement(StaticRouterProvider, { router, context }))
    .replace(/<script[\s\S]*?<\/script>/g, "")
    .replace(/<!-- -->/g, "");
}

const missing = () => Promise.reject(new PythonApiError(404, { error: "missing" }));
const failed = () => Promise.reject(new PythonApiError(503, { error: "unavailable" }));

// Day `number` of the September Season, or of October's after Day 28.
const legendDay = (number: number): RankedDaySummary => {
  const start = Date.parse("2026-09-07T05:00:00Z") + (number - 1) * 86_400_000;
  return {
    dayNumber: number > 28 ? number - 28 : number,
    label: "Ranked day",
    period: `${new Date(start).toISOString()} – ${new Date(start + 86_400_000).toISOString()}`,
    state: number > 28 ? "Live" : "Complete",
    offense: { attacks: 0, threeStars: 0, trophyGain: 0 },
    defense: { defenses: 0, threeStarsAgainst: 0, trophyLoss: 0 },
    trophyChange: 0,
    offenseEvents: [],
    defenseEvents: [],
    completeness: { state: "complete", reason: "Complete" },
    uncertainty: [],
  };
};

describe("Daily Legend log days", () => {
  const ended = [28, 27, 26, 25, 24].map(legendDay);
  const septemberSeason = {
    id: SEASON,
    anchor: "2026-09-07T05:00:00Z",
    currentDayNumber: 28,
    dayCount: 28,
    anchorSource: "official_league_history",
    anchorObservedAt: "2026-09-07T05:10:00Z",
  } as const;
  const afterReset = Date.parse("2026-10-05T05:10:00Z");

  it.each([
    ["no saved Season", null, [legendDay(29), ...ended]],
    ["an expired Season anchor", septemberSeason, ended],
  ])("keeps only the current Season's days with %s", (_, season, recentDays) => {
    const history = selectPlayerHistory(
      { ...PLAYER, season, currentDay: null, recentDays, seasonDays: [] },
      afterReset,
    );
    expect(history.map(({ seasonDay }) => seasonDay)).toEqual(
      season === null ? ["Day 1"] : [],
    );
  });

  it("keeps the saved Season's days before its Reset", () => {
    const history = selectPlayerHistory(
      { ...PLAYER, season: septemberSeason, currentDay: null, recentDays: ended },
      afterReset - 60 * 60 * 1000,
    );
    expect(history.map(({ seasonDay }) => seasonDay)).toEqual([
      "Day 28",
      "Day 27",
      "Day 26",
      "Day 25",
      "Day 24",
    ]);
  });

  it("drops the ended Season's days once the device clock passes Reset", async () => {
    const loadedAt = "2026-10-05T04:00:00Z";
    mocks.getPlayerLookup.mockReset().mockResolvedValue({ tag: TAG, state: "tracking" });
    const client = {
      getPlayer: vi.fn().mockResolvedValue({
        ...PLAYER,
        profile: {
          ...PLAYER.profile,
          freshness: { ...PLAYER.profile.freshness, observedAt: loadedAt },
        },
        season: septemberSeason,
        recentDays: ended,
      }),
      getPlayerSeasons: vi.fn().mockResolvedValue([]),
    };
    expect(await loadAndRender(client, null)).toContain("Day 28");

    // A sleeping device wakes after Reset, before the page's data is reread.
    let wall = Date.parse(loadedAt);
    const clock = vi.spyOn(Date, "now").mockImplementation(() => (wall += 5 * 3_600_000));
    try {
      const html = await loadAndRender(client, null);
      for (const number of [24, 25, 26, 27, 28])
        expect(html).not.toContain(`Day ${number}`);
    } finally {
      clock.mockRestore();
    }
  });
});

describe("past-Season view", () => {
  beforeEach(() => {
    mocks.createPythonClient.mockReset();
    mocks.getPlayerLookup.mockReset().mockResolvedValue({ tag: TAG, state: "tracking" });
  });

  it.each([
    ["official_league_history", null, "Not available yet"],
    ["official_league_history", 180, "180"],
    ["tracked_summary", null, "Not available yet"],
    ["tracked_summary", 1340, "1,340"],
  ] as const)(
    "shows %s official placement %s as the final rank",
    async (source, finalPlacement, expected) => {
      const html = await loadAndRender(
        {
          getPlayer: vi.fn().mockResolvedValue(PLAYER),
          getPlayerSeasons: vi.fn().mockResolvedValue([]),
          getPlayerSeason: vi.fn().mockResolvedValue({
            ...SUMMARY,
            source,
            // Clash Lens's own board rank is never the final rank.
            finalRank: 183,
            officialHistory: {
              observedAt: "2026-10-05T05:08:00Z",
              eodTrophies: 5812,
              finalPlacement,
            },
          }),
        },
        SEASON,
      );
      expect(html).toContain(`<dt>Final rank</dt><dd>${expected}</dd>`);
      expect(html).toContain("Final rank is the in-game rank from Clash of Clans.");
      expect(html).not.toContain("Clash Lens final rank");
      expect(html).not.toContain(">183<");
      if (source === "official_league_history") {
        expect(html).toContain("<dt>Final trophies</dt><dd>5,812</dd>");
      } else {
        expect(html).toContain("Final trophies: 5,812");
        expect(html).toContain("A Legend day runs from 05:00 to 05:00 UTC.");
        expect(html).toContain('<th scope="col">Trophy change</th>');
        expect(html).not.toContain('<th scope="col">Net</th>');
      }
    },
  );

  it("says a partial total covers 5 of 28 days and includes automatic losses", async () => {
    const html = await loadAndRender(
      {
        getPlayer: vi.fn().mockResolvedValue(PLAYER),
        getPlayerSeasons: vi.fn().mockResolvedValue([]),
        getPlayerSeason: vi.fn().mockResolvedValue(SUMMARY),
      },
      SEASON,
    );
    expect(html).toContain("records cover 5 of 28 Legend days");
    expect(html).toContain("Totals below cover the recorded days only.");
    expect(html).toContain(
      "<dt>Sum of daily trophy changes (5 of 28 days)</dt><dd>+300</dd>",
    );
    expect(html).toContain("includes automatic defense losses at Reset");
    expect(html).not.toContain("Net change");
    // Missing days alone do not make any known total unavailable.
    expect(html).not.toContain("Some daily totals are unavailable.");
    // The day's -30 trophy change stays apart from its +10 recorded battle net.
    expect(html).toContain("<td>-30</td><td>+10</td>");
  });

  it("shows each day's Reset rank, or Unknown when the board lacks the player", async () => {
    const html = await loadAndRender(
      {
        getPlayer: vi.fn().mockResolvedValue(PLAYER),
        getPlayerSeasons: vi.fn().mockResolvedValue([]),
        getPlayerSeason: vi.fn().mockResolvedValue({
          ...SUMMARY,
          dailyEntries: [
            { ...DAY, resetRank: 1042 },
            { ...DAY, dayNumber: 23 },
          ],
        }),
      },
      SEASON,
    );
    expect(html).toContain('<th scope="col">Reset rank</th>');
    // EOD change, then Reset rank, then attacks recorded.
    expect(html).toContain("<td>Unknown</td><td>1,042</td><td>8</td>");
    expect(html).toContain("<td>Unknown</td><td>Unknown</td><td>8</td>");
  });

  it("says a total is unavailable only when one is unknown", async () => {
    const html = await loadAndRender(
      {
        getPlayer: vi.fn().mockResolvedValue(PLAYER),
        getPlayerSeasons: vi.fn().mockResolvedValue([]),
        getPlayerSeason: vi.fn().mockResolvedValue({
          ...SUMMARY,
          netTrophyChange: null,
          dailyEntries: [{ ...DAY, netChange: null }],
        }),
      },
      SEASON,
    );
    expect(html).toContain(
      "<dt>Sum of daily trophy changes (5 of 28 days)</dt><dd>Unknown</dd>",
    );
    expect(html).toContain("Some daily totals are unavailable.");
  });

  it("names complete coverage without a partial warning", async () => {
    const html = await loadAndRender(
      {
        getPlayer: vi.fn().mockResolvedValue(PLAYER),
        getPlayerSeasons: vi.fn().mockResolvedValue([]),
        getPlayerSeason: vi.fn().mockResolvedValue({
          ...SUMMARY,
          daysObserved: 28,
          daysMissing: [],
          coverageState: "complete",
          unresolvedFlags: [],
        }),
      },
      SEASON,
    );
    expect(html).toContain("Records cover all 28 Legend days.");
    expect(html).not.toContain("Partial Season history");
  });

  it("keeps Current Season when the Season list fails", async () => {
    const html = await loadAndRender(
      {
        getPlayer: vi.fn().mockResolvedValue(PLAYER),
        getPlayerSeasons: vi.fn(failed),
        getPlayerSeason: vi.fn().mockResolvedValue(SUMMARY),
      },
      SEASON,
    );
    expect(html).toContain(CURRENT_LINK);
    expect(html).toContain("Season history could not be loaded.");
    expect(html).toContain(`href="/players/%232PP?season=${SEASON}">Try again</a>`);
    expect(html).toContain("<td>-30</td>");
  });

  it.each([
    ["the summary is missing", vi.fn(missing)],
    ["no summary or error came back", vi.fn().mockResolvedValue(null)],
  ])(
    "keeps Current Season with an unavailable profile when %s",
    async (_, getPlayerSeason) => {
      const html = await loadAndRender(
        { getPlayer: vi.fn(missing), getPlayerSeasons: vi.fn(failed), getPlayerSeason },
        SEASON,
      );
      expect(html).toContain(CURRENT_LINK);
      expect(html).toContain("Results for 5 Oct 2026 are unavailable.");
      expect(html).not.toContain("Saved Legend history");
    },
  );

  it.each([
    ["available", PLAYER],
    ["unavailable", null],
  ])(
    "shows a retryable Season-list failure with current profile %s",
    async (_, player) => {
      const client = {
        getPlayer: vi.fn().mockResolvedValue(player),
        getPlayerSeasons: vi.fn().mockImplementation(failed),
        getPlayerSeason: vi.fn(),
      };
      const html = await loadAndRender(client, null);
      expect(html).toContain("Season history could not be loaded.");
      expect(html).toContain('href="/players/%232PP">Try again</a>');
      if (player) expect(html).toContain("Nova");

      client.getPlayerSeasons.mockResolvedValue([]);
      const recoveredHtml = await loadAndRender(client, null);
      expect(recoveredHtml).not.toContain("Season history could not be loaded.");
      expect(recoveredHtml).not.toContain('aria-label="Seasons"');
    },
  );

  it("shows no Season navigation on the current page when the list is empty", async () => {
    const html = await loadAndRender(
      {
        getPlayer: vi.fn().mockResolvedValue(PLAYER),
        getPlayerSeasons: vi.fn().mockResolvedValue([]),
        getPlayerSeason: vi.fn(),
      },
      null,
    );
    expect(html).not.toContain('aria-label="Seasons"');
    expect(html).not.toContain("Current Season");
  });

  // Day 1 of the Season starting 5 Oct, after the September Season ended.
  const newSeasonClient = () => {
    const today = legendDay(29);
    const ended = [28, 27, 26, 25, 24].map(legendDay);
    const ref = { coverageState: "partial", daysObserved: 5, daysMissing: 23 } as const;
    return {
      getPlayer: vi.fn().mockResolvedValue({
        ...PLAYER,
        season: {
          id: "1791176400",
          anchor: "2026-10-05T05:00:00Z",
          currentDayNumber: 1,
          dayCount: 28,
          anchorSource: "official_league_history",
          anchorObservedAt: "2026-10-05T05:10:00Z",
        },
        currentDay: today,
        recentDays: [today, ...ended],
        seasonDays: [today],
      }),
      getPlayerSeasons: vi.fn().mockResolvedValue([
        // The Season ending 7 Sep has only the game's result, no Clash Lens days.
        {
          ...ref,
          seasonId: "1786338000",
          daysObserved: 0,
          daysMissing: 28,
          source: "official_league_history",
          officialHistory: { observedAt: "2026-09-07T06:00:00Z", eodTrophies: 5600 },
        },
        { ...ref, seasonId: SEASON, source: "tracked_summary", officialHistory: null },
      ]),
      getPlayerSeason: vi.fn().mockResolvedValue({
        ...SUMMARY,
        dailyEntries: ended.map((day) => ({
          ...DAY,
          dayNumber: day.dayNumber,
          period: day.period,
        })),
      }),
    };
  };

  it("keeps only the new Season's Day 1 in the log and the ended Season in Seasons", async () => {
    const client = newSeasonClient();
    const current = (await loadAndRender(client, null)).replace(/<[^>]*>/g, " ");
    expect(current).toContain("Day 1");
    for (const hidden of ["Date only", "Day 24", "Day 28", "30 Sep 2026", "4 Oct 2026"])
      expect(current).not.toContain(hidden);
    expect(current).toMatch(/Seasons\s+Current Season\s+5 Oct 2026/);
    expect(current).not.toContain("7 Sep 2026");

    const past = await loadAndRender(client, SEASON);
    for (const number of [24, 25, 26, 27, 28])
      expect(past).toContain(`<td>${number}</td>`);
    expect(past).toContain('<strong aria-current="page">5 Oct 2026</strong>');
  });

  it("sends an ended Season's day link to that Season with the day marked", async () => {
    mocks.createPythonClient.mockReturnValue(newSeasonClient());
    const request = new Request(
      "https://clashlens.example/players/%232PP?day=2026-10-04",
    );
    const response = await playerLoader({ request, params: { tag: TAG } } as never).then(
      () => null,
      (thrown: unknown) => thrown,
    );
    expect(response).toBeInstanceOf(Response);
    expect((response as Response).headers.get("Location")).toBe(
      `/players/%232PP?day=2026-10-04&season=${SEASON}#legend-day-2026-10-04`,
    );

    const html = await loadAndRender(newSeasonClient(), SEASON, "2026-10-04");
    expect(html.match(/aria-current="date"/g)).toHaveLength(1);
    expect(html).toContain(
      '<tr id="legend-day-2026-10-04" aria-current="date"><td>28</td>',
    );
    expect(html).toContain('<strong aria-current="page">5 Oct 2026</strong>');
  });

  it("says when an ended Season has no saved log for the linked day", async () => {
    const html = await loadAndRender(newSeasonClient(), SEASON, "2026-09-15");
    expect(html).toContain("No saved Legend log for 15 Sep 2026.");
    expect(html).not.toContain('aria-current="date"');
    expect(html).toContain("<td>28</td>");
  });

  it("opens a current Season day link in the Daily Legend log", async () => {
    const html = await loadAndRender(newSeasonClient(), null, "2026-10-05");
    expect(html).toContain('id="legend-day-2026-10-05" open=""');
    expect(html).toContain("Daily Legend log");
    expect(html).not.toContain("No saved Legend log");
  });

  it("keeps the no-log message for a day outside every tracked Season", async () => {
    // 20 Aug falls in the Season with only the game's result, not Clash Lens days.
    const html = await loadAndRender(newSeasonClient(), null, "2026-08-20");
    expect(html).toContain("No saved Legend log for 20 Aug 2026.");
    expect(html).toContain("Daily Legend log");
  });
});
