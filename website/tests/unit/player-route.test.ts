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
  startPlayerLookup: vi.fn(),
  lookupTimedOut: false,
}));

vi.mock("react", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react")>();
  return {
    ...actual,
    useState: (initialState: unknown) => {
      const state = actual.useState(initialState);
      return initialState === false && mocks.lookupTimedOut ? [true, state[1]] : state;
    },
  };
});

vi.mock("../../app/services/player-lookup.server", () => ({
  getPlayerLookup: mocks.getPlayerLookup,
  startPlayerLookup: mocks.startPlayerLookup,
}));

vi.mock("../../app/services/python.server", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../app/services/python.server")>();
  return { ...actual, createPythonClient: mocks.createPythonClient };
});

import type {
  HistoricalSeasonSummary,
  PlayerPage,
  RankedDaySummary,
  RefreshStatus,
  SummarizedSeasonRef,
} from "../../app/lib/contracts";
import { PythonApiError } from "../../app/services/python.server";
import PlayerRoute, { loader as playerLoader } from "../../app/routes/player";

const TAG = "#2PP";
const SEASON = "1785714000";

const SUMMARY: HistoricalSeasonSummary = {
  kind: "player-season-summary",
  tag: TAG,
  seasonId: SEASON,
  seasonStart: "2026-05-01T05:00:00+00:00",
  seasonEnd: "2026-05-29T05:00:00+00:00",
  startTrophies: 6000,
  endTrophies: 6280,
  finalRank: null,
  attackCount: 56,
  attackGain: 840,
  defenseCount: 28,
  defenseLoss: 560,
  netTrophyChange: 280,
  attackStars: { "0": 0, "1": 0, "2": 28, "3": 28 },
  defenseStars: { "0": 0, "1": 28, "2": 0, "3": 0 },
  attackStarsUnknown: 0,
  defenseStarsUnknown: 0,
  daysObserved: 28,
  daysMissing: [],
  coverageState: "partial",
  unresolvedFlags: [],
  dailyEntries: [],
  publishedAt: "2026-05-29T06:00:00+00:00",
  source: "tracked_summary",
  officialHistory: null,
};

const SEASONS: SummarizedSeasonRef[] = [
  {
    seasonId: SEASON,
    coverageState: "partial",
    daysObserved: 28,
    daysMissing: 0,
    source: "tracked_summary",
    officialHistory: null,
  },
];

const PLAYER = {
  kind: "player-page",
  tag: TAG,
  profile: {
    tag: TAG,
    name: "Nova",
    clan: "Example",
    trophies: 6000,
    freshness: { state: "fresh", observedAt: "2026-08-06T12:00:00Z", ageSeconds: 0 },
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
    observedAt: "2026-08-06T12:00:00Z",
    freshness: "fresh",
    confidence: "high",
    coverage: "complete",
    version: "v1",
  },
} satisfies PlayerPage;

const REFRESH_STATUS: RefreshStatus = {
  kind: "refresh-status",
  workId: "work_1",
  tag: TAG,
  state: "complete",
  progressPercent: 100,
  message: "Complete",
  publishedAt: "2026-08-06T12:00:00Z",
  player: PLAYER,
};

async function renderRoute(data: Awaited<ReturnType<typeof playerLoader>>) {
  const handler = createStaticHandler([
    { path: "/players/:tag", Component: PlayerRoute, loader: () => data },
  ]);
  const context = await handler.query(
    new Request("https://clashlens.example/players/%232PP?season=missing"),
  );
  if (context instanceof Response) throw new Error("unexpected route response");
  const router = createStaticRouter(handler.dataRoutes, context);
  return renderToString(createElement(StaticRouterProvider, { router, context }));
}

function requestFor(season: string | null) {
  const target = season === null ? "/players/%232PP" : `/players/%232PP?season=${season}`;
  return new Request(`https://clashlens.example${target}`);
}

