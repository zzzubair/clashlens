import { createServer, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createMemoryRouter,
  createStaticHandler,
  createStaticRouter,
  RouterProvider,
  StaticRouterProvider,
} from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  createPythonClient: vi.fn(),
  openPage: null as { states: unknown[]; next: number } | null,
}));

// A page left open keeps its state from one in-page visit to the next, as
// React does in the browser; server renders otherwise start fresh.
vi.mock("react", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react")>();
  return {
    ...actual,
    useState: (initialState: unknown) => {
      const [value, setValue] = actual.useState(initialState);
      const page = mocks.openPage;
      if (!page) return [value, setValue];
      const index = page.next++;
      if (index === page.states.length) page.states.push(value);
      return [
        page.states[index],
        (next: unknown) => {
          page.states[index] = next;
          setValue(next);
        },
      ];
    },
  };
});

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
    const html = await renderArmyRoute(`season=${SEASON}&category=troops`);
    const text = renderedText(html);
    // The view picker stays on a past Season.
    expect(html).toMatch(/aria-current="page"[^>]*><strong>Unit usage</);
    expect(html).toContain(
      `href="/analytics/armies?season=${SEASON}&amp;category=army-types"`,
    );
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

  it("says army types are being prepared while the saved armies are sorted", async () => {
    mocks.createPythonClient.mockReturnValue({
      getArmyAnalytics: vi
        .fn()
        .mockRejectedValue(new PythonApiError(404, { error: "army_types_preparing" })),
    });
    const html = await renderArmyRoute("");
    const text = renderedText(html);
    expect(text).toContain("Army types are being prepared");
    expect(text).toContain("Back in a few minutes; Unit usage works now.");
    expect(text).not.toContain("No army stats for this season yet");
    expect(html).toContain('href="/analytics/armies?category=troops"');
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

  it("asks for the current season's attacks over every finished day, keeping a top group", async () => {
    const getArmySeasonSummary = vi.fn();
    const getArmyAnalytics = vi.fn().mockResolvedValue({ selection: {} });
    mocks.createPythonClient.mockReturnValue({
      getArmySeasonSummary,
      getArmyAnalytics,
    });
    const data = await armyLoader({
      request: requestFor(
        "season=current&lens=defense&start_day=3&end_day=9&population=top-300&category=troops&sort=usage-rate",
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
    expect(query.get("population")).toBe("top-300");
    expect(data).toMatchObject({ error: null, seasonEmpty: null });

    // Only leaderboard tops are offered; a visit without one, or an old link
    // to any other group, shows the Top 1,000.
    for (const old of [
      "",
      "band-51-100",
      "streak-top-100",
      "trophies-3800-4999",
      "top-3000",
    ]) {
      await armyLoader({
        request: requestFor(`season=current&category=troops&population=${old}`),
        params: {},
      } as never);
      const [oldQuery] = getArmyAnalytics.mock.lastCall as [URLSearchParams];
      expect(oldQuery.get("population")).toBe("top-1000");
    }

    // Switching between Armies and Unit usage starts again from Top 1,000.
    getArmyAnalytics.mockResolvedValue(currentAnalytics("top-50", [1], 0));
    const html = await renderArmyRoute(
      "season=current&category=troops&population=top-50",
    );
    expect(html).toContain(
      'href="/analytics/armies?season=current&amp;category=army-types"',
    );
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
    expect(text).toContain(
      "Armies The army each attack used Unit usage Troops, spells, heroes and more",
    );
    expect(html).toMatch(/aria-current="page"[^>]*><strong>Armies</);
    expect(text).toContain("Thrower 6 / 12 50.0%");
    expect(text).not.toContain("not which army is best");
    // Unreadable attacks are a row of their own, not excluded.
    expect(text).toContain("Records included 12 Every attack, Unreadable included");
    expect(text).toContain("Records excluded 0");
    expect(text).toContain("Unreadable 2 / 12 16.7%");
    expect(html).toContain(
      'href="/analytics/armies?season=current&amp;lens=defense&amp;category=troops"',
    );
  });

  it("shows Avg. trophies after 3-star on Armies only, sortable", async () => {
    const row = (
      label: string,
      threeStars: number,
      trophies: number,
      miss: number | null,
    ) => ({
      key: `army-type:${label}`,
      label,
      usageCount: 4,
      usageDenominator: 8,
      usageRate: 0.5,
      starCounts: [0, 0, 4 - threeStars, threeStars],
      starRates: [0, 0, (4 - threeStars) / 4, threeStars / 4],
      threeStarRate: threeStars / 4,
      averageStars: 2 + threeStars / 4,
      averageDestruction: 95,
      averageTrophies: trophies,
      averageMissTrophies: miss,
      unknownExcludedAttacks: 0,
    });
    // Same use, and Thrower 3-stars less often, yet wins more an attack.
    const rows = [row("Hydra", 3, 35.5, 22), row("Thrower", 2, 36.04, 32.08)];
    const view = (category: string, sort: string) => {
      const analytics = currentAnalytics("top-100", [1], 1);
      return {
        ...analytics,
        selection: { ...analytics.selection, category, sort },
        rows,
      };
    };
    const getArmyAnalytics = vi
      .fn()
      .mockResolvedValue(view("army-types", "average-trophies"));
    mocks.createPythonClient.mockReturnValue({ getArmyAnalytics });
    const html = await renderArmyRoute("season=current&sort=average-trophies");
    const text = renderedText(html);
    const [query] = getArmyAnalytics.mock.calls[0] as [URLSearchParams];
    expect(query.get("sort")).toBe("average-trophies");
    expect(
      [...html.matchAll(/role="rowheader">(.*?)<\/th>/g)].map(([, name]) => name),
    ).toEqual(["Thrower", "Hydra"]);
    expect(html).toContain('data-column="average-trophies" aria-sort="descending"');
    expect(text).toContain("3-star ↕ Avg. trophies ↓ Avg. stars");
    expect(text).toContain("50.0% 2 battles 36.0 misses 32.1 2.50");
    expect(text).toContain("75.0% 3 battles 35.5 misses 22.0 2.75");
    expect(text).toContain("a 3-star wins 40 and a miss still wins some");

    // A type with variants shows, closed, that it opens and how many named
    // variants it has, its Other (id 0) apart.
    const hydra = (variantIds: number[]) => ({
      ...view("army-types", "usage-rate"),
      rows: [
        {
          ...row("Hydra", 3, 35.5, 22),
          variants: variantIds.map((variantId, index) => ({
            ...rows[index],
            key: `army-variant:4:${variantId}`,
            variantId,
          })),
        },
      ],
    });
    getArmyAnalytics.mockResolvedValue(hydra([1, 0]));
    const closed = await renderArmyRoute("season=current");
    expect(closed).toContain('aria-expanded="false"');
    expect(renderedText(closed)).toContain("Hydra 1 variant + Other 4 / 8");
    expect(renderedText(closed)).toContain("Tap an army to open its variants");
    expect(closed).not.toContain("analytics-variant-row");
    getArmyAnalytics.mockResolvedValue(hydra([1, 2]));
    expect(renderedText(await renderArmyRoute("season=current"))).toContain(
      "Hydra 2 variants 4 / 8",
    );
    // Only its Other: a plain row with nothing to open.
    getArmyAnalytics.mockResolvedValue(hydra([0]));
    const otherOnly = await renderArmyRoute("season=current");
    expect(otherOnly).not.toContain("aria-expanded");
    expect(otherOnly).not.toContain("analytics-type-row");
    expect(renderedText(otherOnly)).toContain("Hydra 4 / 8");
    expect(renderedText(otherOnly)).not.toContain("Tap an army");

    getArmyAnalytics.mockResolvedValue({
      ...view("army-types", "usage-rate"),
      rows: [{ ...row("Dragons", 4, 40, null) }],
    });
    expect(renderedText(await renderArmyRoute("season=current"))).toContain(
      "100.0% 4 battles 40.0 no misses",
    );

    // Unit usage has no trophies column.
    getArmyAnalytics.mockResolvedValue(view("troops", "usage-rate"));
    const units = await renderArmyRoute("season=current&category=troops");
    expect(units).not.toContain("average-trophies");
    expect(units).not.toContain("Avg. trophies");

    // Leaving Armies drops that sort, this Season or a past one.
    const unitUsage = (page: string) =>
      requestFor(
        page
          .match(/href="[^"?]*\?([^"]*)"[^>]*><strong>Unit usage</)![1]
          .replaceAll("&amp;", "&"),
      );
    await armyLoader({ request: unitUsage(html), params: {} } as never);
    const [unitQuery] = getArmyAnalytics.mock.lastCall as [URLSearchParams];
    expect(unitQuery.get("category")).toBe("troops");
    expect(unitQuery.get("sort")).toBe("usage-rate");
    const getArmySeasonSummary = vi.fn().mockResolvedValue({});
    mocks.createPythonClient.mockReturnValue({ getArmySeasonSummary });
    const past = await renderArmyRoute(`season=${SEASON}&sort=average-trophies`);
    await armyLoader({ request: unitUsage(past), params: {} } as never);
    const [pastSeason, pastQuery] = getArmySeasonSummary.mock.lastCall as [
      string,
      URLSearchParams,
    ];
    expect(pastSeason).toBe(SEASON);
    expect(pastQuery.get("category")).toBe("troops");
    expect(pastQuery.get("sort")).toBe("usage-rate");
  });

  it("says Armies are not available for a past Season, staying on Armies", async () => {
    const getArmySeasonSummary = vi.fn();
    mocks.createPythonClient.mockReturnValue({
      getArmySeasonSummary,
      getArmySeasons: vi.fn().mockResolvedValue([SEASON]),
    });
    const html = await renderArmyRoute(`season=${SEASON}&category=army-types`);
    expect(getArmySeasonSummary).not.toHaveBeenCalled();
    const text = renderedText(html);
    expect(text).toContain("Armies are not available for this season");
    expect(html).toMatch(/aria-current="page"[^>]*><strong>Armies</);
    expect(html).toMatch(new RegExp(`<option value="${SEASON}" selected="">`));
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
    const complete = await renderArmyRoute("season=current");
    expect(renderedText(complete)).not.toContain("days not tracked");
  });

  it("keeps notes short: no old-rank warning or star-rate explanation", async () => {
    mocks.createPythonClient.mockReturnValue({
      getArmyAnalytics: vi
        .fn()
        .mockResolvedValue(currentAnalytics("top-100", [25, 26], 100, 100)),
    });
    const html = await renderArmyRoute("season=current&category=troops");
    const text = renderedText(html);
    expect(text).not.toContain("These numbers cover");
    expect(text).not.toContain("Star rates show");
    expect(text).not.toContain("Full star breakdown");
    expect(html).not.toContain("<details");
  });

  it("offers Clan Castle Troops as a unit type, keeping old checkbox links", async () => {
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

    getArmyAnalytics.mockResolvedValue(currentAnalytics("top-100", [1], 0));
    const html = await renderArmyRoute("season=current&category=troops");
    expect(html).toContain('<option value="cc-troops">Clan Castle Troops</option>');
    expect(html).not.toContain('type="checkbox"');
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

  it("offers only leaderboard tops", async () => {
    mocks.createPythonClient.mockReturnValue({
      getArmyAnalytics: vi.fn().mockResolvedValue(currentAnalytics("top-10000", [23], 0)),
    });
    const html = await renderArmyRoute("season=current&population=top-10000");
    const picker = html.match(/<select name="population"[^>]*>(.*?)<\/select>/)?.[1];
    expect(
      [...(picker ?? "").matchAll(/<option value="([^"]+)"/g)].map((m) => m[1]),
    ).toEqual(
      [5, 10, 20, 50, 100, 200, 300, 500, 1000, 2000, 5000, 10000].map((n) => `top-${n}`),
    );
    expect(html).not.toContain("<optgroup");
    expect(html).not.toContain('name="trophy_min"');
    // An empty group still shows the page, with zero records and no rows.
    const text = renderedText(html);
    expect(text).toContain("by top-10,000 players");
    expect(text).toContain("Battle records 0 Recorded in this selection");
    expect(text).toContain("No recognized components in this selection.");
  });

  it.each(["top-300", "top-2000", "top-10000"])(
    "asks the API for %s",
    async (population) => {
      const getArmyAnalytics = vi.fn().mockResolvedValue({ selection: {} });
      mocks.createPythonClient.mockReturnValue({ getArmyAnalytics });
      await armyLoader({
        request: requestFor(`season=current&population=${population}`),
        params: {},
      } as never);
      const [query] = getArmyAnalytics.mock.calls[0] as [URLSearchParams];
      expect(query.get("population")).toBe(population);
    },
  );

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
    const tooLong =
      "This player group took too long to load right now. Try a smaller group.";
    const json = { "content-type": "application/json" };
    try {
      for (const [when, send, message] of [
        ["waiting for the reply", () => {}, tooLong],
        [
          "reading the reply",
          (response: ServerResponse) => {
            response.writeHead(200, json);
            response.write('{"kind":');
          },
          tooLong,
        ],
        [
          "the database cancels",
          (response: ServerResponse) =>
            response.writeHead(503, json).end('{"error":"timeout"}'),
          tooLong,
        ],
        [
          "the service fails another way",
          (response: ServerResponse) =>
            response.writeHead(503, json).end('{"error":"service_unavailable"}'),
          "the live service is unavailable",
        ],
      ] as const) {
        reply = send;
        const html = await renderArmyRoute("season=current&population=top-10000");
        const text = renderedText(html);
        expect(text, when).toContain(message);
        expect(text, when).not.toContain("malformed");
        expect(html).not.toContain("<table");
        expect(html).toMatch(/<option value="top-10000" selected="">/);
      }
      expect(limit).toHaveBeenCalledWith(5_000);
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
    expect(text).toContain("2 of 28 days");
  });

  it("names the unit types as the owner asked, under Unit Type", async () => {
    mocks.createPythonClient.mockReturnValue({
      getArmyAnalytics: vi.fn().mockResolvedValue(currentAnalytics("top-100", [1], 0)),
    });
    const html = await renderArmyRoute("season=current&category=troops");
    const picker = html.match(
      /Unit Type<select name="category"[^>]*>(.*?)<\/select>/,
    )?.[1];
    expect(
      [...(picker ?? "").matchAll(/<option[^>]*>([^<]+)</g)].map((m) => m[1]),
    ).toEqual([
      "Troops",
      "Spells",
      "Siege Machines",
      "Heroes",
      "Pets",
      "Equipments",
      "Hero + Equipment",
      "Hero + Pet",
      "Hero + Both Equipments",
      "Clan Castle Troops",
    ]);
    expect(renderedText(html)).not.toContain("Show ");
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
    expect(first).toContain(">Current Season</option>");

    // A later season lists each finished season the API reports.
    getArmySeasons.mockResolvedValue([SEASON]);
    const later = picker(await renderArmyRoute("season=current"));
    expect(later.match(/<option/g)).toHaveLength(2);
    // Named by the day it ended.
    expect(later).toContain(`value="${SEASON}">30 Aug 2026</option>`);

    // A failed list still leaves the current season to pick.
    getArmySeasons.mockRejectedValue(new Error("boom"));
    const failed = await renderArmyRoute("season=current");
    expect(failed).toContain('aria-label="Army statistics"');
    expect(picker(failed).match(/<option/g)).toHaveLength(1);
  });
});

describe("army analytics top-bar link", () => {
  beforeEach(() => {
    mocks.createPythonClient.mockReset();
  });

  it("drops a chosen sort and lists Usage highest first again", async () => {
    const rows = (
      [
        ["Archer", 1],
        ["Wizard", 3],
      ] as const
    ).map(([label, count]) => ({
      key: `troop:${label}`,
      label,
      usageCount: count,
      usageDenominator: 4,
      usageRate: count / 4,
      starCounts: [0, 0, 0, count],
      starRates: [0, 0, 0, 1],
      threeStarRate: 1,
      averageStars: 3,
      averageDestruction: 100,
      unknownExcludedAttacks: 0,
    }));
    mocks.createPythonClient.mockReturnValue({
      getArmyAnalytics: vi
        .fn()
        .mockResolvedValue({ ...currentAnalytics("top-100", [1], 1), rows }),
    });
    const page = { states: [] as unknown[], next: 0 };
    function OpenPage() {
      page.next = 0;
      mocks.openPage = page;
      try {
        return ArmyRoute();
      } finally {
        mocks.openPage = null;
      }
    }
    const router = createMemoryRouter(
      [{ path: "/analytics/armies", Component: OpenPage, loader: armyLoader }],
      {
        initialEntries: [
          "/analytics/armies?season=current&category=troops&population=top-100",
        ],
      },
    );
    await vi.waitFor(() => expect(router.state.initialized).toBe(true));
    const shown = () => {
      const html = renderToString(createElement(RouterProvider, { router }));
      return {
        rows: [...html.matchAll(/role="rowheader">(.*?)<\/th>/g)].map(([, name]) => name),
        sorted: html.match(/data-column="([^"]+)" aria-sort="([^"]+)"/)?.slice(1),
      };
    };
    expect(shown()).toEqual({
      rows: ["Wizard", "Archer"],
      sorted: ["usage-rate", "descending"],
    });

    // Sort by Name, A to Z: the table's chosen sort is the state that starts empty.
    page.states[page.states.indexOf(null)] = { column: "name", direction: "ascending" };
    expect(shown()).toEqual({
      rows: ["Archer", "Wizard"],
      sorted: ["name", "ascending"],
    });

    await router.navigate("/analytics/armies");
    expect(shown()).toEqual({
      rows: ["Wizard", "Archer"],
      sorted: ["usage-rate", "descending"],
    });
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
    expect(text).toContain("1 of the included armies is partly readable.");
    expect(text).toContain("128,473 / 131,020");
    expect(text).toContain("71.1% 91,388 battles");
    expect(text).toContain("Unknown ID 4000123");
    expect(text).toContain("100.0% 1 battle");
  });

  it("says an empty table has no components instead of showing a bare header", async () => {
    mocks.createPythonClient.mockReturnValue({
      getArmyAnalytics: vi.fn().mockResolvedValue(currentAnalytics("top-100", [1], 0)),
    });
    const html = await renderArmyRoute("season=current");
    expect(html.match(/No recognized components in this selection\./g)).toHaveLength(1);
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
    const text = renderedText(await renderArmyRoute(`season=${SEASON}&category=troops`));
    expect(text).toContain("385,419");
    expect(text).toContain("Showing 2 of 1,284 results");
  });
});
