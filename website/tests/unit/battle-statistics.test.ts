import { describe, expect, it } from "vitest";
import cases from "../../../testdata/w6-presentation-parity.json";
import { mapBattleStatistics } from "../../app/services/player-facts.server";

// Python executes the original numerical examples, including grace-window,
// duplicate, future, shield and missing-rank cases. This boundary reads its
// supplied answers; it no longer derives statistics from battle events.
describe("Python's recorded battle statistics", () => {
  for (const [index, example] of cases
    .filter((c) => c.rule === "battle_statistics")
    .entries()) {
    it(`accepts the shared produced answer ${index}`, () => {
      const stats = mapBattleStatistics(example.expected);
      expect(stats.season.attack.perDay).toBe(
        (example.expected as { season: { attack: { per_day: number | null } } }).season
          .attack.per_day,
      );
    });
  }
  it("rejects missing, unknown and malformed statistics instead of inventing zero", () => {
    const valid = cases.find((c) => c.rule === "battle_statistics")!.expected;
    for (const value of [
      null,
      {},
      { ...(valid as Record<string, unknown>), season: null },
    ])
      expect(() => mapBattleStatistics(value)).toThrow();
  });
});