describe("player route historical independence", () => {
  beforeEach(() => {
    mocks.createPythonClient.mockReset();
    mocks.getPlayerLookup.mockReset().mockResolvedValue({ tag: TAG, state: "tracking" });
    mocks.startPlayerLookup.mockReset();
  });

  it("returns the compact season even when the current profile is unavailable", async () => {
    mocks.createPythonClient.mockReturnValue({
      getPlayer: vi.fn(() => {
        throw new PythonApiError(404, { error: "missing" });
      }),
      getPlayerSeasons: vi.fn().mockResolvedValue(SEASONS),
      getPlayerSeason: vi.fn().mockResolvedValue(SUMMARY),
    });
    const data = await playerLoader({
      request: requestFor(SEASON),
      params: { tag: "#2PP" },
    } as never);
    expect(data.player).toBeNull();
    expect(data.error).not.toBeNull();
    expect(data.selectedSeason).toBe(SEASON);
    expect(data.historical).toMatchObject({ seasonId: SEASON, attackCount: 56 });
    expect(data.historicalError).toBeNull();
  });

  it("loads the profile, seasons, saved season, and refresh status in one waiting period", async () => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    let started = 0;
    const delayed = <T>(value: T) => {
      started += 1;
      return gate.then(() => value);
    };
    mocks.createPythonClient.mockReturnValue({
      getPlayer: vi.fn(() => delayed(PLAYER)),
      getPlayerSeasons: vi.fn(() => delayed(SEASONS)),
      getPlayerSeason: vi.fn(() => delayed(SUMMARY)),
      getRefreshStatus: vi.fn(() => delayed(REFRESH_STATUS)),
    });

    const loading = playerLoader({
      request: new Request(`${requestFor(SEASON).url}&refresh=work_1`),
      params: { tag: TAG },
    } as never);
    try {
      await vi.waitFor(() => expect(started).toBe(4));
    } finally {
      release();
    }
    const data = await loading;
    expect(data.player?.tag).toBe(TAG);
    expect(data.seasons).toEqual(SEASONS);
    expect(data.historical?.seasonId).toBe(SEASON);
    expect(data.refreshStatus?.workId).toBe("work_1");
  });

  it("keeps the profile available when optional season and refresh requests fail", async () => {
    mocks.createPythonClient.mockReturnValue({
      getPlayer: vi.fn().mockResolvedValue(PLAYER),
      getPlayerSeasons: vi.fn().mockRejectedValue(new PythonApiError(503, {})),
      getPlayerSeason: vi.fn().mockRejectedValue(new PythonApiError(404, {})),
      getRefreshStatus: vi.fn().mockRejectedValue(new PythonApiError(503, {})),
    });
    const data = await playerLoader({
      request: new Request(`${requestFor(SEASON).url}&refresh=work_1`),
      params: { tag: TAG },
    } as never);
    expect(data.player?.tag).toBe(TAG);
    expect(data.error).toBeNull();
    expect(data.seasons).toEqual([]);
    expect(data.historical).toBeNull();
    expect(data.historicalError).not.toBeNull();
    expect(data.refreshStatus).toBeNull();
    expect(data.refreshError).not.toBeNull();
  });

  it("shows saved-data fallbacks if the backend client cannot start", async () => {
    mocks.createPythonClient.mockImplementation(() => {
      throw new Error("backend configuration unavailable");
    });
    const data = await playerLoader({
      request: new Request(`${requestFor(SEASON).url}&refresh=work_1`),
      params: { tag: TAG },
    } as never);
    expect(data.player).toBeNull();
    expect(data.error).not.toBeNull();
    expect(data.seasons).toEqual([]);
    expect(data.historical).toBeNull();
    expect(data.historicalError).not.toBeNull();
    expect(data.refreshStatus).toBeNull();
    expect(data.refreshError).not.toBeNull();
  });

  it("reports an unavailable season without substituting live detail", async () => {
    mocks.createPythonClient.mockReturnValue({
      getPlayer: vi.fn().mockRejectedValue(new PythonApiError(404, { error: "missing" })),
      getPlayerSeasons: vi.fn().mockResolvedValue([]),
      getPlayerSeason: vi
        .fn()
        .mockRejectedValue(new PythonApiError(404, { error: "season_not_found" })),
    });
    const data = await playerLoader({
      request: requestFor("no-such-season"),
      params: { tag: "#2PP" },
    } as never);
    expect(data.player).toBeNull();
    expect(data.historical).toBeNull();
    expect(data.historicalError).not.toBeNull();
  });

  it("keeps retained history discoverable without a current profile", async () => {
    mocks.createPythonClient.mockReturnValue({
      getPlayer: vi.fn().mockRejectedValue(new PythonApiError(404, { error: "missing" })),
      getPlayerSeasons: vi.fn().mockResolvedValue(SEASONS),
      getPlayerSeason: vi.fn(),
    });
    const data = await playerLoader({
      request: requestFor(null),
      params: { tag: TAG },
    } as never);
    const html = await renderRoute({ ...data, selectedSeason: null });
    expect(html).toContain(`season=${SEASON}`);
  });

  it("does not show current-day content for a requested missing season", async () => {
    const html = await renderRoute({
      requestedTag: TAG,
      player: PLAYER,
      error: null,
      refreshStatus: null,
      refreshError: null,
      noJsIdempotencyKey: "test-idempotency-key",
      lookup: { tag: TAG, state: "tracking" },
      lookupError: null,
      seasons: SEASONS,
      selectedSeason: "missing",
      historical: null,
      historicalError: {
        error: { code: "missing", message: "That season is unavailable." },
      },
    });
    expect(html).not.toContain("Current Legend day");
    expect(html).not.toContain("Legend season");
  });

  it("keeps all 28 days and 448 battles in the page for search and print", async () => {
    const seasonStart = Date.parse("2026-09-07T05:00:00Z");
    const days: RankedDaySummary[] = Array.from({ length: 28 }, (_, dayIndex) => {
      const start = new Date(seasonStart + dayIndex * 86_400_000);
      const end = new Date(start.getTime() + 86_400_000);
      const battle = (slot: number) => ({
        battleId: `${dayIndex}-${slot}`,
        battleTimestamp: new Date(start.getTime() + slot * 1_800_000).toISOString(),
        opponent: {
          tag: "#Q0002",
          name: `Synthetic Clasher ${dayIndex * 16 + slot + 1}`,
        },
        destructionPercentage: 100,
        stars: 3,
        trophyChange: slot < 8 ? 40 : -40,
        perspectiveDisagreement: false,
        army: null,
        armyShareCode: "u1x0-2x1",
      });
      return {
        dayNumber: dayIndex + 1,
        label: `Day ${dayIndex + 1}`,
        period: `${start.toISOString()} – ${end.toISOString()}`,
        state: dayIndex === 27 ? "Live" : "Complete",
        startTrophies: 6000,
        offense: { attacks: 8, threeStars: 8, trophyGain: 320 },
        defense: { defenses: 8, threeStarsAgainst: 8, trophyLoss: 320 },
        trophyChange: 0,
        offenseEvents: Array.from({ length: 8 }, (_, slot) => battle(slot)),
        defenseEvents: Array.from({ length: 8 }, (_, slot) => battle(slot + 8)),
        completeness: { state: "complete", reason: "Complete" },
        uncertainty: [],
      };
    });
    const html = await renderRoute({
      requestedTag: TAG,
      player: { ...PLAYER, currentDay: days[27], recentDays: days, seasonDays: days },
      error: null,
      refreshStatus: null,
      refreshError: null,
      noJsIdempotencyKey: "test-idempotency-key",
      lookup: { tag: TAG, state: "tracking" },
      lookupError: null,
      seasons: [],
      selectedSeason: null,
      historical: null,
      historicalError: null,
    });
    expect(html.match(/class="legend-day"/g)).toHaveLength(28);
    expect(html.match(/class="battle-profile-link"/g)).toHaveLength(448);
    expect(html).toContain("Synthetic Clasher 448");
    expect(html).toContain("4 Oct 2026");
  });

  it.each(["", ".data"])(
    "reads the canonical page without a redirect loop (suffix %s)",
    async (suffix) => {
      const getPlayerSeason = vi.fn();
      mocks.createPythonClient.mockReturnValue({
        getPlayer: vi
          .fn()
          .mockRejectedValue(new PythonApiError(404, { error: "missing" })),
        getPlayerSeasons: vi.fn().mockResolvedValue([]),
        getPlayerSeason,
      });
      const data = await playerLoader({
        request: new Request(requestFor(null).url + suffix),
        params: { tag: "#2PP" },
      } as never);
      expect(data.selectedSeason).toBeNull();
      expect(data.historical).toBeNull();
      expect(data.historicalError).toBeNull();
      expect(getPlayerSeason).not.toHaveBeenCalled();
    },
  );
});

