import { createServer, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
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
import { dayRangeProblem } from "../../app/lib/validation";
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

  it("names the finished days covered when current-season days are missing", async () => {
    const untracked = Array.from({ length: 22 }, (_, index) => index + 1).concat(24);
    mocks.createPythonClient.mockReturnValue({
      getArmyAnalytics: vi
        .fn()
        .mockResolvedValueOnce(
          currentAnalytics("top-100", [23, 25, 26], 100, 0, untracked),
        )
        .mockResolvedValueOnce(currentAnalytics("top-100", [25, 26], 100)),
    });
    const html = await renderArmyRoute("season=current");
    expect(renderedText(html)).toContain(
      "Days 23, 25–26 of 28; days not tracked: 1–22, 24.",
    );
    // The form keeps asking for the whole range so later days appear once ready.
    expect(html).toMatch(/name="start_day"[^>]*value="1"/);
    expect(html).toMatch(/name="end_day"[^>]*value="28"/);
    // Consistent top needs every selected day, so it points at the tracked run.
    expect(html).toContain('value="top-100"');
    expect(html).not.toContain("streak-top-");
    expect(renderedText(html)).toContain(
      "Consistent top needs every selected day tracked. Use days 25–26",
    );
    expect(html).toMatch(/href="[^"]*start_day=25&amp;end_day=26/);
    const chosen = await renderArmyRoute("season=current&start_day=25");
    expect(renderedText(chosen)).not.toContain("days not tracked");
    expect(chosen).toContain('value="streak-top-100"');
    expect(renderedText(chosen)).not.toContain("needs every selected day");
  });

  it("withholds Consistent top while a selected ended day lacks a saved board", async () => {
    mocks.createPythonClient.mockReturnValue({
      getArmyAnalytics: vi
        .fn()
        .mockResolvedValueOnce(
          currentAnalytics("top-100", [23, 24, 25, 26], 100, 0, [24]),
        )
        .mockResolvedValueOnce(currentAnalytics("top-100", [23, 24], 100, 0, [25, 26])),
    });
    const middle = await renderArmyRoute("season=current&start_day=23&end_day=26");
    expect(middle).not.toContain("streak-top-");
    expect(renderedText(middle)).toContain(
      "Consistent top needs every selected day tracked. Use days 25–26",
    );
    expect(middle).toMatch(/href="[^"]*start_day=25&amp;end_day=26/);
    const trailing = await renderArmyRoute("season=current&start_day=23&end_day=26");
    expect(trailing).not.toContain("streak-top-");
    expect(trailing).toMatch(/href="[^"]*start_day=23&amp;end_day=24/);
  });

  it("explains how Consistent top is decided, including an empty group", async () => {
    const getArmyAnalytics = vi
      .fn()
      .mockResolvedValueOnce(currentAnalytics("streak-top-100", [25, 26], 66, 66))
      .mockResolvedValueOnce(currentAnalytics("streak-top-5", [25, 26], 0));
    mocks.createPythonClient.mockReturnValue({ getArmyAnalytics });
    const filled = renderedText(
      await renderArmyRoute(
        "season=current&start_day=25&end_day=26&population=streak-top-100",
      ),
    );
    expect(filled).toContain("players in the top 100 on every selected day.");
    expect(filled).toContain(
      "66 players were in the top 100 on every selected day. Top 100 on a day means the top 100 of the leaderboard saved just before that day’s Reset (05:00 UTC), ranked by the last trophy count we saw for each player. 66 of these players had a trophy count over 10 minutes old at Reset on at least one day. Comparison with settled end-of-day ranks: not available yet.",
    );
    const empty = renderedText(
      await renderArmyRoute(
        "season=current&start_day=25&end_day=26&population=streak-top-5",
      ),
    );
    expect(empty).toContain("No player was in the top 5 on every selected day.");
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
      renderedText(
        await renderArmyRoute(
          `season=current&start_day=25&end_day=26&population=${population}`,
        ),
      );
    expect(await render("top-100")).toContain(
      "Ranks come from the leaderboard saved just before the last selected day’s Reset (05:00 UTC). 100 of these 100 players had a trophy count over 10 minutes old at that Reset, so their rank may be out of date.",
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
    const html = await renderArmyRoute(
      "season=current&start_day=24&end_day=26&population=streak-top-50",
    );
    expect(renderedText(html)).toContain("No army stats for these days yet");
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
          lens: "defense",
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

describe("army analytics day range", () => {
  it.each([
    ["1", "28", null],
    ["14", "14", null],
    ["27", "26", "can’t be after"],
    ["", "26", "whole numbers"],
    ["0", "26", "whole numbers"],
    ["1", "29", "whole numbers"],
    ["1.5", "26", "whole numbers"],
  ])("checks From %s to %s before the page asks for results", (start, end, message) => {
    const problem = dayRangeProblem(start, end);
    if (message === null) expect(problem).toBeNull();
    else expect(problem).toContain(message);
  });

  it("links both day fields to the message that explains a rejected range", async () => {
    mocks.createPythonClient.mockReturnValue({
      getArmyAnalytics: vi.fn().mockResolvedValue(currentAnalytics("top-100", [23], 0)),
    });
    const html = await renderArmyRoute("season=current&start_day=20&end_day=23");
    for (const name of ["start_day", "end_day"]) {
      expect(html).toMatch(
        new RegExp(`aria-describedby="day-range-help" name="${name}"`),
      );
    }
    expect(html).toMatch(/id="day-range-help"[^>]*>Only completed Legend days/);
  });
});
