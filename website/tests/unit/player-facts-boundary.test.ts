import { describe, expect, it } from "vitest";
import { mapPlayerPage } from "../../app/services/player-response.server";
import { dayEnd, dayPresentation } from "../../app/lib/player-lookup-text";
import { CALENDAR, RAW_DAY_FACTS, RAW_PLAYER_FACTS } from "../fixtures/player-route-data";

function reply() {
  const day = {
    ...RAW_DAY_FACTS,
    ranked_day_start: CALENDAR.day_start,
    ranked_day_end: CALENDAR.day_end,
    official_season_id: CALENDAR.season_id,
    season_day_number: 4,
    state: "Complete",
    confidence: "exact",
    completeness: { state: "complete", reason: "Confirmed" },
    public_confidence: "high",
    uncertainty_reasons: [],
    start_trophies: 5000,
    attack_count: 1,
    attack_three_star_count: 1,
    attack_gain: 40,
    defense_count: 0,
    defense_three_star_count: 0,
    defense_loss: 0,
    net_trophy_change: 40,
    offense_events: [],
    defense_events: [],
    display_start: {
      trophies: 1234,
      source: "calculated",
      calculation: { end_trophies: 1274, net_change: 40 },
    },
    presentation: { ...RAW_DAY_FACTS.presentation, status: "calculated" },
    display_end: { ...RAW_DAY_FACTS.display_end, player_log: 4321, status: "calculated" },
  };
  return {
    tag: "#2PP",
    name: "Nova",
    trophies: 6000,
    screen_ready: {
      ...RAW_PLAYER_FACTS,
      days: [day],
      current_day_start: null,
      recent_day_starts: [day.ranked_day_start],
      season_day_starts: [],
      history_day_starts: [day.ranked_day_start],
      history_day_numbers: { [day.ranked_day_start]: 4 },
      season: null,
      data_quality: [],
      provenance: {
        source: "test",
        observed_at: CALENDAR.as_of,
        freshness: "fresh",
        confidence: "high",
        coverage: "complete",
        version: "test",
      },
    },
  };
}

describe("player facts response boundary", () => {
  it("uses supplied facts independently of the saved evidence", () => {
    const page = mapPlayerPage(reply());
    const day = page.historyDays[0];
    expect(day.startTrophies).toBe(1234);
    expect(dayEnd(day).trophies).toBe(4321);
    expect(dayPresentation(day).status).toBe("Calculated");
    expect(day.start).toBe(CALENDAR.day_start);
    expect(day.seasonId).toBe(CALENDAR.season_id);
    expect(day).not.toHaveProperty("period");
  });
  it.each([
    ["saved", null, undefined],
    ["season_rule", null, "Season rule"],
    ["calculated", { end_trophies: 1274, net_change: 40 }, "Calculated"],
  ] as const)(
    "labels a %s start and keeps its calculation",
    (source, calculation, label) => {
      const raw = reply();
      Object.assign(raw.screen_ready.days[0], {
        display_start: { trophies: 1234, source, calculation },
      });
      Object.assign(raw.screen_ready, { season_day_starts: [CALENDAR.day_start] });
      const page = mapPlayerPage(raw);
      const day = page.recentDays[0];
      expect(day.startTrophies).toBe(1234);
      expect(day.startTrophiesSource).toBe(label);
      expect(day.startTrophiesCalculation?.trophies).toBe(calculation?.end_trophies);
      // A day in both lists reads the same, so the page data carries it once.
      expect(day).toBe(page.seasonDays[0]);
    },
  );
  it("reads the automatic defense loss and other Reset adjustments", () => {
    const load = (adjustments: unknown) => {
      const raw = reply();
      Object.assign(raw.screen_ready.days[0], { adjustments });
      return mapPlayerPage(raw).recentDays[0];
    };
    expect(
      load([
        { type: "automatic_defense", amount: -32, evidence_state: "observed" },
        { type: "season_reset", amount: -170, evidence_state: "official_rule" },
      ]),
    ).toMatchObject({
      automaticDefenseLoss: 32,
      resetAdjustment: { kind: "Season", amount: -170 },
    });
    expect(load([])).toMatchObject({
      automaticDefenseLoss: null,
      resetAdjustment: null,
    });
    expect(() => load([{ type: "automatic_defense", amount: "-32" }])).toThrow();
  });
  it.each([
    "calendar",
    "season_shape",
    "history_day_starts",
    "history_day_numbers",
    "battle_statistics",
    "dashboard_season",
    "rival_season",
    "trophy_trends",
  ])("rejects a full reply missing %s", (key) => {
    const raw = reply();
    delete (raw.screen_ready as Record<string, unknown>)[key];
    expect(() => mapPlayerPage(raw)).toThrow();
  });
  it("accepts lightweight replies without full battle-derived sections", () => {
    const raw = reply();
    for (const key of ["battle_statistics", "dashboard_season", "trophy_trends"])
      delete (raw.screen_ready as Record<string, unknown>)[key];
    expect(mapPlayerPage(raw, false).battleStatistics).toBeNull();
    expect(() => mapPlayerPage(raw)).toThrow();
  });
});