describe("automatic tag lookup", () => {
  beforeEach(() => {
    mocks.lookupTimedOut = false;
    mocks.createPythonClient.mockReturnValue({
      getPlayer: vi.fn().mockRejectedValue(new PythonApiError(404, {})),
      getPlayerSeasons: vi.fn().mockResolvedValue(SEASONS),
      getPlayerSeason: vi.fn().mockResolvedValue(SUMMARY),
    });
    mocks.startPlayerLookup
      .mockReset()
      .mockResolvedValue({ tag: TAG, state: "checking" });
  });

  it("starts a new tag during server rendering without a button or account", async () => {
    mocks.getPlayerLookup.mockResolvedValue({ tag: TAG, state: "unknown" });
    const result = await playerLoader({
      request: requestFor(null),
      params: { tag: TAG },
    } as never);
    expect(result.lookup?.state).toBe("checking");
    expect(mocks.startPlayerLookup).toHaveBeenCalledOnce();
    const html = await renderRoute(result);
    expect(html).toContain("Checking this tag");
    expect(html).not.toContain("Start tracking");
    expect(html).toContain("Check progress");
  });

  it.each([
    ["not_found", "Player not found"],
    ["not_in_legend", "not in Legend I"],
    ["uncertain", "could not confirm"],
    ["failed", "could not finish checking"],
  ])(
    "shows %s honestly and preserves saved seasons without another lookup",
    async (state, message) => {
      mocks.getPlayerLookup.mockResolvedValue({ tag: TAG, state });
      const result = await playerLoader({
        request: requestFor(SEASON),
        params: { tag: TAG },
      } as never);
      const html = await renderRoute(result);
      expect(html).toContain(message);
      expect(html).toContain("Historical seasons");
      expect(html).not.toContain("Current trophies");
      expect(mocks.startPlayerLookup).not.toHaveBeenCalled();
    },
  );

  it("hides the old current profile when newer evidence says the player left Legend I", async () => {
    mocks.createPythonClient.mockReturnValue({
      getPlayer: vi.fn().mockResolvedValue(PLAYER),
      getPlayerSeasons: vi.fn().mockResolvedValue(SEASONS),
      getPlayerSeason: vi.fn().mockResolvedValue(SUMMARY),
    });
    mocks.getPlayerLookup.mockResolvedValue({ tag: TAG, state: "not_in_legend" });
    const result = await playerLoader({
      request: requestFor(SEASON),
      params: { tag: TAG },
    } as never);
    const html = await renderRoute(result);
    expect(html).toContain("not in Legend I");
    expect(html).not.toContain("Current trophies");
    expect(html).not.toContain('class="player-refresh-form"');
    expect(html).toContain("Historical seasons");
  });

  it.each([null, SEASON])(
    "keeps history and hides the profile when lookup fails for season %s",
    async (season) => {
      const day: RankedDaySummary = {
        dayNumber: 1,
        label: "Day 1",
        period: "2026-09-07T05:00:00Z – 2026-09-08T05:00:00Z",
        state: "Complete",
        startTrophies: 6000,
        offense: { attacks: 0, threeStars: 0, trophyGain: 0 },
        defense: { defenses: 0, threeStarsAgainst: 0, trophyLoss: 0 },
        trophyChange: 0,
        offenseEvents: [],
        defenseEvents: [],
        completeness: { state: "complete", reason: "Complete" },
        uncertainty: [],
      };
      mocks.createPythonClient.mockReturnValue({
        getPlayer: vi.fn().mockResolvedValue({ ...PLAYER, seasonDays: [day] }),
        getPlayerSeasons: vi.fn().mockResolvedValue(SEASONS),
        getPlayerSeason: vi.fn().mockResolvedValue(SUMMARY),
      });
      mocks.getPlayerLookup.mockRejectedValue(new PythonApiError(503, {}));
      const result = await playerLoader({
        request: requestFor(season),
        params: { tag: TAG },
      } as never);
      const html = await renderRoute(result);
      expect(html).toContain(result.lookupError!.error.message);
      expect(html).toContain(
        "could not confirm whether this player is currently tracked",
      );
      expect(html).toContain("Historical seasons");
      expect(html).toContain(
        season === null ? "Saved Legend history" : "Daily trophy totals",
      );
      expect(html).not.toContain("Current trophies");
      expect(html).not.toContain("player-refresh-form");
    },
  );

  it.each([
    ["checking", "It may still be running"],
    ["tracking", "It may still be running"],
    ["not_found", "Player not found"],
    ["not_in_legend", "not in Legend I"],
    ["uncertain", "could not confirm"],
    ["failed", "could not finish checking"],
  ])("shows %s correctly after the polling timeout", async (state, message) => {
    mocks.lookupTimedOut = true;
    mocks.getPlayerLookup.mockResolvedValue({ tag: TAG, state });
    const result = await playerLoader({
      request: requestFor(null),
      params: { tag: TAG },
    } as never);
    const html = await renderRoute(result);
    expect(html).toContain(message);
    if (state !== "checking" && state !== "tracking") {
      expect(html).not.toContain("It may still be running");
    }
  });

  it("shows a limit refusal without claiming the check started", async () => {
    mocks.getPlayerLookup.mockResolvedValue({ tag: TAG, state: "unknown" });
    mocks.startPlayerLookup.mockRejectedValue(
      new PythonApiError(429, { error: "rate_limited" }),
    );
    const result = await playerLoader({
      request: requestFor(null),
      params: { tag: TAG },
    } as never);
    expect(result.lookup?.state).toBe("unknown");
    expect(result.lookupError?.error.code).toBe("rate_limited");
    expect(await renderRoute(result)).toContain("Waiting to check");
  });
});
