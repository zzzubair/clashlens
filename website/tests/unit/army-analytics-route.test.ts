import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  StaticRouterProvider,
} from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

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

describe("army analytics route historical reads", () => {
  beforeEach(() => {
    mocks.createPythonClient.mockReset();
  });

  it("serves a historical season from the whole-season summary without day or population filters", async () => {
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
    expect(query.get("lens")).toBe("defense");
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

  it("keeps day ranges and population filters for the current season", async () => {
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
    expect(query.get("start_day")).toBe("3");
    expect(query.get("end_day")).toBe("9");
    expect(query.get("population")).toBe("band-51-100");
    expect(data).toMatchObject({ error: null, seasonEmpty: null });
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
          lens: "defense",
          population: "top-50",
          category: "spells",
          sort: "usage-count",
        },
      },
      error: null,
    });
  });

  it.each([
    "lens=sideways",
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

  it("reconciles recorded, included and excluded captured defense records", async () => {
    const text = renderedText(
      await renderArmyRoute("recent=1&lens=defense&population=top-100"),
    );
    expect(text).toContain("Battle records 1,593 Recorded in this selection");
    expect(text).toContain("Records included 1,591");
    expect(text).toContain("Records excluded 2");
    expect(text).toContain("1 other battle record had no opponent and was excluded");
  });
});
