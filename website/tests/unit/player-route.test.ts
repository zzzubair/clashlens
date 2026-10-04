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
  PlayerLookup,
  PlayerPage,
  RankedDaySummary,
  RefreshStatus,
  SummarizedSeasonRef,
} from "../../app/lib/contracts";
import { PythonApiError } from "../../app/services/python.server";
import PlayerRoute, {
  loader as playerLoader,
  playerLookupView,
} from "../../app/routes/player";
import { isRefreshStatusPayload } from "../../app/lib/validation";
import { createClientAddressContext } from "../../app/server/client-address.server";

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
  trackingState: "tracking",
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

const NEWER_PROFILE = {
  ...PLAYER.profile,
  freshness: { ...PLAYER.profile.freshness, observedAt: "2026-08-06T12:00:01Z" },
};

const SAVED_DAY: RankedDaySummary = {
  dayNumber: null,
  label: "Ranked day",
  period: "2026-09-07T05:00:00Z – 2026-09-08T05:00:00Z",
  state: "Complete",
  startTrophies: 6000,
  offense: { attacks: 1, threeStars: 1, trophyGain: 40 },
  defense: { defenses: 0, threeStarsAgainst: 0, trophyLoss: 0 },
  trophyChange: 40,
  offenseEvents: [
    {
      battleId: "saved-attack",
      battleTimestamp: "2026-09-07T13:00:00Z",
      opponent: { tag: "#2PY", name: "Saved opponent" },
      destructionPercentage: 100,
      stars: 3,
      trophyChange: 40,
      perspectiveDisagreement: false,
      armyShareCode: null,
    },
  ],
  defenseEvents: [],
  completeness: { state: "complete", reason: "Complete" },
  uncertainty: ["player_not_eligible"],
};

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

