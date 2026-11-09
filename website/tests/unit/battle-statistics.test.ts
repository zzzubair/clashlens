import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import cases from "../../../testdata/w6-presentation-parity.json";
import { BattleStatistics } from "../../app/components/BattleStatistics";
import { mapBattleStatistics } from "../../app/services/player-facts.server";
import { PLAYER, EMPTY_STATISTICS } from "../fixtures/player-route-data";

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
  it("renders supplied totals even with no local battle events", () => {
    const stats = {
      ...EMPTY_STATISTICS,
      daysExpected: 5,
      daysSaved: 3,
      attack: {
        ...EMPTY_STATISTICS.attack,
        count: 5,
        stars: [0, 0, 1, 4],
        perDay: 90,
        perBattle: 36,
      },
    };
    const html = renderToStaticMarkup(
      createElement(BattleStatistics, {
        player: {
          ...PLAYER,
          battleStatistics: { "7": stats, "14": stats, season: stats },
        },
        now: 0,
      }),
    );
    expect(html).toContain("2 days missing");
    expect(html).toContain("90");
    expect(html).toContain("36");
    expect(html).toContain("Not ranked yet");
  });
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
