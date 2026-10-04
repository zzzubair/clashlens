import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const TEST_SECRET = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8";

function seasonPayload() {
  return {
    kind: "player-season-summary",
    tag: "#2PP",
    official_season_id: "1785714000",
    season_start: "2026-05-01T05:00:00+00:00",
    season_end: "2026-05-29T05:00:00+00:00",
    start_trophies: 6000,
    end_trophies: 6280,
    final_rank: null,
    attack_count: 56,
    attack_gain: 840,
    attack_three_star_count: 28,
    defense_count: 28,
    defense_loss: 560,
    defense_three_star_count: 0,
    net_trophy_change: 280,
    attack_stars: { "0": 0, "1": 0, "2": 28, "3": 28 },
    attack_stars_unknown: 0,
    defense_stars: { "0": 0, "1": 28, "2": 0, "3": 0 },
    defense_stars_unknown: 0,
    days_observed: 28,
    days_missing: 0,
    missing_days: [],
    coverage_state: "partial",
    unresolved_flags: [],
    daily_entries: [
      {
        season_day_number: 1,
        ranked_day_start: "2026-05-01T05:00:00+00:00",
        ranked_day_end: "2026-05-02T05:00:00+00:00",
        start_trophies: 6000,
        end_trophies: 6010,
        attack_gain: 30,
        defense_loss: 20,
        net_change: 10,
        attack_count: 2,
        defense_count: 1,
        attack_three_star_count: 1,
        defense_three_star_count: 0,
        state: "Complete",
        coverage: "complete",
        confidence: "exact",
        has_adjustment: false,
        adjustment_total: null,
        flags: [],
      },
    ],
    projection_version: "player-season-summary-v1",
    published_at: "2026-05-29T06:00:00+00:00",
    source: "tracked_summary",
    official_history: null,
  };
}

function officialSeasonPayload(overrides: Record<string, unknown> = {}) {
  return {
    ...seasonPayload(),
    official_season_id: "1781499600",
    season_start: "2026-06-15T05:00:00+00:00",
    season_end: "2026-07-13T05:00:00+00:00",
    source: "official_league_history",
    start_trophies: null,
    end_trophies: 5812,
    final_rank: null,
    attack_count: null,
    attack_gain: null,
    defense_count: null,
    defense_loss: null,
    net_trophy_change: null,
    attack_stars: { "0": null, "1": null, "2": null, "3": null },
    defense_stars: { "0": null, "1": null, "2": null, "3": null },
    attack_stars_unknown: null,
    defense_stars_unknown: null,
    days_observed: 0,
    days_missing: 28,
    missing_days: Array.from({ length: 28 }, (_, index) => index + 1),
    daily_entries: [],
    published_at: null,
    official_history: {
      source: "official_league_history",
      observed_at: "2026-08-04T12:05:00+00:00",
      eod_trophies: 5812,
      final_placement: 12,
    },
    ...overrides,
  };
}

function seasonsPayload() {
  return {
    tag: "#2PP",
    seasons: [
      {
        official_season_id: "1785714000",
        coverage_state: "partial",
        days_observed: 28,
        days_missing: 0,
        start_trophies: 6000,
        end_trophies: 6280,
        published_at: "2026-05-29T06:00:00+00:00",
        source: "tracked_summary",
        official_history: null,
      },
    ],
  };
}

