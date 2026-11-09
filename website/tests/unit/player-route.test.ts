import { UNKNOWN_END, VERIFIED, logFacts } from "../fixtures/player-route-data";
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  StaticRouterProvider,
} from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  NEWER_PROFILE,
  PLAYER,
  REFRESH_STATUS,
  SAVED_DAY,
  SEASON,
  SEASONS,
  SUMMARY,
  TAG,
} from "../fixtures/player-route-data";
vi.mock("../../app/services/players.server", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../app/services/players.server")>();
  return {
    ...actual,
    getPlayer: async (...args: Parameters<typeof actual.getPlayer>) =>
      mocks.createPythonClient().getPlayer(...args),
    getPlayerSeasons: async (...args: Parameters<typeof actual.getPlayerSeasons>) =>
      mocks.createPythonClient().getPlayerSeasons(...args),
    getPlayerSeason: async (...args: Parameters<typeof actual.getPlayerSeason>) =>
      mocks.createPythonClient().getPlayerSeason(...args),
    getRefreshStatus: async (...args: Parameters<typeof actual.getRefreshStatus>) =>
      mocks.createPythonClient().getRefreshStatus(...args),
  };
});

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
    useState: (initial: unknown) => {
      const timedOut = initial === "waiting" && mocks.lookupTimedOut;
      return actual.useState(timedOut ? "exhausted" : initial);
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
} from "../../app/lib/contracts";
import { playerLookupView } from "../../app/lib/player-lookup-text";
import { isRefreshStatusPayload } from "../../app/lib/validation";
import PlayerRoute, { loader as playerLoader } from "../../app/routes/player";
import { createClientAddressContext } from "../../app/server/client-address.server";
import { PythonApiError } from "../../app/services/python-response.server";

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

