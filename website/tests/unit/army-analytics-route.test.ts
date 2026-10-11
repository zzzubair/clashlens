import { createServer, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  StaticRouterProvider,
} from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  createPythonClient: vi.fn(),
}));

vi.mock("../../app/services/python.server", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../app/services/python.server")>();
  return { ...actual, createPythonClient: mocks.createPythonClient };
});

import ArmyRoute, { loader as armyLoader } from "../../app/routes/army-analytics";
import { PythonApiError } from "../../app/services/python.server";

const SEASON = "1785714000";
const SEASON_END = (Number(SEASON) + 28 * 86400) * 1000;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"], now: SEASON_END - 10 * 86_400_000 });
});

afterEach(() => {
  vi.useRealTimers();
});

function requestFor(query: string) {
  return new Request(`https://clashlens.example/analytics/armies?${query}`);
}

async function renderArmyRoute(query: string) {
  const handler = createStaticHandler([
    { path: "/analytics/armies", Component: ArmyRoute, loader: armyLoader },
  ]);
  const context = await handler.query(requestFor(query));
  if (context instanceof Response) throw new Error("unexpected response");
  return renderToString(
    createElement(StaticRouterProvider, {
      router: createStaticRouter(handler.dataRoutes, context),
      context,
    }),
  );
}

