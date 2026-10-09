import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { BattleStatistics } from "../../app/components/BattleStatistics";
import { SeasonSummary } from "../../app/components/SeasonSummary";
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

const YESTERDAY = TODAY - DAY;
const render = (saved: PlayerPage, now = NOW) =>
  renderToStaticMarkup(createElement(BattleStatistics, { player: saved, now }));

describe("recorded battle period statistics", () => {
  it("counts finished days only, never today's battles", () => {
    const stats = battleStatistics(
      player([
        day(0, [event("a1", 3, 100, 40)], [event("d1", 0, 0, 0), event("d2", 1, 40, -5)]),
        day(
          1,
          [event("a2", 2, 80, 20, YESTERDAY), event("a3", 1, 40, 10, YESTERDAY)],
          [event("d3", 2, 80, -20, YESTERDAY), event("d4", 0, 0, 0, YESTERDAY)],
        ),
      ]),
      "7",
      NOW,
    );
    expect(stats.attack).toMatchObject({
      count: 2,
      stars: [0, 1, 1, 0],
      trophies: 30,
      perDay: 30,
      perBattle: 15,
    });
    // A zero-star defense is a hold and still counts.
    expect(stats.defense).toMatchObject({ count: 2, stars: [1, 0, 1, 0], trophies: 20 });
    expect(stats.daysSaved).toBe(1);
    expect(stats.daysExpected).toBe(7);
  });

  it("counts a battle reported just after Reset on the finished day it belongs to", () => {
    const stats = battleStatistics(
      player([
        day(0),
        day(
          1,
          [event("a1", 3, 100, 40, YESTERDAY), event("late", 3, 100, 40, TODAY + 60_000)],
          [event("d1", 3, 100, -40, TODAY + 60_000)],
        ),
      ]),
      "7",
      NOW,
    );
    expect(stats.attack).toMatchObject({ count: 2, trophies: 80, perDay: 80 });
    expect(stats.defense).toMatchObject({ count: 1, trophies: 40, perDay: 40 });
  });

  it.each([
    ["7", 7],
    ["14", 14],
    // 7 Sep to 3 Oct: the Season's 27 finished days.
    ["season", 27],
  ] as const)("selects the last %s finished Legend days", (period, count) => {
    const days = Array.from({ length: 30 }, (_, offset) =>
      day(offset, [event(`a${offset}`, 3, 100, 40, TODAY - offset * DAY)]),
    );
    const stats = battleStatistics(player(days), period, NOW);
    expect(stats.attack.count).toBe(count);
    expect(stats.start).toBe(TODAY - count * DAY);
    expect(stats.daysExpected).toBe(count);
    expect(stats.daysSaved).toBe(count);
  });

  it("does not fill gaps with old days or count a repeated battle twice", () => {
    const repeated = event("same", 3, 100, 40, YESTERDAY);
    const stats = battleStatistics(
      player([
        day(1, [repeated, repeated]),
        day(20, [event("old", 3, 100, 40, TODAY - 20 * DAY)]),
      ]),
      "7",
      NOW,
    );
    expect(stats.attack.count).toBe(1);
    expect(stats.daysSaved).toBe(1);
    expect(stats.daysExpected).toBe(7);
  });

  it("counts nothing for zero battles but still averages over the finished day", () => {
    const stats = battleStatistics(player([day(1)]), "season", NOW);
    for (const side of [stats.attack, stats.defense]) {
      expect(side).toMatchObject({
        count: 0,
        stars: [0, 0, 0, 0],
        trophies: 0,
        perDay: 0,
        perBattle: null,
      });
    }
  });

  it("starts a new Season with no finished days and keeps the last Season out", () => {
    const saved = player([day(0, [event("last-season", 3, 100, 40)])]);
    const reset = TODAY + DAY;
    expect(battleStatistics(saved, "season", reset - 1).attack.count).toBe(0);
    expect(battleStatistics(saved, "season", reset)).toMatchObject({
      start: reset,
      daysExpected: 0,
      attack: { count: 0 },
    });
    expect(battleStatistics(saved, "7", reset + DAY)).toMatchObject({
      start: reset,
      daysExpected: 1,
      attack: { count: 0 },
    });
  });

  it("excludes out-of-window and future timestamps and flags incomplete or disputed records", () => {
    const saved = day(1, [
      event("before", 3, 100, 40, TODAY - 8 * DAY),
      event("future", 3, 100, 40, NOW + 1),
      { ...event("disputed", 2, 90, 30, YESTERDAY), perspectiveDisagreement: true },
    ]);
    saved.battlesComplete = false;
    const stats = battleStatistics(player([saved]), "7", NOW);
    expect(stats.attack.count).toBe(1);
    expect(stats.attack.disputed).toBe(true);
    expect(stats.incomplete).toBe(true);
  });

  it("averages every finished day's battles, Uncertain days included", () => {
    const saved = player([
      day(1, [event("a1", 3, 100, 40, YESTERDAY)]),
      { ...day(2, [event("a2", 1, 50, 10, TODAY - 2 * DAY)]), trophyChange: null },
      day(3),
    ]);
    const stats = battleStatistics(saved, "7", NOW);
    expect(stats.attack).toMatchObject({ count: 2, trophies: 50, perBattle: 25 });
    expect(stats.attack.perDay).toBeCloseTo(50 / 3);
  });

  it("renders one summary with dates and unavailable rates and averages", () => {
    const html = render(player());
    expect(html).toContain(
      "7 Sep – 3 Oct · 0 of 27 finished days saved · Some battles may be missing",
    );
    expect(html).toContain(
      '<dt>Rank at last Reset</dt><dd class="summary-words">Not ranked yet</dd>',
    );
    expect(html).toContain(
      '<dt>Trophies at last Reset</dt><dd class="summary-words">Unavailable</dd>',
    );
    expect(html).toContain('<dt>Hit rate</dt><dd class="summary-words">Unavailable</dd>');
    expect(html).toContain("<dt>Per attack</dt><dd>Unavailable</dd>");
    expect(html).not.toContain("Trophies now");
    expect(html).not.toContain("Stars unknown");
    expect(html).not.toContain("NaN");
    expect(html).not.toContain("Infinity");
  });

  it("shows the latest Reset's rank and trophies, hit rate, stars and averages", () => {
    const html = render(
      player([
        // Today's battles never count.
        day(0, [event("a1", 3, 100, 40)], [event("d1", 1, 40, -5)]),
        {
          ...day(
            1,
            [event("a2", 3, 100, 40, YESTERDAY), event("a3", 2, 80, 20, YESTERDAY)],
            [event("d2", 3, 100, -40, YESTERDAY)],
          ),
          // Sunday: 4,900 + 20, raised 80 to 5,000 by the weekly reset.
          startTrophies: 4900,
          trophyChange: 20,
          resetAdjustment: { kind: "weekly", amount: 80 },
          resetRank: 1042,
        },
        { ...day(2), resetRank: 2000 },
        // Last Season's rank never counts.
        { ...day(40), resetRank: 1 },
      ]),
    );
    expect(html).toContain("<dt>Rank at last Reset</dt><dd>1,042</dd>");
    expect(html).toMatch(
      /<dt>Trophies at last Reset<\/dt><dd>5,000<span class="day-mark" title="Calculated">/,
    );
    expect(html).toContain("50.0%<small>1 of 2 attacks</small>");
    expect(html).toMatch(
      /Attacks<\/th><td>2<\/td><td>1<\/td><td>1<\/td><td>0<\/td><td>0</,
    );
    expect(html).toMatch(
      /Defenses<\/th><td>1<\/td><td>1<\/td><td>0<\/td><td>0<\/td><td>0</,
    );
    // Per day divides by every finished day: day 2, with none, counts too.
    expect(html).toMatch(/Offense per day<\/dt><dd>\+30</);
    expect(html).toMatch(/Defense per day<\/dt><dd>-20</);
    expect(html).toContain("<dt>Per attack</dt><dd>+30.0</dd>");
    expect(html).toContain("<dt>Per defense</dt><dd>-40.0</dd>");
  });

  it("labels recent windows that the Season hasn't filled yet", () => {
    const early = render(player(), Date.parse("2026-10-06T12:00:00Z"));
    expect(early).toContain("Last 7 days (1 so far)");
    expect(early).toContain("Last 14 days (1 so far)");
    expect(render(player())).toContain(">Last 7 days</option>");
    const first = render(player(), Date.parse("2026-10-05T12:00:00Z"));
    expect(first).toContain("No finished Legend days yet this Season");
    expect(first).toContain("No finished day yet");
  });

  it("shows a missing latest Reset rank instead of an older one", () => {
    const html = render(
      player([
        day(0),
        { ...day(1), resetRank: null },
        { ...day(2), resetRank: 1042, startTrophies: 5000 },
      ]),
    );
    expect(html).toContain(
      '<dt>Rank at last Reset</dt><dd class="summary-words">Not ranked yet</dd>',
    );
    expect(html).not.toContain("1,042");
  });

  it("names unknown stars only when some are unknown", () => {
    const side = {
      count: 4,
      stars: [0, 1, 1, 1],
      perDay: 60,
      perBattle: 15,
    };
    const render = (unknown: number | null) =>
      renderToStaticMarkup(
        createElement(SeasonSummary, {
          title: "Season",
          rank: ["Final rank", "1"],
          trophies: ["Final trophies", "5,000"],
          attack: { ...side, unknown },
          defense: { ...side, unknown: 0 },
        }),
      );
    expect(render(0)).not.toContain("unknown");
    expect(render(null)).not.toContain("unknown");
    expect(render(1)).toContain("Stars unknown for 1 attack.");
    // Hit rate counts every attack, including ones with unknown stars.
    expect(render(1)).toContain("25.0%<small>1 of 4 attacks</small>");
  });
});
