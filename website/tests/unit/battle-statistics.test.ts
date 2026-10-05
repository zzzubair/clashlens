import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { BattleStatistics } from "../../app/components/BattleStatistics";
import { battleStatistics } from "../../app/lib/battle-statistics";
import type {
  PlayerPage,
  RankedBattleEvent,
  RankedDaySummary,
} from "../../app/lib/contracts";
import { worstCasePlayer } from "../fixtures/player-worst-case";

const NOW = Date.parse("2026-10-04T20:00:00Z");
const DAY = 86_400_000;
const TODAY = Date.parse("2026-10-04T05:00:00Z");
function event(
  id: string,
  stars: number,
  destruction: number,
  trophies: number,
  time = TODAY,
): RankedBattleEvent {
  return {
    battleId: id,
    stars,
    destructionPercentage: destruction,
    trophyChange: trophies,
    battleTimestamp: new Date(time).toISOString(),
    opponent: { tag: "#2PP", name: null },
    perspectiveDisagreement: false,
  };
}
function day(
  offset: number,
  attacks: RankedBattleEvent[] = [],
  defenses: RankedBattleEvent[] = [],
): RankedDaySummary {
  const start = TODAY - offset * DAY;
  return {
    dayNumber: 28 - offset,
    label: "Legend day",
    period: `${new Date(start).toISOString()} – ${new Date(start + DAY).toISOString()}`,
    state: offset === 0 ? "Live" : "Complete",
    offense: { attacks: attacks.length, threeStars: null, trophyGain: 999 },
    defense: { defenses: defenses.length, threeStarsAgainst: null, trophyLoss: 999 },
    trophyChange: -999,
    offenseEvents: attacks,
    defenseEvents: defenses,
    completeness: { state: "complete", reason: "" },
    battlesComplete: true,
    uncertainty: [],
  };
}
function player(days: RankedDaySummary[] = []): PlayerPage {
  return {
    ...worstCasePlayer("#2PP", NOW),
    season: {
      id: "1788757200",
      anchor: "2026-09-07T05:00:00Z",
      currentDayNumber: 28,
      dayCount: 28,
      anchorSource: "official_league_history",
      anchorObservedAt: "2026-09-07T06:00:00Z",
    },
    currentDay: days.find((saved) => saved.state === "Live") ?? null,
    recentDays: days,
    seasonDays: days,
  };
}

describe("recorded battle period statistics", () => {
  it("weights averages by battles and counts actual holds, including zero-star defenses", () => {
    const yesterday = TODAY - DAY;
    const stats = battleStatistics(
      player([
        day(0, [event("a1", 3, 100, 40)], [event("d1", 0, 0, 0), event("d2", 1, 40, -5)]),
        day(
          1,
          [event("a2", 2, 80, 20, yesterday), event("a3", 1, 40, 10, yesterday)],
          [event("d3", 2, 80, -20, yesterday), event("d4", 3, 100, -40, yesterday)],
        ),
      ]),
      "7",
      NOW,
    );
    expect(stats.attack.count).toBe(3);
    expect(stats.attack.averageStars).toBe(2);
    expect(stats.attack.averageDestruction).toBeCloseTo(220 / 3);
    expect(stats.attack.averageTrophies).toBeCloseTo(70 / 3);
    expect(stats.attack.tripleRate).toBeCloseTo(100 / 3);
    expect(stats.defense).toMatchObject({
      count: 4,
      stars: [1, 1, 1, 1],
      averageStars: 1.5,
      averageDestruction: 55,
      averageTrophies: 16.25,
      holdRate: 75,
    });
    expect(stats.daysSaved).toBe(2);
    expect(stats.daysExpected).toBe(7);
  });

  it.each([
    ["7", 7],
    ["14", 14],
    ["season", 28],
  ] as const)("selects %s Legend days, not the latest saved days", (period, count) => {
    const days = Array.from({ length: 30 }, (_, offset) =>
      day(offset, [event(`a${offset}`, 3, 100, 40, TODAY - offset * DAY)]),
    );
    const stats = battleStatistics(player(days), period, NOW);
    expect(stats.attack.count).toBe(count);
    expect(stats.start).toBe(TODAY - (count - 1) * DAY);
    expect(stats.daysExpected).toBe(count);
    expect(stats.daysSaved).toBe(count);
  });

  it("does not fill gaps with old days or count a repeated battle twice", () => {
    const repeated = event("same", 3, 100, 40);
    const stats = battleStatistics(
      player([
        day(0, [repeated, repeated]),
        day(20, [event("old", 3, 100, 40, TODAY - 20 * DAY)]),
      ]),
      "7",
      NOW,
    );
    expect(stats.attack.count).toBe(1);
    expect(stats.daysSaved).toBe(1);
    expect(stats.daysExpected).toBe(7);
  });

  it("has no rates or averages for zero battles and never invents holds", () => {
    const stats = battleStatistics(player([day(0)]), "season", NOW);
    for (const side of [stats.attack, stats.defense]) {
      expect(side).toMatchObject({
        count: 0,
        stars: [0, 0, 0, 0],
        averageStars: null,
        averageDestruction: null,
        averageTrophies: null,
        tripleRate: null,
        holdRate: null,
      });
    }
  });

  it("moves windows at 05:00 UTC and keeps recent windows across Season Reset", () => {
    const saved = player([day(0, [event("last-season", 3, 100, 40)])]);
    const reset = TODAY + DAY;
    expect(battleStatistics(saved, "season", reset - 1).attack.count).toBe(1);
    expect(battleStatistics(saved, "season", reset)).toMatchObject({
      start: reset,
      daysExpected: 1,
      attack: { count: 0 },
    });
    expect(battleStatistics(saved, "7", reset)).toMatchObject({
      start: reset - 6 * DAY,
      attack: { count: 1 },
    });
  });

  it("excludes out-of-window and future timestamps and flags incomplete or disputed records", () => {
    const saved = day(0, [
      event("before", 3, 100, 40, TODAY - 7 * DAY),
      event("future", 3, 100, 40, NOW + 1),
      { ...event("disputed", 2, 90, 30), perspectiveDisagreement: true },
    ]);
    saved.battlesComplete = false;
    const stats = battleStatistics(player([saved]), "7", NOW);
    expect(stats.attack.count).toBe(1);
    expect(stats.attack.disputed).toBe(true);
    expect(stats.incomplete).toBe(true);
  });

  it("renders dates, sample sizes, missing-data notice, and unavailable empty averages", () => {
    const html = renderToStaticMarkup(
      createElement(BattleStatistics, { player: player(), now: NOW }),
    );
    expect(html).toContain("7 Sep 2026 at 05:00 UTC to 4 Oct 2026 so far.");
    expect(html).toContain("0 of 28 Legend days have saved logs.");
    expect(html).toContain("Some battle details may be missing.");
    expect(html).toContain("<dt>Attacks in sample</dt><dd>0</dd>");
    expect(html).toContain("<dt>Defenses in sample</dt><dd>0</dd>");
    expect(html).toContain("<dt>Hold rate</dt><dd>Unavailable</dd>");
    expect(html).toContain("<dt>Average destruction</dt><dd>Unavailable</dd>");
    expect(html).not.toContain("NaN");
    expect(html).not.toContain("Infinity");
  });
});
