import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const TEST_SECRET = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8";

function emptyLivePayload() {
  return {
    kind: "live",
    tracked_population: 0,
    total_entries: 0,
    page: 1,
    page_size: 25,
    page_count: 0,
    has_previous: false,
    has_next: false,
    coverage: {
      state: "partial",
      tracked_players: 0,
      measured_percent: 0,
      note: "Empty.",
    },
    provenance: {
      source: "test",
      observed_at: "2026-08-06T12:00:00Z",
      freshness: "fresh",
      confidence: "partial",
      coverage: "partial",
      version: "test-v1",
    },
    quality_states: ["partial"],
    entries: [],
  };
}

describe("server-only Python client response boundary", () => {
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

  it("rejects a kind-correct but structurally malformed leaderboard", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ kind: "tracked-leaderboard", entries: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;

    const { createPythonClient } = await import("../../app/services/python.server");

    await expect(createPythonClient().getTrackedLeaderboard(25)).rejects.toMatchObject({
      status: 502,
      payload: { error: "malformed" },
    });
  });

  it.each([
    ["wrong requested kind", { ...emptyLivePayload(), kind: "frozen" }],
    ["inconsistent pagination", { ...emptyLivePayload(), page_count: 1 }],
  ])("rejects %s", async (_name, payload) => {
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
    const { createPythonClient } = await import("../../app/services/python.server");
    await expect(createPythonClient().getTrackedLeaderboard()).rejects.toMatchObject({
      status: 502,
      payload: { error: "malformed" },
    });
  });

  it.each([
    ["out-of-range Daily selector", { season_day_number: 29 }],
    ["invalid Daily reset timestamp", { reset_at: "not-a-timestamp" }],
    ["non-reset-hour Daily timestamp", { reset_at: "2026-08-06T06:00:00Z" }],
  ])("rejects an %s", async (_name, invalidField) => {
    const payload = {
      ...emptyLivePayload(),
      kind: "frozen",
      reset_at: "2026-08-06T05:00:00Z",
      season_start_at: "2026-07-16T05:00:00Z",
      season_end_at: "2026-08-13T05:00:00Z",
      official_season_id: "2026-08",
      season_day_number: 21,
      previous_snapshot: null,
      next_snapshot: null,
      ...invalidField,
    };
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
    const { createPythonClient } = await import("../../app/services/python.server");
    await expect(
      createPythonClient().getTrackedLeaderboard(25, "daily"),
    ).rejects.toMatchObject({ status: 502, payload: { error: "malformed" } });
  });

  it("does not use an environment secret in production", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    process.env.NODE_ENV = "production";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;

    const { createPythonClient } = await import("../../app/services/python.server");

    await expect(createPythonClient().getTrackedLeaderboard()).rejects.toMatchObject({
      status: 503,
      payload: { error: "unavailable" },
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects an oversized search before contacting the private service", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;

    const { createPythonClient } = await import("../../app/services/python.server");

    await expect(
      createPythonClient().searchPlayers("x".repeat(81)),
    ).rejects.toMatchObject({
      status: 400,
      payload: { error: "invalid_input" },
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([0, 1.5, 51])(
    "rejects invalid search limit %s before contacting the private service",
    async (limit) => {
      const fetchMock = vi.fn();
      vi.stubGlobal("fetch", fetchMock);
      process.env.NODE_ENV = "test";
      process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;

      const { createPythonClient } = await import("../../app/services/python.server");

      await expect(
        createPythonClient().searchPlayers("Nova", limit),
      ).rejects.toMatchObject({
        status: 400,
        payload: { error: "invalid_input" },
      });
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );

  it("rejects refresh status from a different player", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          refresh_id: "7c4d7f3a-4ff1-4fdd-889e-2284debc622d",
          tag: "#2PQ",
          status: "pending",
          outcome: "created",
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;

    const { createPythonClient } = await import("../../app/services/python.server");

    await expect(
      createPythonClient().getRefreshStatus("work-1", "#2PP"),
    ).rejects.toMatchObject({
      status: 409,
      payload: { error: "conflict" },
    });
  });

  it.each([
    ["leased", "running", 50],
    ["waiting_retry", "queued", 0],
  ] as const)(
    "maps collector %s refresh status to %s",
    async (status, state, progressPercent) => {
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(
          new Response(
            JSON.stringify({
              refresh_id: "7c4d7f3a-4ff1-4fdd-889e-2284debc622d",
              tag: "#2PP",
              status,
              outcome: "created",
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          ),
        ),
      );
      process.env.NODE_ENV = "test";
      process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;
      const { createPythonClient } = await import("../../app/services/python.server");

      await expect(
        createPythonClient().getRefreshStatus(
          "7c4d7f3a-4ff1-4fdd-889e-2284debc622d",
          "#2PP",
        ),
      ).resolves.toMatchObject({ state, progressPercent });
    },
  );

  it("rejects impossible collector running refresh status", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            refresh_id: "7c4d7f3a-4ff1-4fdd-889e-2284debc622d",
            tag: "#2PP",
            status: "running",
            outcome: "created",
          }),
          { status: 200, headers: { "content-type": "application/json" } },
        ),
      ),
    );
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;
    const { createPythonClient } = await import("../../app/services/python.server");

    await expect(
      createPythonClient().getRefreshStatus(
        "7c4d7f3a-4ff1-4fdd-889e-2284debc622d",
        "#2PP",
      ),
    ).rejects.toMatchObject({ status: 502, payload: { error: "malformed" } });
  });

  it("binds refresh idempotency to the signed request ID and sends no body", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          refresh_id: "7c4d7f3a-4ff1-4fdd-889e-2284debc622d",
          tag: "#2PP",
          status: "pending",
          outcome: "created",
        }),
        { status: 202, headers: { "content-type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;
    const idempotencyKey = "3be934b5-68fa-4741-8c7b-e03592e4ad70";

    const { createPythonClient } = await import("../../app/services/python.server");

    await createPythonClient().requestRefresh("#2PP", idempotencyKey);

    expect(fetchMock).toHaveBeenCalledWith(
      new URL("/v1/players/%232PP/refresh", "http://python-fixture.test/"),
      expect.objectContaining({ method: "POST", body: undefined }),
    );
    const request = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(new Headers(request.headers).get("X-ClashLens-Request-Id")).toBe(
      idempotencyKey,
    );
  });

  it("uses the live Python leaderboard endpoint", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ kind: "tracked-leaderboard", entries: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;
    const { createPythonClient } = await import("../../app/services/python.server");

    await expect(createPythonClient().getTrackedLeaderboard()).rejects.toMatchObject({
      status: 502,
    });

    expect(fetchMock.mock.calls[0]?.[0]).toEqual(
      new URL("/v1/leaderboards/live?limit=25&offset=0", "http://python-fixture.test/"),
    );
  });

  it("maps the exact live Python leaderboard payload", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            kind: "live",
            ordering_rule_version: "tracked-trophies-md5-v1",
            generated_at: "2026-08-06T12:00:00+00:00",
            tracked_population: 2,
            total_entries: 1,
            page: 1,
            page_size: 25,
            page_count: 1,
            has_previous: false,
            has_next: false,
            coverage: {
              state: "partial",
              tracked_players: 2,
              measured_percent: 50,
              note: "Tracked cohort.",
            },
            provenance: {
              source: "current accepted profiles",
              observed_at: "2026-08-06T12:00:00+00:00",
              freshness: "fresh",
              confidence: "partial",
              coverage: "partial",
              version: "tracked-trophies-md5-v1",
            },
            quality_states: ["partial"],
            entries: [
              {
                position: 1,
                tag: "#2PP",
                name: "Nova",
                clan: "Northwind",
                trophies: 7211,
                observed_at: "2026-08-06T11:59:00+00:00",
                age_seconds: 60,
                freshness: "fresh",
                confidence: "eligible",
                public_confidence: "high",
                official_rank: null,
              },
            ],
          }),
          { status: 200 },
        ),
      ),
    );
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;
    const { createPythonClient } = await import("../../app/services/python.server");
    await expect(createPythonClient().getTrackedLeaderboard()).resolves.toMatchObject({
      view: "live",
      coverage: { measuredPercent: 50 },
      provenance: { version: "tracked-trophies-md5-v1" },
    });
  });

  it("maps the exact frozen Python leaderboard payload", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            kind: "frozen",
            snapshot_id: "frozen-1",
            boundary_at: "2026-08-06T05:00:00+00:00",
            generated_at: "2026-08-06T05:00:00+00:00",
            version: 7,
            ordering_rule_version: "snapshot-v1",
            tracked_population: 3,
            total_entries: 1,
            page: 1,
            page_size: 25,
            page_count: 1,
            has_previous: false,
            has_next: false,
            reset_at: "2026-08-06T05:00:00+00:00",
            season_start_at: "2026-07-16T05:00:00+00:00",
            season_end_at: "2026-08-13T05:00:00+00:00",
            official_season_id: "2026-08",
            season_day_number: 21,
            previous_snapshot: null,
            next_snapshot: null,
            coverage: {
              state: "partial",
              tracked_players: 3,
              measured_percent: 66.67,
              note: "One accepted profile was excluded.",
            },
            provenance: {
              source: "published frozen snapshot",
              observed_at: "2026-08-06T05:00:00+00:00",
              freshness: "stale",
              confidence: "partial",
              coverage: "partial",
              version: "snapshot-v1",
            },
            quality_states: ["partial", "stale"],
            entries: [
              {
                position: 1,
                tag: "#2PP",
                name: null,
                clan: null,
                trophies: 7211,
                observed_at: "2026-08-06T04:00:00+00:00",
                age_seconds: 3600,
                freshness: "stale",
                confidence: "exact",
                public_confidence: "partial",
                official_rank: 1,
              },
            ],
          }),
          { status: 200 },
        ),
      ),
    );
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;
    const { createPythonClient } = await import("../../app/services/python.server");
    const leaderboard = await createPythonClient().getTrackedLeaderboard(25, "daily", 0, {
      officialSeasonId: "2026-08",
      dayNumber: 21,
    });
    expect(leaderboard).toMatchObject({
      view: "daily",
      entries: [{ name: "Unknown", clan: "Unknown" }],
      page: 1,
      pageSize: 25,
      daily: {
        officialSeasonId: "2026-08",
        dayNumber: 21,
        seasonStartAt: "2026-07-16T05:00:00+00:00",
        seasonEndAt: "2026-08-13T05:00:00+00:00",
      },
      coverage: { measuredPercent: 66.67 },
    });
    expect(leaderboard.entries[0]).not.toHaveProperty("officialRank");
    expect(vi.mocked(fetch).mock.calls[0]?.[0]).toEqual(
      new URL(
        "/v1/leaderboards/frozen?limit=25&offset=0&official_season_id=2026-08&season_day_number=21",
        "http://python-fixture.test/",
      ),
    );
  });

  it("maps the exact Python search payload and preserves uncertain confidence", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            query: "Nova",
            known_only: true,
            results: [
              {
                tag: "#2PP",
                name: "Nova",
                clan: null,
                trophies: 7211,
                observed_at: "2026-08-06T11:59:00+00:00",
                age_seconds: 60,
                freshness: "fresh",
                public_confidence: "uncertain",
              },
            ],
          }),
          { status: 200 },
        ),
      ),
    );
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;
    const { createPythonClient } = await import("../../app/services/python.server");
    await expect(createPythonClient().searchPlayers("Nova")).resolves.toMatchObject({
      exactTag: null,
      results: [
        { freshness: { observedAt: "2026-08-06T11:59:00+00:00" }, state: "uncertain" },
      ],
    });
  });

  it("derives an exact tag from the submitted query, not the Python payload", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ query: "#2pp", known_only: true, results: [] }), {
          status: 200,
        }),
      ),
    );
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;
    const { createPythonClient } = await import("../../app/services/python.server");

    await expect(createPythonClient().searchPlayers("#2pp")).resolves.toMatchObject({
      exactTag: "#2PP",
      knownOnly: true,
    });
  });

  it("maps exact full and profile-only Python player payloads", async () => {
    const offenseEvents = [
      {
        battle_id: "attack-8",
        battle_timestamp: "2026-08-06T16:30:00Z",
        opponent: { tag: "#2P8", name: "Opponent eight" },
        destruction_percentage: 100,
        stars: 3,
        trophy_change: 40,
      },
      {
        battle_id: "attack-7",
        battle_timestamp: "2026-08-06T15:45:00Z",
        opponent: { tag: "#2PY", name: "Opponent seven" },
        destruction_percentage: 99,
        stars: 3,
        trophy_change: 38,
      },
      {
        battle_id: "attack-6",
        battle_timestamp: "2026-08-06T14:30:00Z",
        opponent: { tag: "#2PL", name: null },
        destruction_percentage: 75,
        stars: 2,
        trophy_change: 30,
      },
      {
        battle_id: "attack-5",
        battle_timestamp: "2026-08-06T13:15:00Z",
        opponent: { tag: "#2PG", name: "Opponent five" },
        destruction_percentage: 50,
        stars: 2,
        trophy_change: 25,
      },
      {
        battle_id: "attack-4",
        battle_timestamp: "2026-08-06T12:30:00Z",
        opponent: { tag: "#2PR", name: "Opponent four" },
        destruction_percentage: 33,
        stars: 1,
        trophy_change: 16,
      },
      {
        battle_id: "attack-3",
        battle_timestamp: "2026-08-06T11:30:00Z",
        opponent: { tag: "#2PJ", name: "Opponent three" },
        destruction_percentage: 10,
        stars: 1,
        trophy_change: 5,
      },
      {
        battle_id: "attack-2",
        battle_timestamp: "2026-08-06T10:00:00Z",
        opponent: { tag: "#2PC", name: "Opponent two" },
        destruction_percentage: 9,
        stars: 0,
        trophy_change: 0,
      },
      {
        battle_id: "attack-1",
        battle_timestamp: "2026-08-06T09:00:00Z",
        opponent: { tag: "#2PU", name: "Opponent one" },
        destruction_percentage: 0,
        stars: 0,
        trophy_change: 0,
      },
    ];
    const defenseEvents = [
      {
        battle_id: "defense-3",
        battle_timestamp: "2026-08-06T16:00:00Z",
        opponent: { tag: "#2PV", name: "Defender three" },
        destruction_percentage: 100,
        stars: 3,
        trophy_change: -40,
      },
      {
        battle_id: "defense-2",
        battle_timestamp: "2026-08-06T14:00:00Z",
        opponent: { tag: "#2P9", name: null },
        destruction_percentage: 50,
        stars: 1,
        trophy_change: -5,
      },
      {
        battle_id: "defense-1",
        battle_timestamp: "2026-08-06T12:00:00Z",
        opponent: { tag: "#28PP", name: "Defender one" },
        destruction_percentage: 0,
        stars: 0,
        trophy_change: 0,
      },
    ];
    const currentDay = {
      ranked_day_start: "2026-08-06T05:00:00+00:00",
      ranked_day_end: "2026-08-07T05:00:00+00:00",
      official_season_id: "2026-08",
      season_day_number: 3,
      version: 9,
      state: "Live",
      confidence: "partial",
      completeness: { state: "partial", reason: "Open day." },
      public_confidence: "partial",
      uncertainty_reasons: ["Open day."],
      attack_count: null,
      attack_three_star_count: null,
      attack_gain: null,
      defense_count: null,
      defense_three_star_count: null,
      defense_loss: null,
      net_trophy_change: null,
      offense_events: offenseEvents,
      defense_events: defenseEvents,
    };
    const previousDay = {
      ...currentDay,
      ranked_day_start: "2026-08-05T05:00:00+00:00",
      ranked_day_end: "2026-08-06T05:00:00+00:00",
      season_day_number: 2,
      offense_events: [],
      defense_events: [],
    };
    const full = {
      tag: "#2PP",
      name: "Nova",
      clan: null,
      trophies: 7211,
      observed_at: "2026-08-06T11:59:00+00:00",
      age_seconds: 60,
      freshness: "fresh",
      public_confidence: "high",
      eligibility: "eligible",
      screen_ready: {
        current_day: currentDay,
        recent_days: [previousDay],
        season_days: [currentDay, previousDay],
        season: {
          id: "2026-08",
          start: "2026-08-04T05:00:00+00:00",
          end: "2026-09-01T05:00:00+00:00",
          current_day_number: 3,
        },
        data_quality: [{ code: "partial", label: "Partial", detail: "Open day." }],
        provenance: {
          source: "api_player_daily_logs",
          observed_at: "2026-08-06T11:59:00+00:00",
          freshness: "fresh",
          confidence: "partial",
          coverage: "partial",
          version: "api-player-daily-log-v3",
        },
      },
    };
    const profileOnly = {
      ...full,
      screen_ready: {
        ...full.screen_ready,
        current_day: null,
        season: null,
        recent_days: [],
        season_days: [],
        data_quality: [
          {
            code: "unavailable",
            label: "Missing ranked-day data",
            detail: "No ranked-day publication exists.",
          },
        ],
      },
    };
    const malformedEvents = {
      ...full,
      screen_ready: {
        ...full.screen_ready,
        current_day: {
          ...currentDay,
          offense_events: [{ ...offenseEvents[0], trophy_change: -1 }],
        },
        recent_days: [],
        season_days: [currentDay],
      },
    };
    const tooManyEvents = {
      ...full,
      screen_ready: {
        ...full.screen_ready,
        current_day: {
          ...currentDay,
          offense_events: Array.from({ length: 9 }, (_, index) => ({
            ...offenseEvents[0],
            battle_id: `extra-attack-${index}`,
          })),
        },
        recent_days: [],
        season_days: [],
      },
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(full), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(profileOnly), { status: 200 }))
      .mockResolvedValueOnce(
        new Response(JSON.stringify(malformedEvents), { status: 200 }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify(tooManyEvents), { status: 200 }),
      );
    vi.stubGlobal("fetch", fetchMock);
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;
    const { createPythonClient } = await import("../../app/services/python.server");
    const mapped = await createPythonClient().getPlayer("#2PP");
    expect(mapped.season).toMatchObject({
      anchor: "2026-08-04T05:00:00+00:00",
      dayCount: 28,
    });
    expect(mapped.seasonDays.map((day) => day.dayNumber)).toEqual([3, 2]);
    expect(mapped.recentDays.map((day) => day.dayNumber)).toEqual([2]);
    const mappedCurrentDay = mapped.currentDay;
    if (mappedCurrentDay === null) throw new Error("expected active day");
    const mapExpectedEvent = (event: (typeof offenseEvents)[number]) => ({
      battleId: event.battle_id,
      battleTimestamp: event.battle_timestamp,
      opponent: event.opponent,
      destructionPercentage: event.destruction_percentage,
      stars: event.stars,
      trophyChange: event.trophy_change,
    });
    expect(mappedCurrentDay.offenseEvents).toEqual(offenseEvents.map(mapExpectedEvent));
    expect(mappedCurrentDay.defenseEvents).toEqual(defenseEvents.map(mapExpectedEvent));
    await expect(createPythonClient().getPlayer("#2PP")).resolves.toMatchObject({
      season: null,
      currentDay: null,
      seasonDays: [],
      dataQuality: [{ code: "unavailable" }],
    });
    await expect(createPythonClient().getPlayer("#2PP")).rejects.toMatchObject({
      status: 502,
      payload: { error: "malformed" },
    });
    await expect(createPythonClient().getPlayer("#2PP")).rejects.toMatchObject({
      status: 502,
      payload: { error: "malformed" },
    });
  });

  it("rejects a non-canonical refresh UUID before signing or fetch", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;
    const { createPythonClient } = await import("../../app/services/python.server");
    await expect(
      createPythonClient().requestRefresh("#2PP", "not-a-uuid"),
    ).rejects.toMatchObject({ status: 400, payload: { error: "invalid_input" } });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