function renderedText(html: string) {
  return html
    .replaceAll("<!-- -->", "")
    .replace(/<[^>]*>/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function currentAnalytics(
  population: string,
  coveredDays: number[],
  cohortPlayers: number,
  staleOrUncertainCohortMembers = 0,
  streakGapDays: number[] = [],
) {
  return {
    kind: "army-analytics",
    selection: {
      lens: "offense",
      season: SEASON,
      startDay: coveredDays[0],
      endDay: coveredDays[coveredDays.length - 1],
      population,
      category: "troops",
      sort: "usage-rate",
    },
    totalAttacks: 0,
    usableArmySample: 0,
    armyStates: {},
    armyStatesSumConfirmed: true,
    unknownAffectedAttacks: 0,
    unknownComponentOccurrences: 0,
    perspectiveDisagreementCount: 0,
    missingTrophyMembershipEvidence: 0,
    cohortEvidence: {
      cohortPlayers,
      staleOrUncertainCohortMembers,
      streakExcludedPlayers: 0,
      shieldedPlayerDays: 0,
    },
    collectionCoverage: {
      state: "complete",
      completedDays: coveredDays.length,
      coveredDays,
      streakGapDays,
    },
    freshness: { state: "frozen" },
    reproducibility: {
      officialSeasonId: SEASON,
      legendDays: [coveredDays[0], coveredDays[coveredDays.length - 1]],
      snapshotVersions: [],
    },
    versions: { decoder: "decoder", catalog: "catalog", analytics: "v3" },
    publicationIdentity: "publication",
    rows: [],
  };
}

describe("army analytics route historical reads", () => {
  beforeEach(() => {
    mocks.createPythonClient.mockReset();
  });

  it("serves a historical season's attacks from the whole-season summary without day or population filters", async () => {
    const getArmySeasonSummary = vi.fn().mockResolvedValue({ selection: {} });
    const getArmyAnalytics = vi.fn();
    mocks.createPythonClient.mockReturnValue({
      getArmySeasonSummary,
      getArmyAnalytics,
    });
    const data = await armyLoader({
      request: requestFor(
        `season=${SEASON}&lens=defense&start_day=3&end_day=9&population=top-100&category=troops&sort=usage-rate`,
      ),
      params: {},
    } as never);
    // Day ranges and population selections in the URL are not forwarded:
    // the historical form disables those controls.
    expect(getArmyAnalytics).not.toHaveBeenCalled();
    expect(getArmySeasonSummary).toHaveBeenCalledTimes(1);
    const [season, query] = getArmySeasonSummary.mock.calls[0] as [
      string,
      URLSearchParams,
    ];
    expect(season).toBe(SEASON);
    expect(query.has("lens")).toBe(false);
    expect(query.get("category")).toBe("troops");
    expect(query.get("sort")).toBe("usage-rate");
    expect(query.has("start_day")).toBe(false);
    expect(query.has("end_day")).toBe(false);
    expect(query.has("population")).toBe(false);
    expect(data).toMatchObject({
      error: null,
      seasonEmpty: null,
      historicalSummary: true,
    });
  });

  it("renders finalized quantity, usage and star counts", async () => {
    const getArmySeasonSummary = vi.fn().mockResolvedValue({
      kind: "army-analytics",
      selection: {
        season: SEASON,
        lens: "offense",
        startDay: 1,
        endDay: 28,
        population: "all",
        category: "troops",
        sort: "usage-rate",
      },
      totalAttacks: 10,
      usableArmySample: 10,
      armyStates: { fully_decoded: 10 },
      armyStatesSumConfirmed: true,
      unknownAffectedAttacks: 0,
      unknownComponentOccurrences: 0,
      perspectiveDisagreementCount: 0,
      missingTrophyMembershipEvidence: 0,
      cohortEvidence: {
        cohortPlayers: 0,
        staleOrUncertainCohortMembers: 0,
        streakExcludedPlayers: 0,
        shieldedPlayerDays: 0,
      },
      collectionCoverage: { state: "complete", completedDays: 28 },
      freshness: { state: "frozen" },
      reproducibility: {
        officialSeasonId: SEASON,
        legendDays: [1, 28],
        snapshotVersions: [],
      },
      versions: { decoder: "decoder", catalog: "catalog", analytics: "v3" },
      publicationIdentity: "publication",
      pagination: { offset: 0, totalRows: 1, nextOffset: null },
      rows: [
        {
          key: "troop:58@5",
          label: "Ice Golem",
          quantity: 5,
          usageCount: 5,
          usageDenominator: 10,
          usageRate: 0.5,
          oneStarCount: 1,
          twoStarCount: 1,
          threeStarCount: 2,
        },
      ],
    });
    mocks.createPythonClient.mockReturnValue({
      getArmySeasonSummary,
      getArmyAnalytics: vi.fn(),
    });
    const text = renderedText(await renderArmyRoute(`season=${SEASON}`));
    expect(text).toContain("Quantity");
    expect(text).toContain("1-star");
    expect(text).toContain("2-star");
    expect(text).toContain("3-star");
    expect(text).toContain("Ice Golem");
    expect(text).toContain("5 / 10");
    expect(text).toContain("50.0%");
    expect(text).toContain("1 battle");
    expect(text).toContain("2 battles");
  });

  it("reports unavailable when historical detail exists but its summary is missing", async () => {
    const getArmySeasonSummary = vi
      .fn()
      .mockRejectedValue(
        new PythonApiError(404, { error: "army_analytics_unavailable" }),
      );
    const getArmyAnalytics = vi.fn().mockResolvedValue({ selection: {} });
    mocks.createPythonClient.mockReturnValue({
      getArmySeasonSummary,
      getArmyAnalytics,
    });
    const data = await armyLoader({
      request: requestFor(
        `season=${SEASON}&lens=defense&start_day=3&end_day=9&population=band-51-100&category=heroes&sort=usage-count`,
      ),
      params: {},
    } as never);
    expect(getArmySeasonSummary).toHaveBeenCalledTimes(1);
    expect(getArmyAnalytics).not.toHaveBeenCalled();
    expect(data).toMatchObject({
      init: { status: 404 },
      data: {
        analytics: null,
        historicalSummary: true,
        error: { error: { code: "unavailable" } },
      },
    });
  });

  it("does not fall back on validation or other errors", async () => {
    for (const summaryError of [
      new PythonApiError(422, { error: "invalid_army_analytics_selection" }),
      new PythonApiError(404, { error: "missing" }),
      new Error("boom"),
    ]) {
      const getArmySeasonSummary = vi.fn().mockRejectedValue(summaryError);
      const getArmyAnalytics = vi.fn();
      mocks.createPythonClient.mockReturnValue({
        getArmySeasonSummary,
        getArmyAnalytics,
      });
      await armyLoader({
        request: requestFor(`season=${SEASON}&lens=defense`),
        params: {},
      } as never);
      expect(getArmyAnalytics).not.toHaveBeenCalled();
    }
  });

  it("asks for the current season's attacks over every finished day, keeping the player group", async () => {
    const getArmySeasonSummary = vi.fn();
    const getArmyAnalytics = vi.fn().mockResolvedValue({ selection: {} });
    mocks.createPythonClient.mockReturnValue({
      getArmySeasonSummary,
      getArmyAnalytics,
    });
    const data = await armyLoader({
      request: requestFor(
        "season=current&lens=defense&start_day=3&end_day=9&population=band-51-100&category=troops&sort=usage-rate",
      ),
      params: {},
    } as never);
    expect(getArmySeasonSummary).not.toHaveBeenCalled();
    expect(getArmyAnalytics).toHaveBeenCalledTimes(1);
    const [query] = getArmyAnalytics.mock.calls[0] as [URLSearchParams];
    // Old links may still carry a lens or day range; the API's defaults
    // (attacks, Day 1 to the latest finished day) apply instead.
    expect(query.has("lens")).toBe(false);
    expect(query.has("start_day")).toBe(false);
    expect(query.has("end_day")).toBe(false);
    expect(query.get("population")).toBe("band-51-100");
    expect(data).toMatchObject({ error: null, seasonEmpty: null });
  });

  it("opens on armies, attacks only, with a picker for unit usage", async () => {
    const getArmyAnalytics = vi.fn().mockResolvedValue({
      ...currentAnalytics("top-100", [1, 2], 0),
      selection: {
        ...currentAnalytics("top-100", [1, 2], 0).selection,
        category: "army-types",
      },
      totalAttacks: 12,
      usableArmySample: 10,
      perspectiveDisagreementCount: 0,
      rows: [
        {
          key: "army-type:3",
          label: "Thrower",
          usageCount: 6,
          usageDenominator: 12,
          usageRate: 0.5,
          starCounts: [0, 1, 2, 3],
          starRates: [0, 1 / 6, 2 / 6, 3 / 6],
          threeStarRate: 0.5,
          averageStars: 2.33,
          averageDestruction: 91,
          unknownExcludedAttacks: 0,
        },
        {
          key: "army-type:2",
          label: "Unreadable",
          usageCount: 2,
          usageDenominator: 12,
          usageRate: 2 / 12,
          starCounts: [1, 1, 0, 0],
          starRates: [0.5, 0.5, 0, 0],
          threeStarRate: 0,
          averageStars: 0.5,
          averageDestruction: 40,
          unknownExcludedAttacks: 0,
        },
      ],
    });
    mocks.createPythonClient.mockReturnValue({ getArmyAnalytics });
    const html = await renderArmyRoute("season=current&lens=defense");
    const [query] = getArmyAnalytics.mock.calls[0] as [URLSearchParams];
    expect(query.get("category")).toBe("army-types");
    expect(query.has("lens")).toBe(false);
    const text = renderedText(html);
    // Attacks only: the Armies view offers no Attacks/Defenses choice.
    expect(html).not.toContain('name="lens"');
    expect(text).toContain("Armies Unit usage");
    expect(html).toMatch(/aria-current="page"[^>]*>Armies</);
    expect(text).toContain("Thrower 6 / 12 50.0%");
    expect(text).toContain("not which army is best");
    // Unreadable attacks are a row of their own, not excluded.
    expect(text).toContain("Records included 12 Every attack, Unreadable included");
    expect(text).toContain("Records excluded 0");
    expect(text).toContain(
      "2 attacks had missing or unreadable army details and count as Unreadable.",
    );
    expect(text).not.toContain("conflicting reports");
    expect(text).not.toContain("excluded from every row");
    expect(text).not.toContain("Additional exclusions");
    expect(html).toContain(
      'href="/analytics/armies?season=current&amp;lens=defense&amp;category=troops"',
    );
  });

  it("opens a past Season chosen from the Armies view on its unit usage", async () => {
    const getArmySeasonSummary = vi.fn().mockResolvedValue({ selection: {} });
    mocks.createPythonClient.mockReturnValue({ getArmySeasonSummary });
    const result = await armyLoader({
      request: requestFor(`season=${SEASON}&category=army-types&lens=offense`),
      params: {},
    } as never);
    const [season, query] = getArmySeasonSummary.mock.calls[0] as [
      string,
      URLSearchParams,
    ];
    expect(season).toBe(SEASON);
    expect(query.get("category")).toBe("troops");
    expect(result).toMatchObject({ historicalSummary: true, error: null });
  });

  it("opens a visit with no query on armies, not the captured unit preview", async () => {
    const getArmyAnalytics = vi
      .fn()
      .mockResolvedValue(currentAnalytics("top-100", [1], 0));
    mocks.createPythonClient.mockReturnValue({ getArmyAnalytics });
    const result = await armyLoader({ request: requestFor(""), params: {} } as never);
    const [query] = getArmyAnalytics.mock.calls[0] as [URLSearchParams];
    expect(query.get("category")).toBe("army-types");
    expect(result).toMatchObject({ requestedSeason: "current", error: null });

    const units = await armyLoader({
      request: requestFor("category=troops"),
      params: {},
    } as never);
    expect(getArmyAnalytics).toHaveBeenCalledTimes(1);
    expect(units).toMatchObject({
      analytics: { selection: { category: "troops" } },
      error: null,
    });
  });

  it("names the finished days covered when current-season days are missing", async () => {
    const untracked = Array.from({ length: 22 }, (_, index) => index + 1).concat(24);
    mocks.createPythonClient.mockReturnValue({
      getArmyAnalytics: vi
        .fn()
        .mockResolvedValueOnce(
          currentAnalytics("top-100", [23, 25, 26], 100, 0, untracked),
        )
        .mockResolvedValueOnce(currentAnalytics("top-100", [1, 2, 3], 100)),
    });
    const html = await renderArmyRoute("season=current");
    expect(renderedText(html)).toContain(
      "Days 23, 25–26 of 28; days not tracked: 1–22, 24.",
    );
    // There are no day fields to change; the page always reads the Season.
    expect(html).not.toContain('name="start_day"');
    expect(html).not.toContain('name="end_day"');
    // Consistent top needs every ended day, so a gap withholds it.
    expect(html).toContain('value="top-100"');
    expect(html).not.toContain("streak-top-");
    expect(renderedText(html)).not.toContain("choose days tracked throughout");
    const complete = await renderArmyRoute("season=current");
    expect(renderedText(complete)).not.toContain("days not tracked");
    expect(complete).toContain('value="streak-top-100"');
  });

  it("explains how Consistent top is decided, including an empty group", async () => {
    const getArmyAnalytics = vi
      .fn()
      .mockResolvedValueOnce(currentAnalytics("streak-top-100", [25, 26], 66, 66))
      .mockResolvedValueOnce(currentAnalytics("streak-top-5", [25, 26], 0));
    mocks.createPythonClient.mockReturnValue({ getArmyAnalytics });
    const filled = renderedText(
      await renderArmyRoute("season=current&population=streak-top-100"),
    );
    expect(filled).toContain("players in the top 100 on every Legend day so far.");
    expect(filled).toContain(
      "66 players were in the top 100 on every Legend day so far. Top 100 on a day means the top 100 of the leaderboard saved just before that day’s Reset (05:00 UTC), ranked by each player’s total at that Reset, before the game’s automatic defense loss: their last value saved before the Reset plus the battles recorded after it. Boards published before we started adding those battles, and not rebuilt since, use the saved value alone, as do players whose later battles we can’t confirm. 66 of these players had a trophy count over 10 minutes old at Reset on at least one day. Comparison with settled end-of-day ranks: not available yet.",
    );
    const empty = renderedText(
      await renderArmyRoute("season=current&population=streak-top-5"),
    );
    expect(empty).toContain("No player was in the top 5 on every Legend day so far.");
    expect(empty).not.toContain("of these players");
    expect(empty).toContain(
      "Comparison with settled end-of-day ranks: not available yet.",
    );
  });

  it("warns about old saved ranks for top and rank-range groups only", async () => {
    const getArmyAnalytics = vi
      .fn()
      .mockResolvedValueOnce(currentAnalytics("top-100", [25, 26], 100, 100))
      .mockResolvedValueOnce(currentAnalytics("band-101-200", [25, 26], 100, 7))
      .mockResolvedValueOnce(currentAnalytics("top-100", [25, 26], 100, 0))
      .mockResolvedValueOnce(currentAnalytics("all", [25, 26], 0, 0));
    mocks.createPythonClient.mockReturnValue({ getArmyAnalytics });
    const render = async (population: string) =>
      renderedText(await renderArmyRoute(`season=current&population=${population}`));
    expect(await render("top-100")).toContain(
      "Ranks come from the leaderboard saved just before the latest included day’s Reset (05:00 UTC). 100 of these 100 players had a trophy count over 10 minutes old at that Reset, so their rank may be out of date.",
    );
    expect(await render("band-101-200")).toContain(
      "7 of these 100 players had a trophy count over 10 minutes old at that Reset",
    );
    expect(await render("top-100")).not.toContain("Ranks come from");
    expect(await render("all")).not.toContain("Ranks come from");
  });

  it("keeps a Consistent top choice when its days are unavailable", async () => {
    mocks.createPythonClient.mockReturnValue({
      getArmyAnalytics: vi.fn().mockRejectedValue(
        new PythonApiError(404, {
          error: "army_analytics_unavailable",
          affected_days: [24],
        }),
      ),
    });
    const html = await renderArmyRoute("season=current&population=streak-top-50");
    expect(renderedText(html)).toContain("No army stats for this season yet");
    expect(html).toMatch(/<option value="streak-top-50" selected="">/);
    expect(html).not.toContain("Selected player group");
  });

  it("reads individual Clan Castle troops when the toggle is on", async () => {
    const getArmyAnalytics = vi.fn().mockResolvedValue({ selection: {} });
    mocks.createPythonClient.mockReturnValue({ getArmyAnalytics });

    await armyLoader({
      request: requestFor("season=current&category=troops&cc=1"),
      params: {},
    } as never);
    expect(getArmyAnalytics.mock.calls[0][0].get("category")).toBe("cc-troops");

    await armyLoader({
      request: requestFor("season=current&category=troops"),
      params: {},
    } as never);
    expect(getArmyAnalytics.mock.calls[1][0].get("category")).toBe("troops");
  });

  it("does not show regular troop results for an unavailable past-season Clan Castle selection", async () => {
    const getArmySeasonSummary = vi
      .fn()
      .mockRejectedValue(
        new PythonApiError(404, { error: "army_analytics_unavailable" }),
      );
    const getArmyAnalytics = vi.fn();
    mocks.createPythonClient.mockReturnValue({
      getArmySeasonSummary,
      getArmyAnalytics,
    });

    const result = await armyLoader({
      request: requestFor(`season=${SEASON}&category=troops&cc=1`),
      params: {},
    } as never);
    expect(getArmySeasonSummary.mock.calls[0][1].get("category")).toBe("cc-troops");
    expect(getArmyAnalytics).not.toHaveBeenCalled();
    expect(result).toMatchObject({
      init: { status: 404 },
      data: { analytics: null, error: { error: { code: "unavailable" } } },
    });
  });

  it("uses captured-preview defaults only when filters are absent", async () => {
    const defaults = await armyLoader({
      request: requestFor("recent=1"),
      params: {},
    } as never);
    expect(defaults).toMatchObject({
      analytics: {
        selection: {
          lens: "offense",
          population: "top-100",
          category: "troops",
          sort: "usage-rate",
        },
      },
      error: null,
    });

    const selected = await armyLoader({
      request: requestFor(
        "recent=1&lens=defense&population=top-50&category=spells&sort=usage-count",
      ),
      params: {},
    } as never);
    expect(selected).toMatchObject({
      analytics: {
        selection: {
          lens: "offense",
          population: "top-50",
          category: "spells",
          sort: "usage-count",
        },
      },
      error: null,
    });

    const clanCastle = await armyLoader({
      request: requestFor("recent=1&cc=1"),
      params: {},
    } as never);
    expect(clanCastle).toMatchObject({
      analytics: { selection: { category: "cc-troops" } },
      error: null,
    });
  });

  it.each([
    "population=top-99",
    "category=unknown",
    "sort=alphabetical",
    "sample=0",
    "sample=no",
    "sample=",
  ])("rejects an invalid captured-preview filter: %s", async (filter) => {
    const result = await armyLoader({
      request: requestFor(`recent=1&${filter}`),
      params: {},
    } as never);
    expect(result).toMatchObject({
      init: { status: 422 },
      data: {
        analytics: null,
        error: { error: { code: "invalid_input" } },
      },
    });
  });

  it("redirects the original sample link to the captured preview", async () => {
    const result = await armyLoader({
      request: requestFor(
        "sample=1&season=current&start_day=1&end_day=7&category=spells",
      ),
      params: {},
    } as never);
    expect(result).toBeInstanceOf(Response);
    expect((result as Response).status).toBe(302);
    expect((result as Response).headers.get("location")).toBe(
      "?category=spells&recent=1",
    );
  });

  it("reconciles recorded, included and excluded captured attack records", async () => {
    const text = renderedText(await renderArmyRoute("recent=1&population=top-100"));
    expect(text).toContain("Battle records 1,600 Recorded in this selection");
    expect(text).toContain("Records included 1,600");
    expect(text).toContain("Records excluded 0");
  });
});

describe("army analytics current season end", () => {
  beforeEach(() => {
    mocks.createPythonClient.mockReset();
  });

  it("shows a current-season result before its 28 days end", async () => {
    vi.setSystemTime(SEASON_END - 1);
    mocks.createPythonClient.mockReturnValue({
      getArmyAnalytics: vi.fn().mockResolvedValue(currentAnalytics("top-100", [27], 100)),
    });
    const html = await renderArmyRoute("season=current");
    expect(html).toContain('aria-label="Army statistics"');
    expect(renderedText(html)).not.toContain("A new season is underway");
  });

  it("shows the new season's waiting state for the same result once its season ends", async () => {
    vi.setSystemTime(SEASON_END);
    mocks.createPythonClient.mockReturnValue({
      getArmyAnalytics: vi.fn().mockResolvedValue(currentAnalytics("top-100", [27], 100)),
      getArmySeasons: vi.fn().mockResolvedValue([SEASON]),
    });
    const html = await renderArmyRoute("season=current");
    expect(html).not.toContain('aria-label="Army statistics"');
    expect(renderedText(html)).toContain("A new season is underway");
    expect(html).toContain(`href="/analytics/armies?season=${SEASON}&amp;`);
  });

  it("does not show a reply that arrives after its season ended", async () => {
    vi.setSystemTime(SEASON_END - 1000);
    mocks.createPythonClient.mockReturnValue({
      getArmyAnalytics: vi.fn().mockImplementation(async () => {
        vi.setSystemTime(SEASON_END + 1000);
        return currentAnalytics("top-100", [27], 100);
      }),
    });
    const html = await renderArmyRoute("season=current");
    expect(html).not.toContain('aria-label="Army statistics"');
    expect(renderedText(html)).toContain("A new season is underway");
  });
});

describe("army analytics player groups", () => {
  beforeEach(() => {
    mocks.createPythonClient.mockReset();
  });

  it("offers the larger tops, rank ranges and a custom trophy range", async () => {
    mocks.createPythonClient.mockReturnValue({
      getArmyAnalytics: vi.fn().mockResolvedValue(currentAnalytics("top-10000", [23], 0)),
    });
    const html = await renderArmyRoute("season=current&population=top-10000");
    for (const population of [
      "top-1000",
      "top-2000",
      "top-5000",
      "top-10000",
      "band-1-100",
      "band-901-1000",
      "band-1001-2000",
      "band-2001-5000",
      "band-5001-10000",
      "trophies",
    ]) {
      expect(html).toContain(`value="${population}"`);
    }
    expect(html).not.toContain("Selected player group");
    expect(html).not.toContain('name="trophy_min"');
    // An empty group still shows the page, with zero records and no rows.
    const text = renderedText(html);
    expect(text).toContain("by top-10,000 players");
    expect(text).toContain("Battle records 0 Recorded in this selection");
    expect(text).toContain("No recognized components in this selection.");
  });

  it("puts trophy filtering first without changing the default player group", async () => {
    const getArmyAnalytics = vi
      .fn()
      .mockResolvedValue(currentAnalytics("top-100", [25, 26], 100));
    mocks.createPythonClient.mockReturnValue({ getArmyAnalytics });
    const html = await renderArmyRoute("saved=1&season=current");
    const picker = html.match(/<select name="population"[^>]*>(.*?)<\/select>/)?.[1];
    expect(picker?.match(/<option value="([^"]+)"/)?.[1]).toBe("trophies");
    expect(picker).toContain('<option value="top-100" selected="">');
    const [query] = getArmyAnalytics.mock.calls[0] as [URLSearchParams];
    expect(query.get("population")).toBe("top-100");
  });

  it("keeps trophy help relevant despite missing rank days", async () => {
    mocks.createPythonClient.mockReturnValue({
      getArmyAnalytics: vi
        .fn()
        .mockResolvedValue(currentAnalytics("trophies-5500-5600", [25, 26], 0, 0, [24])),
    });
    const text = renderedText(
      await renderArmyRoute("saved=1&season=current&population=trophies-5500-5600"),
    );
    expect(text).toContain(
      "Each battle counts if the player’s trophies at that moment fall in this range.",
    );
    expect(text).toContain("by players with 5,500 to 5,600 trophies at battle time");
    expect(text).not.toContain("This range has not been applied");
  });

  it.each([
    "top-2000",
    "top-5000",
    "top-10000",
    "band-1-100",
    "band-1001-2000",
    "band-5001-10000",
    "trophies-3800-4999",
  ])("asks the API for %s", async (population) => {
    const getArmyAnalytics = vi.fn().mockResolvedValue({ selection: {} });
    mocks.createPythonClient.mockReturnValue({ getArmyAnalytics });
    await armyLoader({
      request: requestFor(`season=current&population=${population}`),
      params: {},
    } as never);
    const [query] = getArmyAnalytics.mock.calls[0] as [URLSearchParams];
    expect(query.get("population")).toBe(population);
  });

  it("turns the custom trophy fields into one shareable range", async () => {
    const result = await armyLoader({
      request: requestFor(
        "season=current&population=trophies&trophy_min=3800&trophy_max=05200&lens=defense",
      ),
      params: {},
    } as never);
    expect((result as Response).status).toBe(302);
    expect((result as Response).headers.get("location")).toBe(
      "/analytics/armies?season=current&population=trophies-3800-5200&lens=defense",
    );
  });

  it("shows a chosen trophy range in its fields", async () => {
    mocks.createPythonClient.mockReturnValue({
      getArmyAnalytics: vi
        .fn()
        .mockResolvedValue(currentAnalytics("trophies-3800-5200", [23], 0)),
    });
    const html = await renderArmyRoute("season=current&population=trophies-3800-5200");
    expect(html).toMatch(/<option value="trophies" selected="">/);
    expect(html).toMatch(/name="trophy_min"[^>]*value="3800"/);
    expect(html).toMatch(/name="trophy_max"[^>]*value="5200"/);
    expect(renderedText(html)).toContain(
      "by players with 3,800 to 5,200 trophies at battle time",
    );
  });

  it("keeps the picker and explains a request that hits the 5-second limit", async () => {
    const actual = await vi.importActual<
      typeof import("../../app/services/python.server")
    >("../../app/services/python.server");
    let reply: (response: ServerResponse) => void = () => {};
    const server = createServer((_request, response) => reply(response));
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const saved = {
      url: process.env.CLASHLENS_PYTHON_API_URL,
      secret: process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64,
    };
    process.env.CLASHLENS_PYTHON_API_URL = `http://127.0.0.1:${(server.address() as AddressInfo).port}/`;
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 =
      "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8";
    mocks.createPythonClient.mockImplementation(actual.createPythonClient);
    const timeout = AbortSignal.timeout.bind(AbortSignal);
    const limit = vi.spyOn(AbortSignal, "timeout").mockImplementation(() => timeout(100));
    const tooWide =
      "This trophy range is too wide to load right now. Try a narrower range.";
    const json = { "content-type": "application/json" };
    try {
      for (const [when, send, message] of [
        ["waiting for the reply", () => {}, tooWide],
        [
          "reading the reply",
          (response: ServerResponse) => {
            response.writeHead(200, json);
            response.write('{"kind":');
          },
          tooWide,
        ],
        [
          "the database cancels",
          (response: ServerResponse) =>
            response.writeHead(503, json).end('{"error":"timeout"}'),
          tooWide,
        ],
        [
          "the service fails another way",
          (response: ServerResponse) =>
            response.writeHead(503, json).end('{"error":"service_unavailable"}'),
          "the live service is unavailable",
        ],
      ] as const) {
        reply = send;
        const html = await renderArmyRoute(
          "season=current&population=trophies-3800-6100",
        );
        const text = renderedText(html);
        expect(text, when).toContain(message);
        expect(text, when).not.toContain("malformed");
        expect(html).not.toContain("<table");
        expect(html).toMatch(/<option value="trophies" selected="">/);
        expect(html).toContain('value="top-10000"');
        expect(html).toMatch(/name="trophy_min"[^>]*value="3800"/);
        expect(html).toMatch(/name="trophy_max"[^>]*value="6100"/);
      }
      expect(limit).toHaveBeenCalledWith(5_000);
      reply = () => {};
      const top = renderedText(
        await renderArmyRoute("season=current&population=top-10000"),
      );
      expect(top).toContain("This player group took too long to load right now.");
      expect(top).not.toContain("trophy range is too wide");
    } finally {
      limit.mockRestore();
      server.closeAllConnections();
      server.close();
      for (const [name, value] of [
        ["CLASHLENS_PYTHON_API_URL", saved.url],
        ["CLASHLENS_PYTHON_HMAC_SECRET_B64", saved.secret],
      ] as const) {
        if (value === undefined) delete process.env[name];
        else process.env[name] = value;
      }
    }
  });

  it.each([
    ["population=trophies&trophy_min=6000&trophy_max=5000", "can’t be above"],
    ["population=trophies-6000-5000", "can’t be above"],
    ["population=trophies&trophy_min=&trophy_max=5000", "whole numbers"],
    ["population=trophies&trophy_min=-1&trophy_max=5000", "whole numbers"],
    ["population=trophies&trophy_min=12.5&trophy_max=5000", "whole numbers"],
    ["population=trophies-5000-100000", "whole numbers"],
    ["population=trophies-abc", "whole numbers"],
  ])("rejects the trophy range %s before asking the API", async (query, message) => {
    const getArmyAnalytics = vi.fn();
    mocks.createPythonClient.mockReturnValue({ getArmyAnalytics });
    const result = await armyLoader({
      request: requestFor(`season=current&${query}`),
      params: {},
    } as never);
    expect(getArmyAnalytics).not.toHaveBeenCalled();
    expect(result).toMatchObject({
      init: { status: 422 },
      data: { analytics: null, error: { error: { code: "invalid_input" } } },
    });
    const error = (result as { data: { error: { error: { message: string } } } }).data
      .error.error.message;
    expect(error).toContain(message);
  });
});

describe("army analytics attacks and season picker", () => {
  beforeEach(() => {
    mocks.createPythonClient.mockReset();
  });

  it("shows attacks only, with no perspective or day-range controls", async () => {
    mocks.createPythonClient.mockReturnValue({
      getArmyAnalytics: vi.fn().mockResolvedValue(currentAnalytics("top-100", [1, 2], 0)),
    });
    const html = await renderArmyRoute("season=current&lens=defense");
    const text = renderedText(html);
    expect(html).not.toContain('name="lens"');
    expect(text).not.toContain("Defenses");
    expect(text).not.toContain("From Legend day");
    expect(text).toContain("usage in attacks by top-100 players");
    expect(text).toContain("Days 1–2 · Attacks");
    expect(text).toContain(
      "Attacks from every completed Legend day this season, from Day 1.",
    );
  });

  it("names the hero equipment views as the owner asked", async () => {
    mocks.createPythonClient.mockReturnValue({
      getArmyAnalytics: vi.fn().mockResolvedValue(currentAnalytics("top-100", [1], 0)),
    });
    const text = renderedText(await renderArmyRoute("season=current"));
    expect(text).toContain("Hero equipment combos");
    expect(text).toContain("Hero and both equipments");
    expect(text).not.toContain("Equipment by hero");
    expect(text).not.toContain("Hero and equipment");
  });

  it("lists only seasons that have army data", async () => {
    const getArmySeasons = vi.fn().mockResolvedValue([]);
    mocks.createPythonClient.mockReturnValue({
      getArmyAnalytics: vi.fn().mockResolvedValue(currentAnalytics("top-100", [1], 0)),
      getArmySeasons,
    });
    const picker = (html: string) =>
      html.match(/<select name="season"[^>]*>(.*?)<\/select>/)?.[1] ?? "";
    // The first season: nothing to pick but the current one.
    const first = picker(await renderArmyRoute("season=current"));
    expect(first.match(/<option/g)).toHaveLength(1);
    expect(first).toContain(">Current season</option>");

    // A later season lists each finished season the API reports.
    getArmySeasons.mockResolvedValue([SEASON]);
    const later = picker(await renderArmyRoute("season=current"));
    expect(later.match(/<option/g)).toHaveLength(2);
    expect(later).toContain(`value="${SEASON}"`);

    // A failed list still leaves the current season to pick.
    getArmySeasons.mockRejectedValue(new Error("boom"));
    const failed = await renderArmyRoute("season=current");
    expect(failed).toContain('aria-label="Army statistics"');
    expect(picker(failed).match(/<option/g)).toHaveLength(1);
  });
});

describe("army analytics worst-case data", () => {
  // Long Clan Castle mixes, unknown catalog IDs, six-figure counts and counts of 1.
  const worstRows = [
    {
      key: "cc:troop:58,troop:1,troop:6,troop:57,troop:59",
      label: "Super Wall Breaker + Wall Breaker + Balloon + Rocket Balloon + Ice Golem",
      usageCount: 128_473,
      usageDenominator: 131_020,
      usageRate: 128_473 / 131_020,
      starCounts: [1_284, 12_345, 23_456, 91_388],
      starRates: [0.01, 0.096, 0.183, 0.711],
      threeStarRate: 0.711,
      averageStars: 2.6,
      averageDestruction: 87.6,
      unknownExcludedAttacks: 12_847,
    },
    {
      key: "troop:4000123",
      label: "Unknown ID 4000123",
      usageCount: 1,
      usageDenominator: 131_020,
      usageRate: 1 / 131_020,
      starCounts: [0, 0, 0, 1],
      starRates: [0, 0, 0, 1],
      threeStarRate: 1,
      averageStars: 3,
      averageDestruction: 100,
      unknownExcludedAttacks: 1,
    },
  ];

  beforeEach(() => {
    mocks.createPythonClient.mockReset();
  });

  it("formats large counts and says 1 correctly", async () => {
    mocks.createPythonClient.mockReturnValue({
      getArmyAnalytics: vi.fn().mockResolvedValue({
        ...currentAnalytics("top-100", [1], 1, 1),
        totalAttacks: 2,
        usableArmySample: 1,
        armyStates: { partial: 1 },
        perspectiveDisagreementCount: 1,
        rows: worstRows,
      }),
    });
    const text = renderedText(await renderArmyRoute("season=current&population=top-100"));
    expect(text).toContain(
      "This player had a trophy count over 10 minutes old at that Reset",
    );
    expect(text).toContain("1 of the included armies is partly readable.");
    expect(text).toContain(
      "1 completed Legend day. 1 battle has conflicting reports. 1 battle record had missing or unreadable army details and is excluded from every row.",
    );
    expect(text).toContain("128,473 / 131,020");
    expect(text).toContain("91,388 (71.1%)");
    expect(text).toContain("12,847");
    expect(text).toContain("Unknown ID 4000123");
  });

  it("says an empty breakdown has no components instead of showing a bare header", async () => {
    mocks.createPythonClient.mockReturnValue({
      getArmyAnalytics: vi.fn().mockResolvedValue(currentAnalytics("top-100", [1], 0)),
    });
    const html = await renderArmyRoute("season=current");
    expect(html.match(/No recognized components in this selection\./g)).toHaveLength(2);
    expect(html).toMatch(/aria-label="Star breakdown table" hidden=""/);
  });

  it("formats past-season quantities and result totals", async () => {
    const pastRows = worstRows.map(({ key, label, usageCount, usageDenominator }) => ({
      key,
      label,
      usageCount,
      usageDenominator,
      usageRate: usageCount / usageDenominator,
      quantity: usageCount * 3,
      oneStarCount: 1,
      twoStarCount: 1,
      threeStarCount: 1,
    }));
    mocks.createPythonClient.mockReturnValue({
      getArmySeasonSummary: vi.fn().mockResolvedValue({
        ...currentAnalytics("all", [1, 28], 0),
        pagination: { offset: 0, totalRows: 1_284, nextOffset: 50 },
        rows: pastRows,
      }),
      getArmyAnalytics: vi.fn(),
    });
    const text = renderedText(await renderArmyRoute(`season=${SEASON}`));
    expect(text).toContain("385,419");
    expect(text).toContain("Showing 2 of 1,284 results");
  });
});
