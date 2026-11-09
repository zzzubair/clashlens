import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { BattleStatistics } from "../../app/components/BattleStatistics";
import { SeasonSummary } from "../../app/components/SeasonSummary";
import type { BattleStatisticsFact, RankedDaySummary } from "../../app/lib/contracts";
import {
  PLAYER,
  SAVED_DAY,
  EMPTY_STATISTICS,
  UNKNOWN_END,
} from "../fixtures/player-route-data";

const stats = (): BattleStatisticsFact => ({
  ...EMPTY_STATISTICS,
  daysExpected: 27,
  daysSaved: 0,
  incomplete: true,
});
const ended = (
  start: string,
  trophies: number,
  rank: number | null,
  status: "calculated" | "uncertain" = "calculated",
): RankedDaySummary => ({
  ...SAVED_DAY,
  start,
  resetRank: rank,
  displayEnd: {
    ...UNKNOWN_END,
    before_reset: trophies,
    after_reset: trophies,
    player_log: trophies,
    dashboard: trophies,
    status,
  },
});
const render = (facts = stats(), days: RankedDaySummary[] = []) =>
  renderToStaticMarkup(
    createElement(BattleStatistics, {
      player: {
        ...PLAYER,
        recentDays: days,
        battleStatistics: { "7": facts, "14": facts, season: facts },
      },
      now: 0,
    }),
  );

describe("supplied Season summary rendering", () => {
  it("renders one summary with dates and unavailable rates and averages", () => {
    const html = render();
    expect(html).toContain("27 days missing · Some battles may be missing");
    expect(html).toContain(
      '<dt>Reset rank</dt><dd class="summary-words">Not ranked yet</dd>',
    );
    expect(html).toContain(
      '<dt>Reset trophies</dt><dd class="summary-words">Unavailable</dd>',
    );
    expect(html).toContain('<dt>Hit rate</dt><dd class="summary-words">Unavailable</dd>');
    expect(html).toContain("<dt>Per attack</dt><dd>Unavailable</dd>");
    for (const text of ["Trophies now", "Stars unknown", "NaN", "Infinity"])
      expect(html).not.toContain(text);
  });
  it("shows the latest Reset's rank and trophies, hit rate, stars and averages", () => {
    const day = ended("2026-10-04T05:00:00Z", 5000, 1042);
    const facts = {
      ...stats(),
      resetDayStart: day.start,
      attack: {
        ...stats().attack,
        count: 2,
        stars: [0, 0, 1, 1],
        trophies: 60,
        perDay: 60,
        perBattle: 30,
      },
      defense: {
        ...stats().defense,
        count: 1,
        stars: [0, 0, 0, 1],
        trophies: 40,
        perDay: 40,
        perBattle: 40,
      },
    };
    const html = render(facts, [day]);
    expect(html).toContain("<dt>Reset rank</dt><dd>1,042</dd>");
    expect(html).toMatch(
      /<dt>Reset trophies<\/dt><dd>5,000<span class="day-mark" title="Calculated">/,
    );
    expect(html).toContain("50.0%<small>1 of 2</small>");
    expect(html).toMatch(
      /Attacks<\/th><td[^>]*>2<\/td><td[^>]*>1<\/td><td[^>]*>1<\/td><td[^>]*>0<\/td><td[^>]*>0</,
    );
    expect(html).toMatch(
      /Defenses<\/th><td[^>]*>1<\/td><td[^>]*>1<\/td><td[^>]*>0<\/td><td[^>]*>0<\/td><td[^>]*>0</,
    );
    expect(html).toMatch(/Offense per day<\/dt><dd>\+60</);
    expect(html).toMatch(/Defense per day<\/dt><dd>−40</);
    expect(html).toContain("<dt>Per attack</dt><dd>+30.0</dd>");
    expect(html).toContain("<dt>Per defense</dt><dd>−40.0</dd>");
  });
  it("offers no recent-days choice and says when no day has finished", () => {
    const html = render({ ...stats(), daysExpected: 0 });
    expect(html).not.toContain("Last 7 days");
    expect(html).toContain("No finished Legend days yet this Season");
    expect(html).toContain("No finished day yet");
  });
  it("shows the Reset before's rank and trophies until the latest board ranks the player", () => {
    const older = ended("2026-10-03T05:00:00Z", 5100, 1042);
    const newest = ended("2026-10-04T05:00:00Z", 4101, null);
    const html = render({ ...stats(), resetDayStart: older.start }, [newest, older]);
    expect(html).toContain("<dt>Reset rank</dt><dd>1,042</dd>");
    expect(html).toMatch(/<dt>Reset trophies<\/dt><dd>5,100<span/);
    expect(html).not.toContain("4,101");
  });
  it("says Not ranked yet when the day before has no rank or is last Season's", () => {
    const old = ended("2026-09-06T05:00:00Z", 5100, 1042);
    const html = render({ ...stats(), resetDayStart: null }, [old]);
    expect(html).toContain(
      '<dt>Reset rank</dt><dd class="summary-words">Not ranked yet</dd>',
    );
    expect(html).not.toContain("1,042");
  });
  it("takes trophies at last Reset from today's start, Uncertain when battles disagree", () => {
    const day = ended("2026-10-04T05:00:00Z", 5100, null, "uncertain");
    const html = render({ ...stats(), resetDayStart: day.start }, [day]);
    expect(html).toMatch(
      /<dt>Reset trophies<\/dt><dd>5,100<span class="day-mark day-mark-gap" title="Uncertain">/,
    );
  });
  it("names unknown stars only when some are unknown", () => {
    const side = { count: 4, stars: [0, 1, 1, 1], perDay: 60, perBattle: 15 };
    const summary = (unknown: number | null) =>
      renderToStaticMarkup(
        createElement(SeasonSummary, {
          title: "Season",
          rank: ["Final rank", "1"],
          trophies: ["Final trophies", "5,000"],
          attack: { ...side, unknown },
          defense: { ...side, unknown: 0 },
        }),
      );
    expect(summary(0)).not.toContain("unknown");
    expect(summary(null)).not.toContain("unknown");
    expect(summary(1)).toContain("Stars unknown for 1 attack.");
    expect(summary(1)).toContain("25.0%<small>1 of 4</small>");
  });
});
