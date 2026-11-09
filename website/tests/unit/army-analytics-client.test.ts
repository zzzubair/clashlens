import { CALENDAR, SEASON_BOUNDS } from "../fixtures/player-route-data";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
const TEST_SECRET = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8";

describe("army analytics response boundary", () => {
  const savedEnvironment = {
    NODE_ENV: process.env.NODE_ENV,
    CLASHLENS_PYTHON_API_URL: process.env.CLASHLENS_PYTHON_API_URL,
    CLASHLENS_PYTHON_HMAC_SECRET_B64: process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64,
    CLASHLENS_PYTHON_HMAC_SECRET_FILE: process.env.CLASHLENS_PYTHON_HMAC_SECRET_FILE,
  };

  beforeEach(() => {
    vi.resetModules();
    process.env.CLASHLENS_PYTHON_API_URL = "http://python-fixture.test/";
    delete process.env.CLASHLENS_PYTHON_HMAC_SECRET_FILE;
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    for (const [name, value] of Object.entries(savedEnvironment)) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
  });

  function armyAnalyticsPayload() {
    return {
      kind: "army-analytics",
      calendar: CALENDAR,
      season_bounds: {
        id: "1791176400",
        start: "2026-10-05T05:00:00Z",
        end: "2026-11-02T05:00:00Z",
        last_day_start: "2026-11-01T05:00:00Z",
        day_count: 28,
      },
      total_attacks: 2,
      usable_army_sample: 2,
      army_states: { fully_decoded: 1, partial: 1 },
      army_states_sum_confirmed: true,
      unknown_affected_attacks: 1,
      unknown_component_occurrences: 1,
      perspective_disagreement_count: 0,
      missing_trophy_membership_evidence: 3,
      cohort_evidence: {
        cohort_players: 4,
        stale_or_uncertain_cohort_members: 2,
        streak_excluded_players: 1,
        shielded_player_days: 3,
      },
      collection_coverage: { state: "complete", completed_days: 8, covered_days: [9] },
      freshness: { state: "frozen" },
      reproducibility: {
        official_season_id: "1783918800",
        legend_days: [23, 23],
        snapshot_versions: [4],
      },
      versions: {
        decoder: "army-decoder-v2",
        catalog: "unit-catalog-v1",
        analytics: "army-analytics-v2",
      },
      publication_identity: "army-publication-abc123-v1",
      selection: {
        lens: "offense",
        season: "1783918800",
        start_day: 23,
        end_day: 23,
        population: "top-100",
        category: "troops",
        sort: "usage-rate",
      },
      rows: [
        {
          key: "troop:58",
          label: "Ice Golem",
          usage_count: 2,
          usage_denominator: 2,
          usage_rate: 1,
          star_counts: [0, 0, 0, 2],
          star_rates: [0, 0, 0, 1],
          three_star_rate: 1,
          average_stars: 3,
          average_destruction: 100,
          unknown_excluded_attacks: 0,
        },
      ],
    };
  }

  it("translates a current season with no completed days into the empty-state error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            error: "no_completed_legend_days",
            calendar: CALENDAR,
            previous_season_id: "1783916800",
          }),
          { status: 404, headers: { "content-type": "application/json" } },
        ),
      ),
    );
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;
    const mod = await import("../../app/services/army-analytics.server");
    const error = await (
      await import("../../app/services/army-analytics.server")
    )
      .getArmyAnalytics(new URLSearchParams({ season: "current" }))
      .catch((cause: unknown) => cause);
    expect(error).toBeInstanceOf(mod.NoCompletedLegendDaysError);
    expect(error).toMatchObject({ previousSeasonId: "1783916800" });
  });

  it("maps the army analytics payload and preserves URL-backed state", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(armyAnalyticsPayload()), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;
    const query = new URLSearchParams({
      season: "1783918800",
      lens: "defense",
      start_day: "3",
      end_day: "9",
      population: "band-51-100",
      category: "equipment-for-hero",
      sort: "average-destruction",
    });
    const mapped = await (
      await import("../../app/services/army-analytics.server")
    ).getArmyAnalytics(query);
    expect(fetchMock.mock.calls[0][0]).toBeInstanceOf(URL);
    expect((fetchMock.mock.calls[0][0] as URL).pathname).toBe("/v1/analytics/armies");
    expect(mapped).toMatchObject({
      kind: "army-analytics",
      calendar: CALENDAR,
      seasonBounds: {
        id: "1791176400",
        start: "2026-10-05T05:00:00Z",
        end: "2026-11-02T05:00:00Z",
        last_day_start: "2026-11-01T05:00:00Z",
        day_count: 28,
      },
      totalAttacks: 2,
      usableArmySample: 2,
      armyStates: { fully_decoded: 1, partial: 1 },
      armyStatesSumConfirmed: true,
      unknownAffectedAttacks: 1,
      perspectiveDisagreementCount: 0,
      missingTrophyMembershipEvidence: 3,
      cohortEvidence: {
        cohortPlayers: 4,
        staleOrUncertainCohortMembers: 2,
        streakExcludedPlayers: 1,
        shieldedPlayerDays: 3,
      },
      collectionCoverage: { state: "complete", completedDays: 8, coveredDays: [9] },
      freshness: { state: "frozen" },
      reproducibility: {
        officialSeasonId: "1783918800",
        legendDays: [23, 23],
        snapshotVersions: [4],
      },
      publicationIdentity: "army-publication-abc123-v1",
      versions: { decoder: "army-decoder-v2", analytics: "army-analytics-v2" },
      selection: {
        // The echoed selection is the Python-resolved canonical state, not
        // just the submitted URL parameters.
        lens: "offense",
        season: "1783918800",
        startDay: 23,
        endDay: 23,
        population: "top-100",
        category: "troops",
        sort: "usage-rate",
      },
    });
    expect(mapped.rows[0]).toMatchObject({
      key: "troop:58",
      usageCount: 2,
      usageDenominator: 2,
      threeStarRate: 1,
      starCounts: [0, 0, 0, 2],
    });
  });

  it("maps army types' average trophies and refuses a malformed one", async () => {
    const [unit] = armyAnalyticsPayload().rows;
    const payload = (miss: unknown) => ({
      ...armyAnalyticsPayload(),
      rows: [
        {
          ...unit,
          key: "army-type:3",
          average_trophies: 40,
          average_miss_trophies: null,
          variants: [
            {
              ...unit,
              key: "army-variant:3:3",
              variant_id: 3,
            },
          ],
        },
        {
          ...unit,
          key: "army-type:4",
          average_trophies: 36.6,
          average_miss_trophies: miss,
        },
      ],
    });
    const fetchMock = vi.fn(() =>
      Promise.resolve(
        new Response(
          JSON.stringify(payload(fetchMock.mock.calls.length > 1 ? "26" : 26.3)),
          {
            status: 200,
            headers: { "content-type": "application/json" },
          },
        ),
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;
    const query = new URLSearchParams({ season: "current", category: "army-types" });
    const mapped = await (
      await import("../../app/services/army-analytics.server")
    ).getArmyAnalytics(query);
    expect(
      mapped.rows.map(({ averageTrophies, averageMissTrophies }) => [
        averageTrophies,
        averageMissTrophies,
      ]),
    ).toEqual([
      [40, null],
      [36.6, 26.3],
    ]);
    // An Armies type row carries its variants.
    expect(mapped.rows[0].variants?.map((row) => [row.key, row.variantId])).toEqual([
      ["army-variant:3:3", 3],
    ]);
    expect(mapped.rows[1].variants).toBeUndefined();
    await expect(
      (await import("../../app/services/army-analytics.server")).getArmyAnalytics(query),
    ).rejects.toMatchObject({
      status: 502,
    });
  });

  it("maps a whole-season army summary without day ranges or population filters", async () => {
    const payload = {
      ...armyAnalyticsPayload(),
      history_usage_only: true,
      rows: [
        {
          key: "troop:58@5",
          unit_id: "troop:58",
          label: "Yeti",
          quantity: 5,
          usage_count: 2,
          usage_denominator: 4,
          usage_rate: 0.5,
          one_star_count: 0,
          two_star_count: 1,
          three_star_count: 1,
        },
      ],
      publication_identity: "army-season-abc123-def45678",
      collection_coverage: { state: "partial", completed_days: 20 },
      reproducibility: {
        official_season_id: "1785714000",
        legend_days: [1, 28],
        snapshot_versions: [],
      },
      selection: {
        lens: "defense",
        season: "1785714000",
        start_day: 1,
        end_day: 28,
        population: "all",
        category: "troops",
        sort: "usage-rate",
      },
    };
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(payload), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;
    const query = new URLSearchParams({
      lens: "defense",
      category: "troops",
      sort: "usage-rate",
    });
    const mapped = await (
      await import("../../app/services/army-analytics.server")
    ).getArmySeasonSummary("1785714000", query);
    const url = fetchMock.mock.calls[0][0] as URL;
    expect(url.pathname).toBe("/v1/analytics/armies/seasons/1785714000");
    expect(url.searchParams.has("start_day")).toBe(false);
    expect(url.searchParams.has("population")).toBe(false);
    expect(mapped.selection).toMatchObject({
      startDay: 1,
      endDay: 28,
      population: "all",
    });
    expect(mapped.collectionCoverage).toEqual({ state: "partial", completedDays: 20 });
    expect(mapped.rows[0]).toMatchObject({
      usageCount: 2,
      quantity: 5,
      oneStarCount: 0,
      twoStarCount: 1,
      threeStarCount: 1,
    });
    expect(mapped.rows[0].starCounts).toBeUndefined();
  });

  it("reads the finished seasons that have army data and rejects a malformed list", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        Response.json({
          seasons: ["1785714000", "1783296000"],
          season_details: [SEASON_BOUNDS["1785714000"], SEASON_BOUNDS["1783296000"]],
        }),
      )
      .mockResolvedValueOnce(Response.json({ seasons: [1785714000] }));
    vi.stubGlobal("fetch", fetchMock);
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;
    await expect(
      (await import("../../app/services/army-analytics.server")).getArmySeasons(),
    ).resolves.toEqual([SEASON_BOUNDS["1785714000"], SEASON_BOUNDS["1783296000"]]);
    expect((fetchMock.mock.calls[0][0] as URL).pathname).toBe(
      "/v1/analytics/armies/seasons",
    );
    await expect(
      (await import("../../app/services/army-analytics.server")).getArmySeasons(),
    ).rejects.toMatchObject({
      status: 502,
      payload: { error: "malformed" },
    });
  });

  it("rejects an army analytics payload with malformed evidence coverage", async () => {
    const payload = armyAnalyticsPayload();
    delete (payload as Record<string, unknown>).cohort_evidence;
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(payload), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;
    await expect(
      (await import("../../app/services/army-analytics.server")).getArmyAnalytics(
        new URLSearchParams({ season: "x" }),
      ),
    ).rejects.toMatchObject({ status: 502, payload: { error: "malformed" } });
  });

  it.each([
    [
      "wrong army analytics kind",
      (payload: Record<string, unknown>) => {
        payload.kind = "army-analytics-v1";
      },
    ],
    [
      "missing army-state reconciliation flag",
      (payload: Record<string, unknown>) => {
        delete payload.army_states_sum_confirmed;
      },
    ],
    [
      "non-integer army-state count",
      (payload: Record<string, unknown>) => {
        (payload.army_states as Record<string, unknown>).partial = "1";
      },
    ],
    [
      "missing publication identity",
      (payload: Record<string, unknown>) => {
        delete payload.publication_identity;
      },
    ],
    [
      "a truncated star-count vector",
      (payload: Record<string, unknown>) => {
        (payload.rows as Array<Record<string, unknown>>)[0].star_counts = [0, 0, 0];
      },
    ],
    [
      "a malformed row denominator",
      (payload: Record<string, unknown>) => {
        (payload.rows as Array<Record<string, unknown>>)[0].usage_denominator = "2";
      },
    ],
  ])("rejects army analytics with %s", async (_name, mutate) => {
    const payload = armyAnalyticsPayload();
    mutate(payload);
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(payload), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;
    await expect(
      (await import("../../app/services/army-analytics.server")).getArmyAnalytics(
        new URLSearchParams("season=x"),
      ),
    ).rejects.toMatchObject({ status: 502, payload: { error: "malformed" } });
  });
});
