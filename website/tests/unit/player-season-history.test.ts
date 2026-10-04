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
} from "../../app/lib/contracts";
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

async function loadAndRender(client: Record<string, unknown>, season: string | null) {
  mocks.createPythonClient.mockReturnValue(client);
  const search = season === null ? "" : `?season=${season}`;
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

describe("past-Season view", () => {
  beforeEach(() => {
    mocks.createPythonClient.mockReset();
    mocks.getPlayerLookup.mockReset().mockResolvedValue({ tag: TAG, state: "tracking" });
  });

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

  it("shows no Season navigation on the current page when the list is empty", async () => {
    const html = await loadAndRender(
      {
        getPlayer: vi.fn().mockResolvedValue(PLAYER),
        getPlayerSeasons: vi.fn().mockResolvedValue([]),
        getPlayerSeason: vi.fn(),
      },
      null,
    );
    expect(html).not.toContain("Historical seasons");
    expect(html).not.toContain("Current Season");
  });
});