function renderSeason(historical: HistoricalSeasonSummary) {
  return renderRoute(
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
    expect(html).toContain(`<option value="${SEASON}"`);
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

  it("says only that a missing summary is unavailable and keeps a known final count", async () => {
    const official = await renderSeason({
      ...SUMMARY,
      source: "official_league_history",
      officialHistory: {
        observedAt: "2026-05-29T06:00:00+00:00",
        eodTrophies: 5800,
        finalPlacement: 12,
      },
    });
    expect(official).toContain("<dt>Final trophies</dt><dd>5,800</dd>");
    expect(official).not.toContain("not recorded");
    const late = await renderSeason({
      ...SUMMARY,
      startTrophies: null,
      endTrophies: 5800,
    });
    expect(late).toContain("<dt>Final trophies</dt><dd>5,800</dd>");
    const unfinished = await renderSeason({
      ...SUMMARY,
      startTrophies: 5000,
      endTrophies: null,
    });
    expect(unfinished).toContain(
      '<dt>Final trophies</dt><dd class="summary-words">Unknown</dd>',
    );
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
        start: `${start.toISOString()}`,
        end: `${end.toISOString()}`,
        seasonId: null,
        presentation: VERIFIED,
        displayEnd: UNKNOWN_END,
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
      player: {
        ...PLAYER,
        currentDay: days[27],
        recentDays: days,
        seasonDays: days,
        ...logFacts(
          days,
          days.map((day) => day.dayNumber!),
        ),
      },
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
    expect(html.match(/<details class="log-day/g)).toHaveLength(28);
    expect(html.match(/class="battle-row /g)).toHaveLength(448);
    expect(html.match(/>Show army</g)).toHaveLength(448);
    expect(html).not.toContain("army-portraits");
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
      presentation: {
        ...VERIFIED,
        status: "uncertain",
        reason_codes: ["attack_count_exceeds_eight"],
      },
      completeness: { state: "partial", reason: "attack_count_exceeds_eight" },
      uncertainty: ["attack_count_exceeds_eight"],
    };
    const html = await renderRoute(
      {
        requestedTag: TAG,
        player: {
          ...PLAYER,
          recentDays: [day],
          seasonDays: [day],
          ...logFacts([day], [day.dayNumber!]),
        },
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
    expect(html.match(/class="battle-row /g)).toHaveLength(9);
    expect(html).toContain("Opponent 9");
    expect(html).not.toContain("Empty attack slot 9");
    expect(html).toContain(
      "Clash of Clans returned 9 attacks for this day, more than the usual 8, so this day is uncertain.",
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
    ["not_found", "has no player with this tag. Check it for typos and search again."],
    ["not_in_legend", "This player is not in Legend I."],
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
      expect(html).toContain('<select name="season"');
      expect(html).not.toContain("Current trophies");
      expect(mocks.startPlayerLookup).not.toHaveBeenCalled();
      expect(html.includes("Search for the right tag")).toBe(state === "not_found");
      expect(html.includes("?retry=1")).toBe(state === "failed");
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

  it.each([
    ["Electro League 33", "not in Legend I. Current league: Electro League 33."],
    [undefined, "This player is not in Legend I."],
  ])("names a player outside Legend I in %s", async (league, note) => {
    const state = "not_in_legend";
    mocks.createPythonClient.mockReturnValue({
      getPlayer: vi.fn().mockResolvedValue({ ...PLAYER, trackingState: state }),
      getPlayerSeasons: vi.fn().mockResolvedValue(SEASONS),
      getPlayerSeason: vi.fn().mockResolvedValue(SUMMARY),
    });
    const profile = { name: "Sloothy", clan: null, trophies: 152, league };
    mocks.getPlayerLookup.mockResolvedValue({ tag: TAG, state, profile });
    const html = await renderRoute(
      await playerLoader({
        request: requestFor(null),
        params: { tag: TAG },
      } as never),
    );
    expect(html).toContain("<h1><bdi>Sloothy</bdi></h1>");
    expect(html).toContain(note);
    expect(html).not.toMatch(/Season summary|This Season|Current trophies|refresh-form/);
    expect(html).toContain(`<option value="${SEASON}"`);
  });

  it.each([null, SEASON])(
    "keeps history and hides the profile when lookup fails for season %s",
    async (season) => {
      mocks.createPythonClient.mockReturnValue({
        getPlayer: vi.fn().mockResolvedValue({
          ...PLAYER,
          trackingState: "uncertain",
          seasonDays: [SAVED_DAY],
          ...logFacts([SAVED_DAY], [1]),
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
      expect(html).toContain('<select name="season"');
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
        profile: {
          ...PLAYER.profile,
          trophies: 6400,
          seasonResetPending: pending,
        },
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
          ...logFacts([SAVED_DAY], [1]),
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
      getPlayer: vi.fn().mockResolvedValue({
        ...PLAYER,
        currentDay: SAVED_DAY,
        recentDays: [SAVED_DAY],
        ...logFacts([SAVED_DAY], [1]),
      }),
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

  it("shows an old profile's age and, on a tap, when it and its battle history were saved", async () => {
    const stale = {
      ...PLAYER,
      profile: {
        ...PLAYER.profile,
        freshness: {
          state: "stale",
          observedAt: "2026-09-07T12:00:00Z",
          ageSeconds: 7_300,
        },
        battleHistoryUpdatedAt: "2026-09-07T11:40:00Z",
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
    // One short age; the exact times open on a tap, an old one with its date.
    expect(await render(stale)).toContain(
      "Updated 2 hours agoProfile 7 Sep 2026, 12:00 UTCBattles 7 Sep 2026, 11:40 UTC",
    );
    const fresh = await render({
      ...PLAYER,
      profile: { ...PLAYER.profile, battleHistoryUpdatedAt: null },
    });
    expect(fresh).toContain("Updated just nowProfile 12:00 UTCBattles not saved yet");
    const oldHistory = await render({
      ...PLAYER,
      profile: {
        ...PLAYER.profile,
        battleHistoryUpdatedAt: "2026-09-07T11:40:00Z",
      },
    });
    expect(oldHistory).toContain(
      "Updated just nowProfile 12:00 UTCBattles 7 Sep 2026, 11:40 UTC",
    );
  });

  it("keeps a newly published battle when a completed Refresh has the same check time", async () => {
    mocks.createPythonClient.mockReturnValue({
      getPlayer: vi.fn().mockResolvedValue({
        ...PLAYER,
        currentDay: SAVED_DAY,
        recentDays: [SAVED_DAY],
        ...logFacts([SAVED_DAY], [1]),
      }),
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
        start: "2026-09-08T05:00:00Z",
        end: "2026-09-09T05:00:00Z",
        seasonId: null,
        presentation: VERIFIED,
        displayEnd: UNKNOWN_END,
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
          end: "2026-11-02T05:00:00Z",
          initialTrophies: 5000,
          anchorSource: "official_league_history",
          anchorObservedAt: "2026-09-08T13:00:00Z",
        },
        seasonDays: [confirmedDay],
        ...logFacts([confirmedDay, datedDay], [2, 1]),
        recentDays: [
          { ...confirmedDay, start: "2026-09-08T05:00:00+00:00" },
          ...(source === "current" ? [] : [datedDay]),
        ],
        currentDay:
          source === "recent"
            ? null
            : {
                ...datedDay,
                start: "2026-09-07T05:00:00+00:00",
                end: "2026-09-08T05:00:00+00:00",
                seasonId: null,
                presentation: VERIFIED,
                displayEnd: UNKNOWN_END,
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
      // The unconfirmed day is numbered from the current Season's start.
      expect(text).toContain("Day 1");
      expect(text).not.toContain("Date only");
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
    const payload = {
      ...REFRESH_STATUS,
      player: { ...PLAYER, trackingState },
    };
    expect(isRefreshStatusPayload(payload)).toBe(accepted);
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
    expect(html).not.toContain("This page updates as soon as they play.");
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
      getPlayer: vi.fn().mockResolvedValue({
        ...PLAYER,
        seasonDays: [SAVED_DAY],
        ...logFacts([SAVED_DAY], [1]),
      }),
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
    const view = playerLookupView(PLAYER, { tag: TAG, state: "not_in_legend" }, true);
    expect(view).toMatchObject({
      trackedPlayer: null,
      lookup: { state: "not_in_legend" },
      minuteChecks: false,
    });
  });

  it("lets a final lookup outrank an older tracked page on a fresh visit", () => {
    const view = playerLookupView(PLAYER, { tag: TAG, state: "not_in_legend" }, false);
    expect(view).toMatchObject({
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