async function renderRoute(
  data: Awaited<ReturnType<typeof playerLoader>>,
  search = "?season=missing",
) {
  const handler = createStaticHandler([
    { path: "/players/:tag", Component: PlayerRoute, loader: () => data },
  ]);
  const context = await handler.query(
    new Request(`https://clashlens.example/players/%232PP${search}`),
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

  it("never shows official placement as the final rank", async () => {
    const officialHistory = {
      observedAt: "2026-08-04T12:05:00+00:00",
      eodTrophies: 5812,
      finalPlacement: 12,
    };
    const render = (historical: HistoricalSeasonSummary) =>
      renderRoute(
        {
          requestedTag: TAG,
          player: null,
          error: null,
          refreshStatus: null,
          refreshError: null,
          noJsIdempotencyKey: "test-idempotency-key",
          lookup: null,
          lookupError: null,
          seasons: SEASONS,
          selectedSeason: SEASON,
          historical,
          historicalError: null,
        },
        `?season=${SEASON}`,
      );
    const official = await render({
      ...SUMMARY,
      source: "official_league_history",
      finalRank: null,
      officialHistory,
    });
    expect(official).toContain("<dt>Final rank</dt><dd>Unknown</dd>");
    expect(official).not.toContain(">12<");
    const tracked = await render({ ...SUMMARY, finalRank: 3, officialHistory });
    expect(tracked).toContain("<dt>Final rank</dt><dd>3</dd>");
    expect(tracked).toContain("Final trophies: <!-- -->5812");
    expect(tracked).not.toContain(">12<");
    expect(tracked).toContain("A Legend day runs from 05:00 to 05:00 UTC.");
    expect(tracked).toContain('<th scope="col">Trophy change</th>');
    expect(tracked).not.toContain('<th scope="col">Net</th>');
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

  it("shows all nine attacks the game returned and why the day is partial", async () => {
    // Audit example #J9Y9J80L: nine real attacks on one Legend day.
    const attack = SAVED_DAY.offenseEvents[0];
    const day: RankedDaySummary = {
      ...SAVED_DAY,
      state: "Partial",
      offense: { attacks: 9, threeStars: 9, trophyGain: 360 },
      offenseEvents: Array.from({ length: 9 }, (_, index) => ({
        ...attack,
        battleId: `attack-${index + 1}`,
        opponent: { ...attack.opponent, name: `Opponent ${index + 1}` },
      })),
      completeness: { state: "partial", reason: "attack_count_exceeds_eight" },
      uncertainty: ["attack_count_exceeds_eight"],
    };
    const html = await renderRoute(
      {
        requestedTag: TAG,
        player: { ...PLAYER, recentDays: [day], seasonDays: [day] },
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
      },
      "",
    );
    expect(html.match(/class="battle-profile-link"/g)).toHaveLength(9);
    expect(html).toContain("Opponent 9");
    expect(html).not.toContain("Empty attack slot 9");
    expect(html).toContain(
      "Clash of Clans returned 9 attacks for this day, more than the usual 8, so this day is marked partial.",
    );
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
    const request = requestFor(null);
    const result = await playerLoader({
      request,
      params: { tag: TAG },
      context: createClientAddressContext({})(request, { address: "198.51.100.9" }),
    } as never);
    expect(result.lookup?.state).toBe("checking");
    expect(mocks.startPlayerLookup).toHaveBeenCalledExactlyOnceWith("198.51.100.9", TAG);
    const html = await renderRoute(result);
    expect(html).toContain("Checking this tag");
    expect(html).not.toContain("Start tracking");
    expect(html).toContain("Check progress");
  });

  it.each([
    ["not_found", "Player not found"],
    ["not_in_legend", "not in Legend I. Clash Lens tracks Legend League players only."],
    ["uncertain", "Clash Lens tracks Legend League players only."],
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
      const visible = html.split("<script")[0];
      expect(visible).not.toContain("The requested player data is not available.");
      expect(html).toContain("Historical seasons");
      expect(html).not.toContain("Current trophies");
      expect(mocks.startPlayerLookup).not.toHaveBeenCalled();
    },
  );

  it("tells a Clasher to check a mistyped tag instead of refreshing", async () => {
    const result = await playerLoader({
      request: new Request("https://clashlens.example/players/not-a-tag"),
      params: { tag: "not-a-tag" },
    } as never);
    const html = await renderRoute(result);
    expect(html).toContain("The submitted player tag is not valid.");
    expect(html).toContain("Check the tag and try again.");
    expect(html).not.toContain("Try refreshing");
  });

  it("hides the old current profile when newer evidence says the player left Legend I", async () => {
    mocks.createPythonClient.mockReturnValue({
      getPlayer: vi.fn().mockResolvedValue({ ...PLAYER, trackingState: "not_in_legend" }),
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
      mocks.createPythonClient.mockReturnValue({
        getPlayer: vi.fn().mockResolvedValue({
          ...PLAYER,
          trackingState: "uncertain",
          seasonDays: [SAVED_DAY],
        }),
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
      expect(html).toContain("could not confirm they are in Legend I");
      expect(html).toContain("Historical seasons");
      expect(html).toContain(
        season === null ? "Saved Legend history" : "Daily trophy totals",
      );
      expect(html).not.toContain("Current trophies");
      expect(html).not.toContain("player-refresh-form");
    },
  );

  it.each(["profile", "refresh"])(
    "lets a tracking lookup decide over a departed %s response",
    async (source) => {
      const departed = {
        ...PLAYER,
        trackingState: "not_in_legend",
        profile: NEWER_PROFILE,
      };
      mocks.createPythonClient.mockReturnValue({
        getPlayer: vi.fn().mockResolvedValue(source === "profile" ? departed : PLAYER),
        getPlayerSeasons: vi.fn().mockResolvedValue(SEASONS),
        getPlayerSeason: vi.fn().mockResolvedValue(SUMMARY),
        getRefreshStatus: vi
          .fn()
          .mockResolvedValue({ ...REFRESH_STATUS, player: departed }),
      });
      mocks.getPlayerLookup.mockResolvedValue({ tag: TAG, state: "tracking" });
      const result = await playerLoader({
        request: new Request(
          `${requestFor(null).url}${source === "refresh" ? "?refresh=work_1" : ""}`,
        ),
        params: { tag: TAG },
      } as never);
      const html = await renderRoute(result);
      expect(html).toContain(
        "Now tracking in Legend I. The first results are being prepared.",
      );
      expect(html).not.toContain("not in Legend I");
      expect(html).not.toContain("Current trophies");
      expect(html).not.toContain("player-refresh-form");
    },
  );

  it("shows a confirmed tracked profile despite an unavailable lookup", async () => {
    mocks.createPythonClient.mockReturnValue({
      getPlayer: vi.fn().mockResolvedValue(PLAYER),
      getPlayerSeasons: vi.fn().mockResolvedValue(SEASONS),
      getPlayerSeason: vi.fn().mockResolvedValue(SUMMARY),
    });
    mocks.getPlayerLookup.mockRejectedValue(new PythonApiError(503, {}));
    const result = await playerLoader({
      request: requestFor(null),
      params: { tag: TAG },
    } as never);
    const html = await renderRoute(result);
    expect(html).toContain("Current trophies");
    expect(html).toContain("player-refresh-form");
    expect(html).toContain(result.lookupError!.error.message);
  });

  it.each([false, true])(
    "shows a player still waiting for their Season reset without an old current total: %s",
    async (pending) => {
      const player = {
        ...PLAYER,
        profile: { ...PLAYER.profile, trophies: 6400, seasonResetPending: pending },
      };
      mocks.createPythonClient.mockReturnValue({
        getPlayer: vi.fn().mockResolvedValue(player),
        getPlayerSeasons: vi.fn().mockResolvedValue(SEASONS),
        getPlayerSeason: vi.fn().mockResolvedValue(SUMMARY),
      });
      const result = await playerLoader({
        request: requestFor(null),
        params: { tag: TAG },
      } as never);
      const html = (await renderRoute(result)).replaceAll("<!-- -->", "");
      const count = /<strong class="player-trophy-count[^"]*">(.*?)<\/strong>/.exec(html);
      expect(count?.[1]).toContain(
        pending ? "Waiting for this player&#x27;s Season reset" : "6,400",
      );
      expect(count?.[1].includes("6,400")).toBe(!pending);
      expect(html.includes("Last saved before the reset: 6,400")).toBe(pending);
    },
  );

  it.each(
    ["recent", "current", "both"].flatMap((source) =>
      [false, true].map((lookupFailed) => ({ source, lookupFailed })),
    ),
  )(
    "keeps $source dated history when lookup failure is $lookupFailed",
    async ({ source, lookupFailed }) => {
      mocks.createPythonClient.mockReturnValue({
        getPlayer: vi.fn().mockResolvedValue({
          ...PLAYER,
          trackingState: "not_in_legend",
          season: null,
          seasonDays: [],
          currentDay: source === "recent" ? null : SAVED_DAY,
          recentDays: source === "current" ? [] : [SAVED_DAY],
        }),
        getPlayerSeasons: vi.fn().mockResolvedValue(SEASONS),
        getPlayerSeason: vi.fn(),
      });
      if (lookupFailed)
        mocks.getPlayerLookup.mockRejectedValue(new PythonApiError(503, {}));
      else mocks.getPlayerLookup.mockResolvedValue({ tag: TAG, state: "tracking" });
      const result = await playerLoader({
        request: requestFor(null),
        params: { tag: TAG },
      } as never);
      const html = await renderRoute(result);
      expect(html).toContain("Saved Legend history");
      expect(html).toContain("?day=2026-09-07#battle-saved-attack");
      expect(html.match(/id="battle-saved-attack"/g)).toHaveLength(1);
      expect(html).toContain("Saved opponent");
      expect(html).not.toContain("Current trophies");
      expect(html).not.toContain("player-refresh-form");
      expect(result.player?.season).toBeNull();
      expect(
        (result.player?.currentDay ?? result.player?.recentDays[0])?.dayNumber,
      ).toBeNull();
      if (lookupFailed) expect(html).toContain(result.lookupError!.error.message);
    },
  );

  it("keeps dated history on tracked pages without a confirmed season", async () => {
    mocks.createPythonClient.mockReturnValue({
      getPlayer: vi
        .fn()
        .mockResolvedValue({ ...PLAYER, currentDay: SAVED_DAY, recentDays: [SAVED_DAY] }),
      getPlayerSeasons: vi.fn().mockResolvedValue([]),
    });
    mocks.getPlayerLookup.mockResolvedValue({ tag: TAG, state: "tracking" });
    const result = await playerLoader({
      request: requestFor(null),
      params: { tag: TAG },
    } as never);
    const html = await renderRoute(result);
    expect(html).toContain("Current trophies");
    expect(html.match(/id="battle-saved-attack"/g)).toHaveLength(1);
    expect(result.player?.season).toBeNull();
  });

  it("shows an old profile's age and when battle history was last published", async () => {
    const stale = {
      ...PLAYER,
      profile: {
        ...PLAYER.profile,
        freshness: {
          state: "stale",
          observedAt: "2026-08-06T12:00:00Z",
          ageSeconds: 7_300,
        },
        battleHistoryUpdatedAt: "2026-08-06T11:40:00Z",
      },
    } satisfies PlayerPage;
    const render = async (player: PlayerPage) => {
      mocks.createPythonClient.mockReturnValue({
        getPlayer: vi.fn().mockResolvedValue(player),
        getPlayerSeasons: vi.fn().mockResolvedValue([]),
      });
      const result = await playerLoader({
        request: requestFor(null),
        params: { tag: TAG },
      } as never);
      const html = (await renderRoute(result)).split("<script>")[0];
      return html.replaceAll("<!-- -->", "").replace(/<[^>]+>/g, "");
    };
    expect(await render(stale)).toContain(
      "Updated 6 Aug 2026, 12:00 UTC · 2 hours oldBattle history updated 6 Aug 2026, 11:40 UTC · 2 hours old",
    );
    const fresh = await render({
      ...PLAYER,
      profile: { ...PLAYER.profile, battleHistoryUpdatedAt: null },
    });
    expect(fresh).toContain(
      "Updated 6 Aug 2026, 12:00 UTCBattle history updated not yet",
    );
    const oldHistory = await render({
      ...PLAYER,
      profile: { ...PLAYER.profile, battleHistoryUpdatedAt: "2026-08-06T11:40:00Z" },
    });
    expect(oldHistory).toContain(
      "Updated 6 Aug 2026, 12:00 UTCBattle history updated 6 Aug 2026, 11:40 UTC · 20 minutes old",
    );
  });

  it("keeps a newly published battle when a completed Refresh has the same check time", async () => {
    mocks.createPythonClient.mockReturnValue({
      getPlayer: vi
        .fn()
        .mockResolvedValue({ ...PLAYER, currentDay: SAVED_DAY, recentDays: [SAVED_DAY] }),
      getPlayerSeasons: vi.fn().mockResolvedValue([]),
      getRefreshStatus: vi.fn().mockResolvedValue(REFRESH_STATUS),
    });
    mocks.getPlayerLookup.mockResolvedValue({ tag: TAG, state: "tracking" });
    const result = await playerLoader({
      request: new Request(`${requestFor(null).url}?refresh=work_1`),
      params: { tag: TAG },
    } as never);
    const html = await renderRoute(result);
    expect(html).toContain("Current trophies");
    expect(html.match(/id="battle-saved-attack"/g)).toHaveLength(1);
  });

  it.each(
    (["tracking", "not_in_legend"] as const).flatMap((trackingState) =>
      [false, true].flatMap((lookupFailed) =>
        ["profile", "refresh"].flatMap((response) =>
          ["recent", "current", "both"].map((source) => ({
            trackingState,
            lookupFailed,
            response,
            source,
          })),
        ),
      ),
    ),
  )(
    "keeps mixed season history for $trackingState, $response, $source, lookup failure $lookupFailed",
    async ({ trackingState, lookupFailed, response, source }) => {
      const confirmedDay: RankedDaySummary = {
        ...SAVED_DAY,
        dayNumber: 2,
        period: "2026-09-08T05:00:00Z – 2026-09-09T05:00:00Z",
        uncertainty: [],
        offenseEvents: SAVED_DAY.offenseEvents.map((event) => ({
          ...event,
          battleId: "confirmed-attack",
          battleTimestamp: "2026-09-08T13:00:00Z",
          opponent: { ...event.opponent, name: "Confirmed opponent" },
        })),
      };
      const datedDay = { ...SAVED_DAY, dayNumber: 17 };
      const displayed: PlayerPage = {
        ...PLAYER,
        trackingState,
        profile: NEWER_PROFILE,
        season: {
          id: SEASON,
          anchor: "2026-09-07T05:00:00Z",
          currentDayNumber: 2,
          dayCount: 28,
          anchorSource: "official_league_history",
          anchorObservedAt: "2026-09-08T13:00:00Z",
        },
        seasonDays: [confirmedDay],
        recentDays: [
          { ...confirmedDay, period: "2026-09-08T05:00:00+00:00" },
          ...(source === "current" ? [] : [datedDay]),
        ],
        currentDay:
          source === "recent"
            ? null
            : {
                ...datedDay,
                period: "2026-09-07T05:00:00+00:00 – 2026-09-08T05:00:00+00:00",
              },
      };
      mocks.createPythonClient.mockReturnValue({
        getPlayer: vi.fn().mockResolvedValue(response === "profile" ? displayed : PLAYER),
        getPlayerSeasons: vi.fn().mockResolvedValue(SEASONS),
        getRefreshStatus: vi
          .fn()
          .mockResolvedValue({ ...REFRESH_STATUS, player: displayed }),
      });
      if (lookupFailed)
        mocks.getPlayerLookup.mockRejectedValue(new PythonApiError(503, {}));
      else mocks.getPlayerLookup.mockResolvedValue({ tag: TAG, state: "tracking" });
      const result = await playerLoader({
        request: new Request(
          `${requestFor(null).url}${response === "refresh" ? "?refresh=work_1" : ""}`,
        ),
        params: { tag: TAG },
      } as never);
      const markup = (await renderRoute(result, "?day=2026-09-07")).split("<script>")[0];
      expect(markup.match(/id="battle-saved-attack"/g)).toHaveLength(1);
      expect(markup.match(/id="battle-confirmed-attack"/g)).toHaveLength(1);
      expect(markup).toContain("?day=2026-09-07#battle-saved-attack");
      expect(markup).toContain("?day=2026-09-08#battle-confirmed-attack");
      expect(markup).not.toContain("No saved Legend log");
      const text = markup.replace(/<[^>]*>/g, "");
      expect(text).toContain("Day 2");
      expect(text).toContain("Date only");
      expect(text).not.toContain("Day 17");
      expect(displayed.seasonDays).toEqual([confirmedDay]);
      if (trackingState === "tracking") {
        expect(markup).toContain("Current trophies");
        expect(markup).toContain("player-refresh-form");
      } else {
        expect(markup).toContain("not in Legend I");
        expect(markup).not.toContain("Current trophies");
        expect(markup).not.toContain("player-refresh-form");
      }
      if (lookupFailed) expect(markup).toContain(result.lookupError!.error.message);
    },
  );

  it.each([
    ["tracking", true],
    ["not_in_legend", true],
    ["uncertain", true],
    [undefined, false],
    [true, false],
    ["unknown", false],
  ])("validates tracking state %s in Refresh results", (trackingState, accepted) => {
    expect(
      isRefreshStatusPayload({ ...REFRESH_STATUS, player: { ...PLAYER, trackingState } }),
    ).toBe(accepted);
  });

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

  it("explains a tracked player without a Season instead of preparing results", async () => {
    // Even if a minute had passed, it is not a slow check.
    mocks.lookupTimedOut = true;
    mocks.getPlayerLookup.mockResolvedValue({
      tag: TAG,
      state: "tracking",
      reason: "no_legend_battles",
      profile: { name: "Season Clasher", clan: "Synthetic Clan", trophies: 5000 },
    });
    const result = await playerLoader({
      request: requestFor(null),
      params: { tag: TAG },
    } as never);
    const html = await renderRoute(result);
    expect(html).toContain(
      "Season Clasher is in Legend League but hasn&#x27;t played a Legend League battle this Season.",
    );
    expect(html).toContain("This page updates as soon as they play.");
    expect(html).toContain("Synthetic Clan");
    expect(html).toContain("5,000");
    expect(html).not.toContain("Current trophies");
    expect(html).not.toContain("being prepared");
    expect(html).not.toContain("It may still be running");
    expect(html).not.toContain("Daily Legend log");
    expect(mocks.startPlayerLookup).not.toHaveBeenCalled();
  });

  it("explains a newest Season 0 profile over saved results and keeps saved history", async () => {
    mocks.createPythonClient.mockReturnValue({
      getPlayer: vi.fn().mockResolvedValue({ ...PLAYER, seasonDays: [SAVED_DAY] }),
      getPlayerSeasons: vi.fn().mockResolvedValue(SEASONS),
      getPlayerSeason: vi.fn().mockResolvedValue(SUMMARY),
    });
    mocks.getPlayerLookup.mockResolvedValue({
      tag: TAG,
      state: "tracking",
      reason: "no_legend_battles",
      profile: { name: "Nova", clan: "Example", trophies: 5000 },
    });
    const result = await playerLoader({
      request: requestFor(null),
      params: { tag: TAG },
    } as never);
    const html = await renderRoute(result, "");
    expect(html).toContain(
      "Nova is in Legend League but hasn&#x27;t played a Legend League battle this Season.",
    );
    expect(html).toContain("5,000");
    expect(html).not.toContain("Current trophies");
    expect(html).not.toContain('class="player-refresh-form"');
    expect(html).toContain("Saved Legend history");
    expect(html).toContain("Saved opponent");
  });

  it.each([
    "no_legend_battles",
    "season_unconfirmed",
    "unknown_tier",
    "profile_rejected",
  ])("rereads an explained %s page once a minute, never once a second", (reason) => {
    const view = playerLookupView(
      PLAYER,
      { tag: TAG, state: "tracking", reason } as PlayerLookup,
      true,
    );
    expect(view).toMatchObject({
      trackedPlayer: null,
      minuteChecks: true,
      isChecking: false,
    });
  });

  it("keeps rereading once a minute after a failed lookup, without showing older results", () => {
    expect(playerLookupView(PLAYER, null, true)).toMatchObject({
      trackedPlayer: null,
      lookup: null,
      minuteChecks: true,
      isChecking: false,
    });
    // Before any explanation a failed lookup still shows the saved page.
    expect(playerLookupView(PLAYER, null, false).trackedPlayer).toBe(PLAYER);
  });

  it("keeps rereading once a minute when the profile is accepted between the two reads", () => {
    expect(playerLookupView(null, { tag: TAG, state: "tracking" }, true)).toMatchObject({
      minuteChecks: true,
      isChecking: false,
    });
  });

  it.each([
    [PLAYER, { tag: TAG, state: "tracking" }],
    [null, { tag: TAG, state: "not_in_legend" }],
  ] as const)(
    "stops rereading after a successful lookup gives an answer",
    (player, lookup) => {
      expect(playerLookupView(player, lookup, true).minuteChecks).toBe(false);
    },
  );

  it("lets a final lookup outrank an older saved page after an explanation", () => {
    expect(
      playerLookupView(PLAYER, { tag: TAG, state: "not_in_legend" }, true),
    ).toMatchObject({
      trackedPlayer: null,
      lookup: { state: "not_in_legend" },
      minuteChecks: false,
    });
  });

  it("lets a final lookup outrank an older tracked page on a fresh visit", () => {
    expect(
      playerLookupView(PLAYER, { tag: TAG, state: "not_in_legend" }, false),
    ).toMatchObject({
      trackedPlayer: null,
      lookup: { state: "not_in_legend" },
      minuteChecks: false,
      isChecking: false,
    });
  });

  it("keeps rereading once a minute when a tracking lookup meets an older inactive page", () => {
    const older = { ...PLAYER, trackingState: "not_in_legend" } as PlayerPage;
    expect(playerLookupView(older, { tag: TAG, state: "tracking" }, true)).toMatchObject({
      trackedPlayer: null,
      lookup: { state: "tracking" },
      minuteChecks: true,
      isChecking: false,
    });
  });

  it("lets a tracking lookup outrank an older inactive page on a fresh visit", () => {
    const older = { ...PLAYER, trackingState: "not_in_legend" } as PlayerPage;
    expect(playerLookupView(older, { tag: TAG, state: "tracking" }, false)).toMatchObject(
      {
        trackedPlayer: null,
        lookup: { state: "tracking" },
        isChecking: true,
      },
    );
  });

  it("lets an explanation outrank an accepted saved page ", () => {
    const lookup = {
      tag: TAG,
      state: "tracking",
      reason: "no_legend_battles",
    } as PlayerLookup;
    expect(playerLookupView(PLAYER, lookup, true)).toMatchObject({
      trackedPlayer: null,
      lookup,
      minuteChecks: true,
      isChecking: false,
    });
  });

  it("keeps the one-second check for a first-time lookup", () => {
    expect(playerLookupView(null, { tag: TAG, state: "tracking" }, false)).toMatchObject({
      minuteChecks: false,
      isChecking: true,
    });
  });

  it.each([
    ["season_unconfirmed", "has not confirmed this player&#x27;s Season yet"],
    ["unknown_tier", "a league we do not recognize"],
    ["profile_rejected", "player details we could not use"],
    ["pending", "The first results are being prepared."],
  ])("explains a tracked player with reason %s", async (reason, message) => {
    mocks.getPlayerLookup.mockResolvedValue({ tag: TAG, state: "tracking", reason });
    const result = await playerLoader({
      request: requestFor(null),
      params: { tag: TAG },
    } as never);
    const html = await renderRoute(result);
    expect(html).toContain(message);
    expect(html).not.toContain("5,000");
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

describe("player day honesty", () => {
  const ENDED_DAY: RankedDaySummary = {
    ...SAVED_DAY,
    dayNumber: 3,
    period: "2026-10-02T05:00:00Z – 2026-10-03T05:00:00Z",
    state: "Partial",
    offense: { attacks: 8, threeStars: 6, trophyGain: 310 },
    defense: { defenses: 8, threeStarsAgainst: 2, trophyLoss: 284 },
    trophyChange: null,
    offenseEvents: [],
    defenseEvents: [],
    completeness: { state: "partial", reason: "missing_end_baseline" },
    uncertainty: ["missing_end_baseline"],
  };
  const TODAY: RankedDaySummary = {
    ...ENDED_DAY,
    dayNumber: 4,
    period: "2026-10-03T05:00:00Z – 2026-10-04T05:00:00Z",
    state: "Live",
    offense: { attacks: 0, threeStars: 0, trophyGain: 0 },
    defense: { defenses: 0, threeStarsAgainst: 0, trophyLoss: 0 },
    uncertainty: ["missing_end_battle_log_baseline", "missing_start_baseline"],
  };

  function page(days: RankedDaySummary[], extra: Partial<PlayerPage> = {}) {
    return renderRoute(
      {
        requestedTag: TAG,
        player: { ...PLAYER, recentDays: days, seasonDays: days, ...extra },
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
      },
      "",
    ).then((html) => html.split("<script>")[0].replaceAll("<!-- -->", ""));
  }

  function dayHtml(html: string, key: string) {
    return html.split(`id="legend-day-${key}"`)[1].split("</details>")[0];
  }

  const net = (value: string) =>
    new RegExp(`<small>Trophy change</small><strong class="[^"]+">${value}</strong>`);

  it("keeps an unknown daily net unknown while showing recorded battle net", async () => {
    const zero = { ...ENDED_DAY, period: SAVED_DAY.period, trophyChange: 0 };
    const html = await page([ENDED_DAY, zero]);
    const ended = dayHtml(html, "2026-10-02");
    expect(ended).toMatch(net("Unknown"));
    expect(ended).toContain("Result unknown");
    expect(ended).toContain("Recorded battle net +26");
    expect(ended).toContain("Trophies at the end of this day were not recorded.");
    expect(ended).toContain("8 recorded");
    expect(ended).not.toContain("missing_end_baseline");
    expect(dayHtml(html, "2026-09-07")).toMatch(net("0"));
  });

  it("does not invent net zero for a current day with no recorded battles", async () => {
    const html = await page([TODAY], { currentDay: TODAY });
    const today = dayHtml(html, "2026-10-03");
    expect(today).toContain("In progress");
    expect(today).toMatch(net("Unknown"));
    expect(today).toContain("Recorded battle net 0");
    expect(today).toContain("Ending evidence arrives after Reset.");
    expect(today).toContain("Trophies at the start of this day were not recorded.");
    expect(today).toContain('aria-label="Attack 1 not recorded"');
    expect(today).toContain('aria-hidden="true">Not recorded</span>');
    expect(today.match(/>Not recorded</g)).toHaveLength(16);
    expect(html).toContain('id="legend-day-2026-10-03" open=""');

    const missingDefense = { ...TODAY, defense: { ...TODAY.defense, trophyLoss: null } };
    expect(
      dayHtml(await page([missingDefense], { currentDay: missingDefense }), "2026-10-03"),
    ).toContain("Recorded battle net Unknown");
  });

  it("does not label an ended saved Live row as today's result", async () => {
    const ended = { ...TODAY, trophyChange: 12, uncertainty: [] };
    for (const currentDay of [null, ENDED_DAY]) {
      const html = await page([ended, ENDED_DAY], { currentDay });
      const row = dayHtml(html, "2026-10-03");
      expect(row).not.toContain("In progress");
      expect(row).toContain("Incomplete");
      expect(row).toContain("Final evidence for this day has not been processed yet.");
      expect(html).not.toContain('id="legend-day-2026-10-03" open=""');
    }
    const html = await page([ENDED_DAY], { currentDay: ENDED_DAY });
    expect(dayHtml(html, "2026-10-02")).toContain("In progress");
    expect(dayHtml(html, "2026-10-02")).toContain("Ending evidence arrives after Reset.");
  });

  const battles = (id: string, start: string, changes: number[]) =>
    changes.map((trophyChange, slot) => ({
      ...SAVED_DAY.offenseEvents[0],
      battleId: `${id}-${slot}`,
      battleTimestamp: new Date(Date.parse(start) + (slot + 1) * 3_600_000).toISOString(),
      trophyChange,
    }));

  // Prodigi's Day 24: no start-of-day check, but 8 attacks and 8 defenses,
  // so Python saved the net the battles add up to.
  const DAY_24: RankedDaySummary = {
    ...ENDED_DAY,
    dayNumber: 24,
    period: "2026-09-30T05:00:00Z – 2026-10-01T05:00:00Z",
    offense: { attacks: 8, threeStars: 7, trophyGain: 300 },
    defense: { defenses: 8, threeStarsAgainst: 3, trophyLoss: 311 },
    trophyChange: -11,
    battlesComplete: true,
    offenseEvents: battles(
      "a24",
      "2026-09-30T05:00:00Z",
      [40, 40, 40, 40, 40, 40, 40, 20],
    ),
    defenseEvents: battles(
      "d24",
      "2026-09-30T05:00:00Z",
      [-40, -40, -40, -40, -40, -40, -40, -31],
    ),
    uncertainty: ["missing_start_battle_log_baseline", "missing_start_baseline"],
  };

  it("calls a finished day with all 8 attacks and defenses a provisional result", async () => {
    const row = dayHtml(await page([DAY_24]), "2026-09-30");
    expect(row).toMatch(net("-11"));
    expect(row).toContain("Provisional result");
    expect(row).not.toContain("Incomplete");
    expect(row).not.toContain("so far");
    expect(await page([DAY_24])).toContain("A Legend day runs from 05:00 to 05:00 UTC.");
    expect(row).toContain("The battle log was not checked at the start of this day.");

    // Without every battle recorded, a saved number with gaps stays incomplete.
    const gaps = dayHtml(
      await page([{ ...DAY_24, battlesComplete: false }]),
      "2026-09-30",
    );
    expect(gaps).toMatch(net("-11"));
    expect(gaps).toContain("Incomplete");
  });

  it("shows today's net so far only when every battle so far is recorded", async () => {
    // Prodigi's Day 27, in progress: 5,412 + 295 - 139 = 5,568 trophies now.
    const day27: RankedDaySummary = {
      ...TODAY,
      dayNumber: 27,
      offense: { attacks: 8, threeStars: 7, trophyGain: 295 },
      defense: { defenses: 4, threeStarsAgainst: 2, trophyLoss: 139 },
      battlesComplete: true,
      offenseEvents: battles(
        "a27",
        "2026-10-03T05:00:00Z",
        [40, 40, 40, 40, 40, 40, 40, 15],
      ),
      defenseEvents: battles("d27", "2026-10-03T05:00:00Z", [-40, -40, -40, -19]),
      uncertainty: [
        "missing_end_battle_log_baseline",
        "automatic_defense_basis_unavailable",
      ],
    };
    const today = dayHtml(await page([day27], { currentDay: day27 }), "2026-10-03");
    expect(today).toContain("In progress");
    expect(today).toMatch(
      /<small>Trophy change<\/small><strong class="[^"]+">\+156<\/strong><span>so far<\/span>/,
    );

    const gap = { ...day27, battlesComplete: false };
    const unknown = dayHtml(await page([gap], { currentDay: gap }), "2026-10-03");
    expect(unknown).toMatch(net("Unknown"));
    expect(unknown).not.toContain("so far");
  });

  it("keeps a legacy complete day provisional", async () => {
    const complete = {
      ...ENDED_DAY,
      state: "Complete" as const,
      trophyChange: 26,
      completeness: { state: "complete" as const, reason: "Complete" },
      uncertainty: [],
    };
    const row = dayHtml(await page([complete]), "2026-10-02");
    expect(row).toContain("Provisional result");
    expect(row).toMatch(net("\\+26"));
  });

  it("labels saved-season days with the same statuses and plain reasons", async () => {
    const entry = {
      dayNumber: 1,
      period: "2026-05-01T05:00:00Z – 2026-05-02T05:00:00Z",
      startTrophies: 6000,
      endTrophies: 6026,
      attackGain: 310,
      defenseLoss: 284,
      netChange: 26,
      attacks: 8,
      defenses: 8,
      state: "Complete",
      coverage: "complete",
      hasAdjustment: false,
      adjustmentTotal: null,
      flags: [],
    };
    const html = await renderRoute(
      {
        requestedTag: TAG,
        player: PLAYER,
        error: null,
        refreshStatus: null,
        refreshError: null,
        noJsIdempotencyKey: "test-idempotency-key",
        lookup: { tag: TAG, state: "tracking" },
        lookupError: null,
        seasons: SEASONS,
        selectedSeason: SEASON,
        historical: {
          ...SUMMARY,
          dailyEntries: [
            entry,
            {
              ...entry,
              dayNumber: 2,
              state: "Partial",
              coverage: "partial",
              flags: ["missing_end_baseline", "attack_star_total_mismatch"],
            },
            { ...entry, dayNumber: 3, netChange: null, flags: [] },
            { ...entry, dayNumber: 4, flags: ["ranked_version_mismatch"] },
            {
              ...entry,
              dayNumber: 5,
              attackGain: 284,
              netChange: 0,
              defenses: 9,
              state: "Partial",
              coverage: "partial",
              flags: ["defense_count_exceeds_eight"],
            },
            // Prodigi's Day 24: no start-of-day checks, but all 8 of each.
            {
              ...entry,
              dayNumber: 24,
              attackGain: 300,
              defenseLoss: 311,
              netChange: -11,
              state: "Partial",
              coverage: "partial",
              flags: ["missing_start_battle_log_baseline", "missing_start_baseline"],
            },
            {
              ...entry,
              dayNumber: 25,
              state: "Partial",
              coverage: "partial",
              flags: ["missing_start_baseline", "perspective_disagreement"],
            },
            {
              ...entry,
              dayNumber: 26,
              attacks: 7,
              state: "Partial",
              coverage: "partial",
              flags: ["missing_start_baseline"],
            },
          ],
        },
        historicalError: null,
      },
      `?season=${SEASON}`,
    ).then((value) => value.replaceAll("<!-- -->", ""));
    const rows = html.split("<tbody>")[1].split("</tbody>")[0].split("</tr>");
    expect(rows[0]).toContain("<td>Provisional result</td>");
    expect(rows[1]).toContain("Incomplete");
    expect(rows[1]).toContain("Trophies at the end of this day were not recorded.");
    expect(rows[1]).toContain(
      "Recorded attacks do not match the day&#x27;s attack count.",
    );
    expect(rows[1]).not.toContain("missing_end_baseline");
    expect(rows[2]).toContain("Result unknown");
    expect(rows[2]).toContain("<td>Unknown</td><td>+26</td>");
    expect(rows[3]).toContain("Incomplete");
    expect(rows[3]).not.toContain("Provisional result");
    expect(rows[3]).toContain("The evidence for this day conflicts.");
    expect(rows[4]).toContain(
      "Clash of Clans returned 9 defenses for this day, more than the usual 8, so this day is marked partial.",
    );
    expect(rows[4]).toContain("<td>0</td><td>0</td>");
    expect(rows[5]).toContain("<td>Provisional result");
    expect(rows[5]).not.toContain("Incomplete");
    expect(rows[5]).toContain("<td>-11</td><td>-11</td>");
    expect(rows[5]).toContain("The battle log was not checked at the start of this day.");
    expect(rows[6]).toContain("Incomplete");
    expect(rows[6]).toContain(
      "The two players&#x27; battle logs disagree about a result.",
    );
    expect(rows[7]).toContain("Incomplete");
    expect(html).toContain("Attacks recorded");
    expect(html).toContain("Defenses recorded");
    expect(html).toContain("Recorded battle net");
  });

  it("shows the received warnings instead of a late-tracking explanation", async () => {
    const html = await page([ENDED_DAY], {
      dataQuality: [
        {
          code: "stale",
          label: "Stale saved profile",
          detail:
            "The collector has not confirmed this player profile within the current freshness limit.",
        },
        {
          code: "partial",
          label: "Incomplete ranked-day data",
          detail: "missing_end_battle_log_baseline; new_unknown_code",
        },
        {
          code: "uncertain",
          label: "Saved day state",
          detail: "ranked_day_state:Inconsistent; ranked_day_state:Malformed",
        },
      ],
    });
    expect(html).not.toContain("tracking started partway");
    expect(html).toContain(
      "The collector has not confirmed this player profile within the current freshness limit.",
    );
    expect(html).toContain(
      "Ending evidence arrives after Reset. Some daily evidence is unavailable.",
    );
    expect(html).not.toContain("new_unknown_code");
    expect(html).toContain(
      "The evidence for this day conflicts. Some saved evidence for this day could not be read.",
    );
    expect(html).not.toContain("ranked_day_state");
  });
});
