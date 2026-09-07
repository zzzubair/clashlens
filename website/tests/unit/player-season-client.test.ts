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
    days_missing: [],
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
  });

  it("rejects missing unknown-star totals and non-integer adjustment amounts", async () => {
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
  });

  it("rejects malformed seasons, oversized days, and embedded battles", async () => {
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
  });
});
