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
    expect(stats.attack).toMatchObject({
      count: 3,
      stars: [0, 1, 1, 1],
      trophies: 70,
      finishedTrophies: 30,
    });
    expect(stats.defense).toMatchObject({
      count: 4,
      stars: [1, 1, 1, 1],
      trophies: 65,
      finishedTrophies: 60,
    });
    expect(stats.daysSaved).toBe(2);
    expect(stats.attack.activeDays).toBe(1);
    expect(stats.daysExpected).toBe(7);
  });

  it("counts a battle reported just after Reset on the finished day it belongs to", () => {
    const yesterday = TODAY - DAY;
    const stats = battleStatistics(
      player([
        day(0),
        day(
          1,
          [event("a1", 3, 100, 40, yesterday), event("late", 3, 100, 40, TODAY + 60_000)],
          [event("d1", 3, 100, -40, TODAY + 60_000)],
        ),
      ]),
      "7",
      NOW,
    );
    expect(stats.attack).toMatchObject({ trophies: 80, finishedTrophies: 80 });
    expect(stats.defense).toMatchObject({ trophies: 40, finishedTrophies: 40 });
    expect(stats.attack.activeDays).toBe(1);
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

  it("counts nothing for zero battles", () => {
    const stats = battleStatistics(player([day(0)]), "season", NOW);
    for (const side of [stats.attack, stats.defense]) {
      expect(side).toMatchObject({ count: 0, stars: [0, 0, 0, 0], trophies: 0 });
    }
  });

  it("moves windows at 05:00 UTC and keeps recent windows inside the new Season", () => {
    const saved = player([day(0, [event("last-season", 3, 100, 40)])]);
    const reset = TODAY + DAY;
    expect(battleStatistics(saved, "season", reset - 1).attack.count).toBe(1);
    expect(battleStatistics(saved, "season", reset)).toMatchObject({
      start: reset,
      daysExpected: 1,
      attack: { count: 0 },
    });
    expect(battleStatistics(saved, "7", reset)).toMatchObject({
      start: reset,
      daysExpected: 1,
      attack: { count: 0 },
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

  it("renders one summary with dates and unavailable rates and averages", () => {
    const html = renderToStaticMarkup(
      createElement(BattleStatistics, { player: player(), now: NOW, trophies: "6,000" }),
    );
    expect(html).toContain(
      "7 Sep – 4 Oct · 0 of 28 days saved · Some battles may be missing",
    );
    expect(html).toContain(
      '<dt>Rank at last Reset</dt><dd class="summary-words">Not ranked yet</dd>',
    );
    expect(html).toContain("<dt>Trophies now</dt><dd>6,000</dd>");
    expect(html).toContain('<dt>Hit rate</dt><dd class="summary-words">Unavailable</dd>');
    expect(html).toContain("<dt>Per attack</dt><dd>Unavailable</dd>");
    expect(html).not.toContain("Trophies lost");
    expect(html).not.toContain("Stars unknown");
    expect(html).not.toContain("NaN");
    expect(html).not.toContain("Infinity");
  });

  it("shows hit rate, stars, averages and the latest Reset rank this Season", () => {
    const yesterday = TODAY - DAY;
    const html = renderToStaticMarkup(
      createElement(BattleStatistics, {
        player: player([
          day(0, [event("a1", 3, 100, 40)], [event("d1", 1, 40, -5)]),
          {
            ...day(
              1,
              [event("a2", 3, 100, 40, yesterday), event("a3", 2, 80, 20, yesterday)],
              [event("d2", 3, 100, -40, yesterday)],
            ),
            resetRank: 1042,
          },
          { ...day(2), resetRank: 2000 },
          // Last Season's rank never counts.
          { ...day(40), resetRank: 1 },
        ]),
        now: NOW,
        trophies: "6,100",
      }),
    );
    expect(html).toContain("<dt>Rank at last Reset</dt><dd>1,042</dd>");
    expect(html).toContain("66.7%<small>2 of 3 attacks</small>");
    expect(html).toMatch(
      /Attacks<\/th><td>3<\/td><td>2<\/td><td>1<\/td><td>0<\/td><td>0</,
    );
    expect(html).toMatch(
      /Defenses<\/th><td>2<\/td><td>1<\/td><td>0<\/td><td>1<\/td><td>0</,
    );
    // Per day uses finished days with battles: day 2, with none, doesn't count.
    expect(html).toMatch(/Offense per day<\/dt><dd>\+60</);
    expect(html).toMatch(/Defense per day<\/dt><dd>-40</);
    expect(html).toContain("<dt>Per attack</dt><dd>+33.3</dd>");
    expect(html).toContain("<dt>Per defense</dt><dd>-22.5</dd>");
  });

  it("labels recent windows that the Season hasn't filled yet", () => {
    const html = (now: number) =>
      renderToStaticMarkup(
        createElement(BattleStatistics, {
          player: player(),
          now,
          trophies: "5,000",
        }),
      );
    const early = html(Date.parse("2026-10-06T12:00:00Z"));
    expect(early).toContain("Last 7 days (2 so far)");
    expect(early).toContain("Last 14 days (2 so far)");
    expect(html(NOW)).toContain(">Last 7 days</option>");
  });

  it("shows a missing latest Reset rank instead of an older one", () => {
    const html = renderToStaticMarkup(
      createElement(BattleStatistics, {
        player: player([
          day(0),
          { ...day(1), resetRank: null },
          { ...day(2), resetRank: 1042 },
        ]),
        now: NOW,
        trophies: "6,100",
      }),
    );
    expect(html).toContain(
      '<dt>Rank at last Reset</dt><dd class="summary-words">Not ranked yet</dd>',
    );
    expect(html).not.toContain("1,042");
  });

  it("names unknown stars only when some are unknown", () => {
    const side = { count: 4, stars: [0, 1, 1, 1], trophies: 60, perDay: 60 };
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
