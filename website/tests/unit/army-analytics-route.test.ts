import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  createPythonClient: vi.fn(),
}));

vi.mock("../../app/services/python.server", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../app/services/python.server")>();
  return { ...actual, createPythonClient: mocks.createPythonClient };
});

import { loader as armyLoader } from "../../app/routes/army-analytics";
import { PythonApiError } from "../../app/services/python.server";

const SEASON = "1785714000";

function requestFor(query: string) {
  return new Request(`https://clashlens.example/analytics/armies?${query}`);
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

  it("falls back to the requested detailed selection only on a missing summary", async () => {
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
    expect(getArmyAnalytics).toHaveBeenCalledTimes(1);
    const [query] = getArmyAnalytics.mock.calls[0] as [URLSearchParams];
    expect(query.get("season")).toBe(SEASON);
    expect(query.get("lens")).toBe("defense");
    expect(query.get("start_day")).toBe("3");
    expect(query.get("end_day")).toBe("9");
    expect(query.get("population")).toBe("band-51-100");
    expect(query.get("category")).toBe("heroes");
    expect(query.get("sort")).toBe("usage-count");
    expect(data).toMatchObject({
      error: null,
      seasonEmpty: null,
      historicalSummary: false,
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
});
