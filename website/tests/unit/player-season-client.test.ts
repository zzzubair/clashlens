import {
  EMPTY_SUMMARY_FACTS,
  RAW_DAY_FACTS,
  RAW_PLAYER_FACTS,
  RAW_SUMMARY_FACTS,
} from "../fixtures/player-route-data";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const TEST_SECRET = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8";

function seasonPayload() {
  return {
    kind: "player-season-summary",
    ...RAW_SUMMARY_FACTS,
    ...EMPTY_SUMMARY_FACTS,
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
        ...RAW_DAY_FACTS,
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
        day_count: 28,
        season_start: "2026-05-01T05:00:00+00:00",
        season_end: "2026-05-29T05:00:00+00:00",
        last_day_start: "2026-05-28T05:00:00+00:00",
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
          ...RAW_PLAYER_FACTS,
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
                      display_state: "complete",
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
      expect(
        (await (await import("../../app/services/players.server")).getPlayer("#2PP"))
          .trackingState,
      ).toBe(trackingState);
      expect(
        (
          await (
            await import("../../app/services/players.server")
          ).getRefreshStatus(workId, "#2PP")
        ).player?.trackingState,
      ).toBe(trackingState);
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

    const seasons = await (
      await import("../../app/services/players.server")
    ).getPlayerSeasons("#2PP");
    expect(seasons).toEqual([
      {
        seasonId: "1785714000",
        dayCount: 28,
        seasonStart: "2026-05-01T05:00:00+00:00",
        seasonEnd: "2026-05-29T05:00:00+00:00",
        lastDayStart: "2026-05-28T05:00:00+00:00",
        coverageState: "partial",
        daysObserved: 28,
        daysMissing: 0,
        source: "tracked_summary",
        officialHistory: null,
      },
    ]);

    const summary = await (
      await import("../../app/services/players.server")
    ).getPlayerSeason("#2PP", "1785714000");
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
          ...RAW_DAY_FACTS,
          ranked_day_start: "2026-05-02T05:00:00+00:00",
          ranked_day_end: "2026-05-03T05:00:00+00:00",
          state: "Partial",
          coverage: "partial",
          confidence: "partial",
          has_adjustment: true,
          adjustment_total: -15,
          flags: ["late_data"],
          eod_state: "provisional",
          eod_change: 1010,
          eod_change_state: "accepted",
          reset_rank: 42,
        },
      ],
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(payload), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;
    const summary = await (
      await import("../../app/services/players.server")
    ).getPlayerSeason("#2PP", "1785714000");
    expect(summary.attackStarsUnknown).toBe(2);
    expect(summary.defenseStarsUnknown).toBe(1);
    expect(summary.dailyEntries).toHaveLength(2);
    const [completeDay, partialDay] = summary.dailyEntries;
    expect(completeDay.state).toBe("Complete");
    expect(completeDay.coverage).toBe("complete");
    expect(completeDay.confidence).toBe("exact");
    expect(partialDay.confidence).toBe("partial");
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
    // A Reset rank the summary does not send stays unknown.
    expect(completeDay.resetRank).toBeNull();
    expect(partialDay.resetRank).toBe(42);
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

    const summary = await (
      await import("../../app/services/players.server")
    ).getPlayerSeason("#2PP", "1781499600");

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
          JSON.stringify({
            ...seasonPayload(),
            attack_stars_unknown: undefined,
          }),
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
    await expect(
      (await import("../../app/services/players.server")).getPlayerSeason(
        "#2PP",
        "1785714000",
      ),
    ).rejects.toMatchObject({ status: 502, payload: { error: "malformed" } });
    await expect(
      (await import("../../app/services/players.server")).getPlayerSeason(
        "#2PP",
        "1785714000",
      ),
    ).rejects.toMatchObject({ status: 502, payload: { error: "malformed" } });
    await expect(
      (await import("../../app/services/players.server")).getPlayerSeason(
        "#2PP",
        "1785714000",
      ),
    ).rejects.toMatchObject({ status: 502, payload: { error: "malformed" } });
  });

  it("rejects malformed seasons, oversized days, bad EOD states and ranks, and embedded battles", async () => {
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
          JSON.stringify({
            ...seasonPayload(),
            daily_entries: [{ ...seasonPayload().daily_entries[0], reset_rank: 0 }],
          }),
          { status: 200 },
        ),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify(officialSeasonPayload({ season_end: "not a timestamp" })),
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
                season_start: "not a timestamp",
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
    await expect(
      (await import("../../app/services/players.server")).getPlayerSeasons("#2PP"),
    ).rejects.toMatchObject({
      status: 502,
      payload: { error: "malformed" },
    });
    await expect(
      (await import("../../app/services/players.server")).getPlayerSeason(
        "#2PP",
        "1785714000",
      ),
    ).rejects.toMatchObject({ status: 502, payload: { error: "malformed" } });
    await expect(
      (await import("../../app/services/players.server")).getPlayerSeason(
        "#2PP",
        "1785714000",
      ),
    ).rejects.toMatchObject({ status: 502, payload: { error: "malformed" } });
    await expect(
      (await import("../../app/services/players.server")).getPlayerSeason(
        "#2PP",
        "1785714000",
      ),
    ).rejects.toMatchObject({ status: 502, payload: { error: "malformed" } });
    await expect(
      (await import("../../app/services/players.server")).getPlayerSeason(
        "#2PP",
        "1785714000",
      ),
    ).rejects.toMatchObject({ status: 502, payload: { error: "malformed" } });
    await expect(
      (await import("../../app/services/players.server")).getPlayerSeason(
        "#2PP",
        "1785714000",
      ),
    ).rejects.toMatchObject({ status: 502, payload: { error: "malformed" } });
    await expect(
      (await import("../../app/services/players.server")).getPlayerSeasons("#2PP"),
    ).rejects.toMatchObject({
      status: 502,
      payload: { error: "malformed" },
    });
  });
});
