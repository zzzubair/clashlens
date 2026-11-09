import { CALENDAR, RAW_DAY_FACTS, RAW_PLAYER_FACTS } from "../fixtures/player-route-data";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const TEST_SECRET = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8";

function emptyLivePayload() {
  return {
    kind: "live",
    focus_neighbors: 5,
    calendar: CALENDAR,
    generated_at: "2026-08-06T12:00:00Z",
    source_observations: {
      oldest_observed_at: null,
      newest_observed_at: null,
      stale_count: 0,
    },
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

    await expect(
      (await import("../../app/services/leaderboard.server")).getTrackedLeaderboard(25),
    ).rejects.toMatchObject({
      status: 502,
      payload: { error: "malformed" },
    });
  });

  it("maps an empty live leaderboard without source observations", async () => {
    const payload = {
      ...emptyLivePayload(),
      provenance: { ...emptyLivePayload().provenance, observed_at: null },
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
    await expect(
      (await import("../../app/services/leaderboard.server")).getTrackedLeaderboard(),
    ).resolves.toMatchObject({
      entries: [],
      provenance: { observedAt: null },
      sourceObservations: {
        oldestObservedAt: null,
        newestObservedAt: null,
        staleCount: 0,
      },
    });
  });

  it.each([
    ["wrong requested kind", { ...emptyLivePayload(), kind: "frozen" }],
    ["inconsistent pagination", { ...emptyLivePayload(), page_count: 1 }],
    [
      "null provenance with source observations",
      {
        ...emptyLivePayload(),
        source_observations: {
          oldest_observed_at: "2026-08-06T11:59:00Z",
          newest_observed_at: "2026-08-06T12:00:00Z",
          stale_count: 0,
        },
        provenance: { ...emptyLivePayload().provenance, observed_at: null },
      },
    ],
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
    await expect(
      (await import("../../app/services/leaderboard.server")).getTrackedLeaderboard(),
    ).rejects.toMatchObject({
      status: 502,
      payload: { error: "malformed" },
    });
  });

  it("rejects a 7-day list that is not dated Legend days with whole numbers or nulls", async () => {
    const payload = (recent: unknown) => ({
      ...emptyLivePayload(),
      tracked_population: 1,
      total_entries: 1,
      page_count: 1,
      source_observations: {
        oldest_observed_at: "2026-08-06T12:00:00Z",
        newest_observed_at: "2026-08-06T12:00:00Z",
        stale_count: 0,
      },
      entries: [
        {
          position: 1,
          tag: "#2PP",
          name: "Nova",
          clan: null,
          trophies: 5100,
          observed_at: "2026-08-06T12:00:00Z",
          age_seconds: 0,
          freshness: "fresh",
          confidence: "eligible",
          public_confidence: "high",
          official_rank: null,
          recent_days: recent,
        },
      ],
    });
    const day = (start: string, change: unknown) => ({
      ...RAW_DAY_FACTS,
      ranked_day_start: start,
      net_trophy_change: change,
    });
    let recent: unknown = [
      day("2026-08-04T05:00:00+00:00", 40),
      day("2026-08-05T05:00:00Z", null),
    ];
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockImplementation(
          async () => new Response(JSON.stringify(payload(recent)), { status: 200 }),
        ),
    );
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;
    await expect(
      (await import("../../app/services/leaderboard.server")).getTrackedLeaderboard(),
    ).resolves.toMatchObject({
      entries: [
        {
          recentDays: [
            { start: "2026-08-04T05:00:00+00:00", trophyChange: 40 },
            { start: "2026-08-05T05:00:00Z", trophyChange: null },
          ],
        },
      ],
    });
    // A bare number, a fraction, a day with no start or a malformed timestamp,
    // and more than 7 days.
    for (recent of [
      [40],
      [day("2026-08-04T05:00:00Z", 40.5)],
      [{ net_trophy_change: 40 }],
      [day("not-a-timestamp", 40)],
      Array(8).fill(day("2026-08-04T05:00:00Z", 40)),
    ])
      await expect(
        (await import("../../app/services/leaderboard.server")).getTrackedLeaderboard(),
      ).rejects.toMatchObject({
        status: 502,
        payload: { error: "malformed" },
      });
  });

  it.each([
    ["nonpositive Daily selector", { season_day_number: 0 }],
    ["invalid Daily reset timestamp", { reset_at: "not-a-timestamp" }],
    ["non-UTC Daily timestamp", { reset_at: "2026-08-06T06:00:00+01:00" }],
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
    await expect(
      (await import("../../app/services/leaderboard.server")).getTrackedLeaderboard(
        25,
        "daily",
      ),
    ).rejects.toMatchObject({ status: 502, payload: { error: "malformed" } });
  });

  it("does not use an environment secret in production", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    process.env.NODE_ENV = "production";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;

    await expect(
      (await import("../../app/services/leaderboard.server")).getTrackedLeaderboard(),
    ).rejects.toMatchObject({
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

    await expect(
      (await import("../../app/services/players.server")).searchPlayers("x".repeat(81)),
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

      await expect(
        (await import("../../app/services/players.server")).searchPlayers("Nova", limit),
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
          display_state: "queued",
          outcome: "created",
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;

    await expect(
      (await import("../../app/services/players.server")).getRefreshStatus(
        "work-1",
        "#2PP",
      ),
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
              display_state: state,
              outcome: "created",
            }),
            { status: 200, headers: { "content-type": "application/json" } },
          ),
        ),
      );
      process.env.NODE_ENV = "test";
      process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;

      await expect(
        (await import("../../app/services/players.server")).getRefreshStatus(
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

    await expect(
      (await import("../../app/services/players.server")).getRefreshStatus(
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
          display_state: "queued",
          outcome: "created",
        }),
        { status: 202, headers: { "content-type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;
    const idempotencyKey = "3be934b5-68fa-4741-8c7b-e03592e4ad70";

    await (
      await import("../../app/services/players.server")
    ).requestRefresh("#2PP", idempotencyKey);

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

    await expect(
      (await import("../../app/services/leaderboard.server")).getTrackedLeaderboard(),
    ).rejects.toMatchObject({
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
            focus_neighbors: 5,
            calendar: CALENDAR,
            ordering_rule_version: "tracked-trophies-md5-v1",
            generated_at: "2026-08-06T12:00:00+00:00",
            source_observations: {
              oldest_observed_at: "2026-08-06T11:59:00+00:00",
              newest_observed_at: "2026-08-06T12:00:00+00:00",
              stale_count: 0,
            },
            tracked_population: 2,
            total_entries: 2,
            page: 1,
            page_size: 25,
            page_count: 1,
            has_previous: false,
            has_next: false,
            coverage: {
              state: "partial",
              tracked_players: 2,
              measured_percent: 100,
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
                reset_position: 2,
                today_change: 61,
                recent_days: [
                  {
                    ...RAW_DAY_FACTS,
                    ranked_day_start: "2026-08-04T05:00:00+00:00",
                    net_trophy_change: 130,
                  },
                  {
                    ...RAW_DAY_FACTS,
                    ranked_day_start: "2026-08-05T05:00:00+00:00",
                    net_trophy_change: null,
                  },
                ],
              },
              {
                // An older API sends no place at the last Reset.
                position: 2,
                tag: "#8PP",
                name: "Ember",
                clan: null,
                trophies: 7100,
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
    await expect(
      (await import("../../app/services/leaderboard.server")).getTrackedLeaderboard(),
    ).resolves.toMatchObject({
      view: "live",
      entries: [
        {
          tag: "#2PP",
          rank: 1,
          resetRank: 2,
          trophies: 7211,
          todayChange: 61,
          recentDays: [
            { start: "2026-08-04T05:00:00+00:00", trophyChange: 130 },
            { start: "2026-08-05T05:00:00+00:00", trophyChange: null },
          ],
        },
        {
          tag: "#8PP",
          rank: 2,
          resetRank: null,
          trophies: 7100,
          todayChange: null,
          recentDays: [],
        },
      ],
      generatedAt: "2026-08-06T12:00:00+00:00",
      coverage: { measuredPercent: 100 },
      provenance: {
        version: "tracked-trophies-md5-v1",
        observedAt: "2026-08-06T12:00:00+00:00",
      },
      sourceObservations: {
        oldestObservedAt: "2026-08-06T11:59:00+00:00",
        newestObservedAt: "2026-08-06T12:00:00+00:00",
        staleCount: 0,
      },
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
                tied: true,
              },
            ],
          }),
          { status: 200 },
        ),
      ),
    );
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;
    const leaderboard = await (
      await import("../../app/services/leaderboard.server")
    ).getTrackedLeaderboard(25, "daily", 0, {
      officialSeasonId: "2026-08",
      dayNumber: 21,
    });
    expect(leaderboard).toMatchObject({
      view: "daily",
      entries: [
        {
          name: "Unknown",
          clan: "Unknown",
          tied: true,
          resetRank: null,
          todayChange: null,
          recentDays: [],
        },
      ],
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
            users: [],
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
    await expect(
      (await import("../../app/services/players.server")).searchPlayers("Nova"),
    ).resolves.toMatchObject({
      exactTag: null,
      results: [
        { freshness: { observedAt: "2026-08-06T11:59:00+00:00" }, state: "uncertain" },
      ],
    });
  });

  it("derives an exact tag from the submitted query, not the Python payload", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response(
            JSON.stringify({ query: "#2pp", known_only: true, results: [], users: [] }),
            { status: 200 },
          ),
        ),
    );
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;

    await expect(
      (await import("../../app/services/players.server")).searchPlayers("#2pp"),
    ).resolves.toMatchObject({
      exactTag: "#2PP",
      knownOnly: true,
    });
  });

  it("maps exact player payloads and rejects malformed player provenance", async () => {
    const offenseEvents = [
      {
        battle_id: "attack-8",
        battle_timestamp: "2026-08-06T16:30:00Z",
        opponent: { tag: "#2P8", name: "Opponent eight" },
        destruction_percentage: 100,
        stars: 3,
        trophy_change: 40,
        army_share_code: "u1x0-2x1",
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
        opponent: { tag: "#2P9", name: "" },
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
      ...RAW_DAY_FACTS,
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
      ...RAW_DAY_FACTS,
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
        ...RAW_PLAYER_FACTS,
        days: [currentDay, previousDay],
        current_day_start: currentDay.ranked_day_start,
        current_day_published_at: "2026-08-06T11:30:00+00:00",
        recent_day_starts: [previousDay.ranked_day_start],
        season_day_starts: [currentDay.ranked_day_start, previousDay.ranked_day_start],
        season: {
          id: "2026-08",
          start: "2026-08-04T05:00:00+00:00",
          end: "2026-09-01T05:00:00+00:00",
          day_count: 28,
          initial_trophies: 5000,
          current_day_number: 3,
          anchor_source: "daily_publication",
          anchor_observed_at: "2026-08-06T11:58:00+00:00",
        },
        data_quality: [{ code: "partial", label: "Partial", detail: "Open day." }],
        provenance: {
          source: "api_player_daily_logs",
          observed_at: "2026-08-06T11:59:00+00:00",
          freshness: "fresh",
          confidence: "partial",
          coverage: "partial",
          version: "api-player-daily-log-v4",
        },
      },
    };
    const profileOnly = {
      ...full,
      public_confidence: "uncertain",
      profile_not_found_at: "2026-08-06T12:30:00+00:00",
      screen_ready: {
        ...RAW_PLAYER_FACTS,
        ...full.screen_ready,
        days: [],
        current_day_start: null,
        current_day_published_at: null,
        season: null,
        recent_day_starts: [],
        season_day_starts: [],
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
        ...RAW_PLAYER_FACTS,
        ...full.screen_ready,
        days: [
          {
            ...currentDay,
            offense_events: [{ ...offenseEvents[0], trophy_change: -1 }],
          },
          previousDay,
        ],
      },
    };
    // Every day the lists name must be one of the days sent.
    const unsentDay = {
      ...full,
      screen_ready: {
        ...RAW_PLAYER_FACTS,
        ...full.screen_ready,
        days: [currentDay],
      },
    };
    // Audit example #J9Y9J80L: the game returned nine real attacks for one day.
    const nineAttacks = {
      ...currentDay,
      uncertainty_reasons: ["attack_count_exceeds_eight"],
      offense_events: Array.from({ length: 9 }, (_, index) => ({
        ...offenseEvents[0],
        battle_id: `extra-attack-${index}`,
      })),
    };
    const tooManyEvents = {
      ...full,
      screen_ready: {
        ...RAW_PLAYER_FACTS,
        ...full.screen_ready,
        days: [nineAttacks, previousDay],
        season_day_starts: [nineAttacks.ranked_day_start],
      },
    };
    const nullProvenance = {
      ...full,
      screen_ready: {
        ...RAW_PLAYER_FACTS,
        ...full.screen_ready,
        provenance: { ...full.screen_ready.provenance, observed_at: null },
      },
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(full), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(profileOnly), { status: 200 }))
      .mockResolvedValueOnce(
        new Response(JSON.stringify(malformedEvents), { status: 200 }),
      )
      .mockResolvedValueOnce(new Response(JSON.stringify(unsentDay), { status: 200 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(tooManyEvents), { status: 200 }))
      .mockResolvedValueOnce(
        new Response(JSON.stringify(nullProvenance), { status: 200 }),
      );
    vi.stubGlobal("fetch", fetchMock);
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;
    const mapped = await (
      await import("../../app/services/players.server")
    ).getPlayer("#2PP");
    expect(mapped.season).toMatchObject({
      anchor: "2026-08-04T05:00:00+00:00",
      dayCount: 28,
      end: "2026-09-01T05:00:00+00:00",
      initialTrophies: 5000,
      anchorSource: "daily_publication",
      anchorObservedAt: "2026-08-06T11:58:00+00:00",
    });
    expect(mapped.seasonDays.map((day) => day.dayNumber)).toEqual([3, 2]);
    expect(mapped.recentDays.map((day) => day.dayNumber)).toEqual([2]);
    expect(mapped.profile.notFoundAt).toBeNull();
    expect(mapped.currentDayPublishedAt).toBe("2026-08-06T11:30:00+00:00");
    const mappedCurrentDay = mapped.currentDay;
    if (mappedCurrentDay === null) throw new Error("expected active day");
    const mapExpectedEvent = (event: (typeof offenseEvents)[number]) => ({
      battleId: event.battle_id,
      battleTimestamp: event.battle_timestamp,
      opponent: { ...event.opponent, name: event.opponent.name || null },
      destructionPercentage: event.destruction_percentage,
      stars: event.stars,
      trophyChange: event.trophy_change,
      perspectiveDisagreement: false,
      armyShareCode: event.army_share_code ?? null,
    });
    expect(mappedCurrentDay.offenseEvents).toEqual(offenseEvents.map(mapExpectedEvent));
    expect(mappedCurrentDay.defenseEvents).toEqual(defenseEvents.map(mapExpectedEvent));
    await expect(
      (await import("../../app/services/players.server")).getPlayer("#2PP"),
    ).resolves.toMatchObject({
      season: null,
      currentDay: null,
      currentDayPublishedAt: null,
      seasonDays: [],
      dataQuality: [{ code: "unavailable" }],
      profile: { notFoundAt: "2026-08-06T12:30:00+00:00", confidence: "uncertain" },
    });
    await expect(
      (await import("../../app/services/players.server")).getPlayer("#2PP"),
    ).rejects.toMatchObject({
      status: 502,
      payload: { error: "malformed" },
    });
    await expect(
      (await import("../../app/services/players.server")).getPlayer("#2PP"),
    ).rejects.toMatchObject({
      status: 502,
      payload: { error: "malformed" },
    });
    const [excessDay] = (
      await (await import("../../app/services/players.server")).getPlayer("#2PP")
    ).seasonDays;
    expect(excessDay.offenseEvents).toHaveLength(9);
    expect(excessDay.uncertainty).toEqual(["attack_count_exceeds_eight"]);
    // Shared/player provenance cannot use the live leaderboard's empty-source exception.
    await expect(
      (await import("../../app/services/players.server")).getPlayer("#2PP"),
    ).rejects.toMatchObject({
      status: 502,
      payload: { error: "malformed" },
    });
  });

  it("rejects a non-canonical refresh UUID before signing or fetch", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;
    await expect(
      (await import("../../app/services/players.server")).requestRefresh(
        "#2PP",
        "not-a-uuid",
      ),
    ).rejects.toMatchObject({
      status: 400,
      payload: { error: "invalid_input" },
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("keeps a disagreement battle on its player event", async () => {
    const day = {
      ...RAW_DAY_FACTS,
      ranked_day_start: "2026-08-06T05:00:00+00:00",
      ranked_day_end: null,
      official_season_id: null,
      season_day_number: null,
      version: 1,
      state: "Complete",
      confidence: null,
      completeness: { state: "complete", reason: "Complete." },
      public_confidence: "high",
      uncertainty_reasons: [],
      attack_count: 1,
      attack_three_star_count: 0,
      attack_gain: 0,
      defense_count: null,
      defense_three_star_count: null,
      defense_loss: null,
      net_trophy_change: null,
      offense_events: [
        {
          battle_id: "battle-1",
          battle_timestamp: "2026-08-06T06:00:00Z",
          opponent: { tag: "#8PP", name: "Opp" },
          destruction_percentage: 100,
          stars: 3,
          trophy_change: 35,
          perspective_disagreement: true,
        },
        {
          battle_id: "battle-2",
          battle_timestamp: "2026-08-06T07:00:00Z",
          opponent: { tag: "#8PY", name: "Calm" },
          destruction_percentage: 50,
          stars: 1,
          trophy_change: 8,
        },
      ],
      defense_events: [],
    };
    const payload = {
      tag: "#2PP",
      name: "Nova",
      clan: null,
      trophies: 6000,
      observed_at: "2026-08-06T11:59:00+00:00",
      age_seconds: 60,
      freshness: "fresh",
      public_confidence: "high",
      eligibility: "eligible",
      screen_ready: {
        ...RAW_PLAYER_FACTS,
        days: [day],
        current_day_start: day.ranked_day_start,
        recent_day_starts: [],
        season_day_starts: [day.ranked_day_start],
        season: null,
        data_quality: [],
        provenance: {
          source: "api_player_daily_logs",
          observed_at: "2026-08-06T11:59:00+00:00",
          freshness: "fresh",
          confidence: "high",
          coverage: "complete",
          version: "api-player-daily-log-v4",
        },
      },
    };
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValueOnce(
        new Response(JSON.stringify(payload), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      ),
    );
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;
    const mapped = await (
      await import("../../app/services/players.server")
    ).getPlayer("#2PP");
    const event = mapped.currentDay?.offenseEvents[0];
    if (!event) throw new Error("expected an offense event");
    // Disagreement battles stay visible on their row instead of being dropped.
    expect(event.perspectiveDisagreement).toBe(true);
    expect(mapped.currentDay?.offenseEvents[1]?.perspectiveDisagreement).toBe(false);
  });
});

describe("Python's refresh display state", () => {
  const environment = {
    NODE_ENV: process.env.NODE_ENV,
    CLASHLENS_PYTHON_API_URL: process.env.CLASHLENS_PYTHON_API_URL,
    CLASHLENS_PYTHON_HMAC_SECRET_B64: process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64,
  };
  afterEach(() => {
    vi.unstubAllGlobals();
    for (const [key, value] of Object.entries(environment)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  });
  it.each([
    ["pending", "queued"],
    ["leased", "running"],
    ["waiting_retry", "queued"],
    ["complete", "complete"],
    ["failed", "failed"],
    ["cancelled", "failed"],
  ])("reads %s as the supplied %s", async (status, state) => {
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_API_URL = "http://python-fixture.test/";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;
    const key = "7c4d7f3a-4ff1-4fdd-889e-2284debc622d";
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        Response.json(
          {
            refresh_id: key,
            tag: "#2PP",
            status,
            display_state: state,
            outcome: "saved",
          },
          { status: 202 },
        ),
      ),
    );
    const result = await (
      await import("../../app/services/players.server")
    ).requestRefresh("#2PP", key);
    expect(result.state).toBe(state);
    vi.unstubAllGlobals();
  });
  it.each([undefined, "invented"])(
    "rejects missing or unknown display state %s",
    async (state) => {
      process.env.NODE_ENV = "test";
      process.env.CLASHLENS_PYTHON_API_URL = "http://python-fixture.test/";
      process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;
      const key = "7c4d7f3a-4ff1-4fdd-889e-2284debc622d";
      vi.stubGlobal(
        "fetch",
        vi.fn().mockResolvedValue(
          Response.json(
            {
              refresh_id: key,
              tag: "#2PP",
              status: "pending",
              display_state: state,
              outcome: "saved",
            },
            { status: 202 },
          ),
        ),
      );
      await expect(
        (await import("../../app/services/players.server")).requestRefresh("#2PP", key),
      ).rejects.toMatchObject({ status: 502 });
      vi.unstubAllGlobals();
    },
  );
});