describe("historical player-season client boundary", () => {
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

  it.each([
    [true, "eligible", "tracking"],
    [false, "ineligible", "not_in_legend"],
    [false, "eligible", "uncertain"],
    [false, "uncertain", "uncertain"],
    [undefined, "eligible", "uncertain"],
    ["true", "eligible", "uncertain"],
  ])(
    "carries active=%s and eligibility=%s through profile and Refresh responses",
    async (active, eligibility, trackingState) => {
      const payload = {
        tag: "#2PP",
        name: "Nova",
        trophies: 6000,
        active,
        eligibility,
        observed_at: "2026-08-06T12:00:00Z",
        screen_ready: {
          days: [],
          current_day_start: null,
          recent_day_starts: [],
          season_day_starts: [],
          season: null,
          data_quality: [],
          provenance: {
            source: "test",
            observed_at: "2026-08-06T12:00:00Z",
            freshness: "fresh",
            confidence: "partial",
            coverage: "partial",
            version: "test",
          },
        },
      };
      const workId = "00000000-0000-4000-8000-000000000001";
      vi.stubGlobal(
        "fetch",
        vi.fn(
          async (input: string | URL) =>
            new Response(
              JSON.stringify(
                String(input).includes("/refreshes/")
                  ? {
                      refresh_id: workId,
                      tag: "#2PP",
                      status: "complete",
                      outcome: "Complete",
                    }
                  : payload,
              ),
              { status: 200 },
            ),
        ),
      );
      process.env.NODE_ENV = "test";
      process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;
      const { createPythonClient } = await import("../../app/services/python.server");
      const client = createPythonClient();
      expect((await client.getPlayer("#2PP")).trackingState).toBe(trackingState);
      expect((await client.getRefreshStatus(workId, "#2PP")).player?.trackingState).toBe(
        trackingState,
      );
    },
  );

  it.each([
    // October day 1 after the player's Season reset.
    ["2026-10-05", 1, "1791176400", false, "2026-10-05T20:00:00Z", 5000, null, 5000],
    // October day 1 still read from a September profile.
    ["2026-10-05", 1, "1788757200", true, "2026-10-05T20:00:00Z", 5000, null, null],
    // October day 1 from an October profile still showing pre-Reset trophies.
    ["2026-10-05", 1, "1791176400", true, "2026-10-05T20:00:00Z", 5000, null, null],
    // A day 1 start other than 5,000 is not one, calculated or saved.
    ["2026-10-05", 1, "1791176400", false, "2026-10-05T20:00:00Z", 5957, null, null],
    ["2026-10-05", 1, "1791176400", false, "2026-10-06T06:00:00Z", 5000, 5957, null],
    ["2026-10-05", 1, "1791176400", false, "2026-10-06T06:00:00Z", 5000, 5000, 5000],
    // September day 28 read from a September profile, seen at October 5 05:10.
    ["2026-10-04", 28, "1788757200", true, "2026-10-04T23:00:00Z", 5000, null, 5000],
  ])(
    "uses an in-day profile as a day total only for its own Season: %s day %s",
    async (date, dayNumber, seasonId, pending, observedAt, trophies, saved, expected) => {
      // A day with all sixteen battles, read before any stored starting total.
      const event = (id: string, change: number) => ({
        battle_id: id,
        battle_timestamp: `${date}T13:00:00Z`,
        opponent: { tag: "#2PY", name: "Opponent" },
        stars: 3,
        destruction_percentage: 100,
        trophy_change: change,
      });
      const day = {
        ranked_day_start: `${date}T05:00:00Z`,
        ranked_day_end: new Date(Date.parse(`${date}T05:00:00Z`) + 86_400_000)
          .toISOString()
          .replace(".000Z", "Z"),
        season_day_number: dayNumber,
        state: "Live",
        confidence: "partial",
        completeness: { state: "partial", reason: "No saved reset total." },
        public_confidence: "partial",
        uncertainty_reasons: [],
        start_trophies: saved,
        attack_count: 8,
        attack_three_star_count: 8,
        attack_gain: 320,
        defense_count: 8,
        defense_three_star_count: 8,
        defense_loss: 320,
        net_trophy_change: null,
        offense_events: Array.from({ length: 8 }, (_, i) => event(`a${i}`, 40)),
        defense_events: Array.from({ length: 8 }, (_, i) => event(`d${i}`, -40)),
      };
      const payload = {
        tag: "#2PP",
        name: "Nova",
        trophies,
        season_reset_pending: pending,
        current_league_season_id: seasonId,
        observed_at: observedAt,
        screen_ready: {
          days: [day],
          current_day_start: day.ranked_day_start,
          recent_day_starts: [day.ranked_day_start],
          season_day_starts: [day.ranked_day_start],
          season: null,
          data_quality: [],
          provenance: {
            source: "test",
            observed_at: observedAt,
            freshness: "fresh",
            confidence: "partial",
            coverage: "partial",
            version: "test",
          },
        },
      };
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(new Response(JSON.stringify(payload), { status: 200 })),
      );
      process.env.NODE_ENV = "test";
      process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;
      const { createPythonClient } = await import("../../app/services/python.server");
      const player = await createPythonClient().getPlayer("#2PP");
      expect(player.profile.seasonResetPending).toBe(pending);
      expect(player.currentDay?.startTrophies).toBe(expected);
      expect(player.seasonDays[0].startTrophies).toBe(expected);
      expect(player.recentDays[0].startTrophies).toBe(expected);
    },
  );

  it.each([
    [5957, null],
    [5000, 5000],
  ])(
    "works back to a day 1 start from day 2's saved %s only when it gives 5,000",
    async (dayTwoStart, expected) => {
      const event = (id: string, change: number) => ({
        battle_id: id,
        battle_timestamp: "2026-10-05T13:00:00Z",
        opponent: { tag: "#2PY", name: "Opponent" },
        stars: 3,
        destruction_percentage: 100,
        trophy_change: change,
      });
      const day = (start: string, end: string, dayNumber: number) => ({
        ranked_day_start: start,
        ranked_day_end: end,
        season_day_number: dayNumber,
        state: "Partial",
        confidence: "partial",
        completeness: { state: "partial", reason: "No saved reset total." },
        public_confidence: "partial",
        uncertainty_reasons: [],
        start_trophies: dayNumber === 2 ? dayTwoStart : null,
        attack_count: dayNumber === 1 ? 8 : 0,
        attack_three_star_count: dayNumber === 1 ? 8 : 0,
        attack_gain: dayNumber === 1 ? 320 : 0,
        defense_count: dayNumber === 1 ? 8 : 0,
        defense_three_star_count: dayNumber === 1 ? 8 : 0,
        defense_loss: dayNumber === 1 ? 320 : 0,
        net_trophy_change: null,
        offense_events:
          dayNumber === 1 ? Array.from({ length: 8 }, (_, i) => event(`a${i}`, 40)) : [],
        defense_events:
          dayNumber === 1 ? Array.from({ length: 8 }, (_, i) => event(`d${i}`, -40)) : [],
      });
      const days = [
        day("2026-10-05T05:00:00Z", "2026-10-06T05:00:00Z", 1),
        day("2026-10-06T05:00:00Z", "2026-10-07T05:00:00Z", 2),
      ];
      const observedAt = "2026-10-07T06:00:00Z";
      const payload = {
        tag: "#2PP",
        name: "Nova",
        trophies: dayTwoStart,
        season_reset_pending: false,
        current_league_season_id: "1791176400",
        observed_at: observedAt,
        screen_ready: {
          days,
          current_day_start: null,
          recent_day_starts: days.map((d) => d.ranked_day_start),
          season_day_starts: days.map((d) => d.ranked_day_start),
          season: null,
          data_quality: [],
          provenance: {
            source: "test",
            observed_at: observedAt,
            freshness: "fresh",
            confidence: "partial",
            coverage: "partial",
            version: "test",
          },
        },
      };
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(new Response(JSON.stringify(payload), { status: 200 })),
      );
      process.env.NODE_ENV = "test";
      process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;
      const { createPythonClient } = await import("../../app/services/python.server");
      const player = await createPythonClient().getPlayer("#2PP");
      expect(player.seasonDays[0].startTrophies).toBe(expected);
      expect(player.recentDays[0].startTrophies).toBe(expected);
    },
  );

  it("maps summarized seasons and one compact season without battle drilldown", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify(seasonsPayload()), { status: 200 }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify(seasonPayload()), { status: 200 }),
      );
    vi.stubGlobal("fetch", fetchMock);
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;
    const { createPythonClient } = await import("../../app/services/python.server");

    const seasons = await createPythonClient().getPlayerSeasons("#2PP");
    expect(seasons).toEqual([
      {
        seasonId: "1785714000",
        coverageState: "partial",
        daysObserved: 28,
        daysMissing: 0,
        source: "tracked_summary",
        officialHistory: null,
      },
    ]);

    const summary = await createPythonClient().getPlayerSeason("#2PP", "1785714000");
    expect(summary).toMatchObject({
      kind: "player-season-summary",
      seasonId: "1785714000",
      attackCount: 56,
      netTrophyChange: 280,
      finalRank: null,
    });
    expect(summary.dailyEntries).toHaveLength(1);
    expect(summary.dailyEntries[0]).toMatchObject({
      dayNumber: 1,
      startTrophies: 6000,
      endTrophies: 6010,
    });
    expect(Object.keys(summary.dailyEntries[0])).not.toContain("battles");
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(String(fetchMock.mock.calls[1]?.[0])).toContain(
      "/v1/players/%232PP/seasons/1785714000",
    );
  });

  it("preserves unknown-star totals, day state and flags, and adjustments", async () => {
    const clean = seasonPayload().daily_entries[0];
    const payload = {
      ...seasonPayload(),
      attack_stars_unknown: 2,
      defense_stars_unknown: 1,
      daily_entries: [
        clean,
        {
          ...clean,
          season_day_number: 2,
          ranked_day_start: "2026-05-02T05:00:00+00:00",
          ranked_day_end: "2026-05-03T05:00:00+00:00",
          state: "Partial",
          coverage: "partial",
          has_adjustment: true,
          adjustment_total: -15,
          flags: ["late_data"],
          eod_state: "provisional",
          eod_change: 1010,
          eod_change_state: "accepted",
        },
      ],
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(payload), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;
    const { createPythonClient } = await import("../../app/services/python.server");
    const summary = await createPythonClient().getPlayerSeason("#2PP", "1785714000");
    expect(summary.attackStarsUnknown).toBe(2);
    expect(summary.defenseStarsUnknown).toBe(1);
    expect(summary.dailyEntries).toHaveLength(2);
    const [completeDay, partialDay] = summary.dailyEntries;
    expect(completeDay.state).toBe("Complete");
    expect(completeDay.coverage).toBe("complete");
    expect(completeDay.flags).toEqual([]);
    expect(completeDay.hasAdjustment).toBe(false);
    expect(completeDay.adjustmentTotal).toBeNull();
    expect(partialDay.state).toBe("Partial");
    expect(partialDay.coverage).toBe("partial");
    expect(partialDay.flags).toEqual(["late_data"]);
    expect(partialDay.hasAdjustment).toBe(true);
    expect(partialDay.adjustmentTotal).toBe(-15);
    // Older summaries have no EOD fields: they stay unknown, never 0.
    expect(completeDay).toMatchObject({
      eodState: null,
      eodChange: null,
      eodChangeState: null,
    });
    expect(partialDay).toMatchObject({
      netChange: 10,
      eodState: "provisional",
      eodChange: 1010,
      eodChangeState: "accepted",
    });
  });

  it("keeps official history separate when tracked day detail is unavailable", async () => {
    const payload = officialSeasonPayload();
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(new Response(JSON.stringify(payload), { status: 200 })),
    );
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;
    const { createPythonClient } = await import("../../app/services/python.server");

    const summary = await createPythonClient().getPlayerSeason("#2PP", "1781499600");

    expect(summary.source).toBe("official_league_history");
    expect(summary.attackStars["3"]).toBeNull();
    expect(summary.officialHistory).toEqual({
      observedAt: "2026-08-04T12:05:00+00:00",
      eodTrophies: 5812,
      finalPlacement: 12,
    });
  });

  it("rejects missing star totals, invented official stars, and bad adjustments", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({ ...seasonPayload(), attack_stars_unknown: undefined }),
          { status: 200 },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify(
            officialSeasonPayload({
              attack_stars: { "0": null, "1": null, "2": null, "3": 1 },
            }),
          ),
          { status: 200 },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            ...seasonPayload(),
            daily_entries: [
              { ...seasonPayload().daily_entries[0], adjustment_total: "15" },
            ],
          }),
          { status: 200 },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;
    const { createPythonClient } = await import("../../app/services/python.server");
    await expect(
      createPythonClient().getPlayerSeason("#2PP", "1785714000"),
    ).rejects.toMatchObject({ status: 502, payload: { error: "malformed" } });
    await expect(
      createPythonClient().getPlayerSeason("#2PP", "1785714000"),
    ).rejects.toMatchObject({ status: 502, payload: { error: "malformed" } });
    await expect(
      createPythonClient().getPlayerSeason("#2PP", "1785714000"),
    ).rejects.toMatchObject({ status: 502, payload: { error: "malformed" } });
  });

  it("rejects malformed seasons, oversized days, bad EOD states, and embedded battles", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ seasons: [{}] }), { status: 200 }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            ...seasonPayload(),
            daily_entries: Array.from(
              { length: 29 },
              () => seasonPayload().daily_entries[0],
            ),
          }),
          { status: 200 },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            ...seasonPayload(),
            daily_entries: [{ ...seasonPayload().daily_entries[0], battles: [] }],
          }),
          { status: 200 },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            ...seasonPayload(),
            daily_entries: [{ ...seasonPayload().daily_entries[0], eod_state: "final" }],
          }),
          { status: 200 },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify(
            officialSeasonPayload({ season_end: "2026-06-16T05:00:00+00:00" }),
          ),
          { status: 200 },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            tag: "#2PP",
            seasons: [
              {
                ...seasonsPayload().seasons[0],
                source: "official_league_history",
                days_observed: 0,
                days_missing: 28,
                official_history: officialSeasonPayload().official_history,
              },
            ],
          }),
          { status: 200 },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;
    const { createPythonClient } = await import("../../app/services/python.server");
    await expect(createPythonClient().getPlayerSeasons("#2PP")).rejects.toMatchObject({
      status: 502,
      payload: { error: "malformed" },
    });
    await expect(
      createPythonClient().getPlayerSeason("#2PP", "1785714000"),
    ).rejects.toMatchObject({ status: 502, payload: { error: "malformed" } });
    await expect(
      createPythonClient().getPlayerSeason("#2PP", "1785714000"),
    ).rejects.toMatchObject({ status: 502, payload: { error: "malformed" } });
    await expect(
      createPythonClient().getPlayerSeason("#2PP", "1785714000"),
    ).rejects.toMatchObject({ status: 502, payload: { error: "malformed" } });
    await expect(
      createPythonClient().getPlayerSeason("#2PP", "1785714000"),
    ).rejects.toMatchObject({ status: 502, payload: { error: "malformed" } });
    await expect(createPythonClient().getPlayerSeasons("#2PP")).rejects.toMatchObject({
      status: 502,
      payload: { error: "malformed" },
    });
  });
});
