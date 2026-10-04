import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const TEST_SECRET = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8";

describe("calculated starting trophies on the player page", () => {
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
    ["matching saved day", "2026-09-21T21:41:20Z", "normal", 5632, 5462, 5427],
    ["before the next 05:00 reset", "2026-09-22T04:59:00Z", "normal", 5632, 5462, 5427],
    ["profile from another day", "2026-09-22T05:00:00Z", "normal", 5632, null, null],
    [
      "battles newer than the profile",
      "2026-09-21T12:00:00Z",
      "normal",
      5632,
      null,
      null,
    ],
    ["missing battle counts", "2026-09-21T21:41:20Z", "missing-count", 5632, null, null],
    [
      "partial day with one saved attack",
      "2026-09-21T21:41:20Z",
      "partial-current",
      5632,
      null,
      null,
    ],
    [
      "uncertain day with one saved attack",
      "2026-09-21T21:41:20Z",
      "uncertain-current",
      5632,
      null,
      null,
    ],
    [
      "all sixteen battles without a reset snapshot",
      "2026-09-21T21:41:20Z",
      "full-battles",
      5632,
      5632,
      5597,
    ],
    ["stored total takes priority", "2026-09-21T21:41:20Z", "stored", 5632, 5500, 5465],
    ["incomplete older day", "2026-09-21T21:41:20Z", "partial-older", 5632, 5462, null],
    ["gap between days", "2026-09-21T21:41:20Z", "gap", 5632, 5462, null],
    ["season reset", "2026-09-21T21:41:20Z", "season-reset", 5632, 5462, null],
    ["zero starting total", "2026-09-21T21:41:20Z", "normal", 170, 0, null],
    ["impossible negative total", "2026-09-21T21:41:20Z", "normal", 100, null, null],
    ["negative daily change", "2026-09-21T21:41:20Z", "negative-net", 5632, 5792, 5757],
    ["zero daily change", "2026-09-21T21:41:20Z", "zero-net", 5632, 5632, 5597],
    [
      "sixteen battles after a Reset profile rejected for its timing",
      "2026-09-21T21:41:20Z",
      "late-reset-profile",
      6040,
      6000,
      5965,
    ],
  ])(
    "calculates starting trophies: %s",
    async (_name, observedAt, variant, trophies, expected, olderExpected) => {
      const makeDay = (date: number, losses: number[], attacks = 8) => {
        const event = (id: string, change: number) => ({
          battle_id: id,
          battle_timestamp: `2026-09-${date}T13:00:00Z`,
          opponent: { tag: "#2PY", name: "Opponent" },
          stars: Math.abs(change) === 40 ? 3 : 1,
          destruction_percentage: 100,
          trophy_change: change,
        });
        return {
          ranked_day_start: `2026-09-${date}T05:00:00Z`,
          ranked_day_end: `2026-09-${date + 1}T05:00:00Z`,
          season_day_number: date - 6,
          state: "Live",
          confidence: "partial",
          completeness: { state: "partial", reason: "No saved reset total." },
          public_confidence: "partial",
          uncertainty_reasons: [],
          start_trophies: null as number | null,
          attack_count: attacks as number | null,
          attack_three_star_count: attacks,
          attack_gain: attacks * 40,
          defense_count: losses.length,
          defense_three_star_count: losses.filter((value) => value === 40).length,
          defense_loss: losses.reduce((sum, value) => sum + value, 0),
          net_trophy_change: null,
          offense_events: Array.from({ length: attacks }, (_, i) =>
            event(`attack-${date}-${i}`, 40),
          ),
          defense_events: losses.map((loss, i) => event(`defense-${date}-${i}`, -loss)),
        };
      };
      const day = makeDay(
        21,
        variant.endsWith("-current")
          ? []
          : variant === "zero-net" || variant === "full-battles"
            ? Array(8).fill(40)
            : variant === "late-reset-profile"
              ? [40, 40, 40, 40, 40, 40, 20, 20]
              : variant === "negative-net"
                ? [40, 40, 40, 40]
                : [40, 40, 40, 30],
        variant.endsWith("-current") ? 1 : variant === "negative-net" ? 0 : 8,
      );
      const older = makeDay(
        variant === "gap" ? 19 : 20,
        variant === "partial-older" ? [40, 40, 40, 40] : [40, 40, 40, 40, 40, 40, 40, 5],
      );
      if (
        !variant.endsWith("-current") &&
        variant !== "full-battles" &&
        variant !== "late-reset-profile" &&
        variant !== "stored"
      ) {
        day.completeness = {
          state: "complete",
          reason: "All changes since reset are recorded.",
        };
      }
      if (variant === "uncertain-current") day.completeness.state = "uncertain";
      if (variant === "missing-count") day.attack_count = null;
      if (variant === "stored") day.start_trophies = 5500;
      if (variant === "season-reset") {
        day.season_day_number = 1;
        older.season_day_number = 28;
      }
      const payload = {
        tag: "#2PP",
        name: "Angela",
        trophies,
        current_league_season_id: "1788757200",
        observed_at: observedAt,
        screen_ready: {
          days: [day, older],
          current_day_start: day.ranked_day_start,
          recent_day_starts: [day.ranked_day_start, older.ranked_day_start],
          season_day_starts: [day.ranked_day_start, older.ranked_day_start],
          season: null,
          data_quality: [],
          provenance: {
            source: "test",
            observed_at: observedAt,
            freshness: "stale",
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
      expect(player.currentDay?.startTrophies).toBe(expected);
      expect(player.seasonDays.map((value) => value.startTrophies)).toEqual([
        expected,
        olderExpected,
      ]);
      expect(player.recentDays.map((value) => value.startTrophies)).toEqual([
        expected,
        olderExpected,
      ]);
      expect(player.currentDay?.startTrophiesCalculation?.trophies).toBe(
        expected !== null && variant !== "stored" ? trophies : undefined,
      );
    },
  );
});
