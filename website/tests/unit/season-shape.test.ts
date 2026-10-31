import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { BattleStatistics } from "../../app/components/BattleStatistics";
import type { PlayerPage, RankedDaySummary, SeasonWeek } from "../../app/lib/contracts";
import { dayEnd } from "../../app/lib/player-lookup-text";
import { gapLabel, logGaps, runLines, seasonShape } from "../../app/lib/season-shape";
import { worstCasePlayer } from "../fixtures/player-worst-case";

// The Season of 5 October 2026; "now" is Wednesday 28 October, Day 24.
const SEASON = Date.parse("2026-10-05T05:00:00Z");
const DAY = 86_400_000;
const NOW = SEASON + 23 * DAY + 7 * 3_600_000;

type Played = { attacks: number; defenses: number; start: number; change: number };
const UNPLAYED: Played = { attacks: 0, defenses: 0, start: 5000, change: 0 };

function day(
  number: number,
  { attacks, defenses, start, change }: Played,
): RankedDaySummary {
  const begins = SEASON + (number - 1) * DAY;
  return {
    dayNumber: number,
    label: "Legend day",
    period: `${new Date(begins).toISOString()} – ${new Date(begins + DAY).toISOString()}`,
    state: number === 24 ? "Live" : "Complete",
    startTrophies: start,
    offense: { attacks, threeStars: 0, trophyGain: Math.max(change, 0) },
    defense: { defenses, threeStarsAgainst: 0, trophyLoss: Math.max(-change, 0) },
    trophyChange: change,
    offenseEvents: [],
    defenseEvents: [],
    completeness: { state: "complete", reason: "" },
    battlesComplete: true,
    uncertainty: [],
  };
}

const LEAGUES = { L: "legend_i", B: "below_legend_i" } as const;
// One letter a week: L Legend I, B below it. `entries` says how each Legend I
// week began.
function weeks(pattern: string, entries: SeasonWeek["entry"][]): SeasonWeek[] {
  return [...pattern].map((letter, index) => ({
    start: new Date(SEASON + index * 7 * DAY).toISOString(),
    league: LEAGUES[letter as "L" | "B"],
    entry: letter === "L" ? entries[index] : null,
  }));
}

function player(days: RankedDaySummary[], seasonWeeks?: SeasonWeek[]): PlayerPage {
  const newest = [...days].reverse();
  return {
    ...worstCasePlayer("#2PP", NOW),
    season: {
      id: String(SEASON / 1000),
      anchor: new Date(SEASON).toISOString(),
      currentDayNumber: 24,
      dayCount: 28,
      anchorSource: "weekly_league_record",
      anchorObservedAt: new Date(SEASON).toISOString(),
    },
    seasonWeeks,
    currentDay: newest.find((saved) => saved.state === "Live") ?? null,
    recentDays: newest,
    seasonDays: newest,
  };
}

// Days `from` to `to`, each 8 attacks and 8 defenses, gaining `step` a day
// from `start`.
function playedDays(from: number, to: number, start: number, step: number) {
  return Array.from({ length: to - from + 1 }, (_, index) =>
    day(from + index, {
      attacks: 8,
      defenses: 8,
      start: start + index * step,
      change: step,
    }),
  );
}

const summary = (saved: PlayerPage) =>
  renderToStaticMarkup(createElement(BattleStatistics, { player: saved, now: NOW }));

describe("a partial Season", () => {
  it("leaves at a Monday Reset and comes back on a Wednesday sign-up", () => {
    // Days 1-7 in Legend I, dropped at the 12 October Reset, promoted back
    // at the 19 October Reset, signed up Wednesday 21 October with 2 attacks.
    const saved = player(
      [
        ...playedDays(1, 7, 5000, -45),
        // The API marks a finished day of fewer than 8 attacks and 8
        // defenses as one that may have lost battles.
        { ...day(15, UNPLAYED), battlesComplete: false },
        { ...day(16, UNPLAYED), battlesComplete: false },
        day(17, { attacks: 2, defenses: 2, start: 5000, change: 31 }),
        ...playedDays(18, 24, 5031, 12),
      ],
      weeks("LBLL", ["season_start", null, "promoted", "stayed"]),
    );
    const shape = seasonShape(saved, NOW);

    expect(shape.partial).toBe(true);
    expect(shape.outsideNow).toBe(false);
    expect(shape.kinds.slice(0, 24).join(" ")).toBe(
      [
        ...Array(7).fill("legend"),
        ...Array(7).fill("outside"),
        "waiting",
        "waiting",
        ...Array(8).fill("legend"),
      ].join(" "),
    );
    expect(runLines(shape)).toEqual([
      "Days 1–7 · dropped to Legend II at the Mon 12 Oct Reset",
      "From Day 15 · promoted at the Mon 19 Oct Reset · signed up Wed 21 Oct (Day 17)",
    ]);
    expect(logGaps(shape).map((gap) => gapLabel(shape, gap))).toEqual([
      "Mon 12 Oct – Sun 18 Oct · Not in Legend I, not tracked",
      "Mon 19 Oct – Tue 20 Oct · In Legend I, not signed up yet · 5,000",
    ]);
    // Days 1-7 and 17-23 are finished Legend I days; 15 and 16 are before
    // the sign-up and 8-14 outside Legend I, so none of them is missing.
    expect(shape.legendDays).toEqual({ expected: 14, saved: 14 });
    expect(summary(saved)).toContain('aria-live="polite">14 Legend I days</p>');
    expect(summary(saved)).not.toContain("Some battles may be missing");
    // The 2-attack sign-up day itself may have lost battles, as any short day can.
    const short = {
      ...saved,
      seasonDays: saved.seasonDays.map((kept) =>
        kept.dayNumber === 17 ? { ...kept, battlesComplete: false } : kept,
      ),
    };
    expect(summary({ ...short, recentDays: short.seasonDays })).toContain(
      "14 Legend I days · Some battles may be missing",
    );
  });

  it("keeps a dropped player's last Legend I rank and total", () => {
    const days = playedDays(1, 7, 4992, -44);
    days[6] = { ...days[6], resetRank: 12411 };
    const saved = player(days, weeks("LBBB", ["season_start"]));
    const shape = seasonShape(saved, NOW);

    expect(shape.outsideNow).toBe(true);
    expect(runLines(shape)).toEqual([
      "Days 1–7 · dropped to Legend II at the Mon 12 Oct Reset",
    ]);
    expect(logGaps(shape).map((gap) => gapLabel(shape, gap))).toEqual([
      "Mon 12 Oct – now · Not in Legend I, not tracked",
    ]);
    // No Legend day follows the drop and no weekly raise applies: the last
    // day ends on its start plus its change, however long ago it was.
    expect(dayEnd(shape.lastLegendDay!, saved, NOW).trophies).toBe(4684);
    const html = summary(saved);
    expect(html).toContain('aria-live="polite">7 Legend I days</p>');
    expect(html).toContain("Rank when they left");
    expect(html).toContain("12,411");
    expect(html).toContain("Trophies when they left");
    expect(html).toContain("4,684");
  });

  it("joins mid-Season at a Monday Reset", () => {
    const saved = player(
      playedDays(8, 24, 5000, 9),
      weeks("BLLL", [null, "promoted", "stayed", "stayed"]),
    );
    const shape = seasonShape(saved, NOW);

    expect(runLines(shape)).toEqual(["From Day 8 · promoted at the Mon 12 Oct Reset"]);
    expect(shape.legendDays).toEqual({ expected: 16, saved: 16 });
    expect(summary(saved)).toContain('aria-live="polite">16 Legend I days</p>');
  });

  it("keeps a day with unknown numbers as a Legend I day that may be missing", () => {
    // Promoted at the 12 October Reset; Day 8's numbers never arrived.
    const unknown: RankedDaySummary = {
      ...day(8, UNPLAYED),
      startTrophies: null,
      offense: { attacks: null, threeStars: null, trophyGain: null },
      defense: { defenses: null, threeStarsAgainst: null, trophyLoss: null },
      trophyChange: null,
      battlesComplete: false,
    };
    const saved = player(
      [unknown, ...playedDays(9, 24, 5000, 9)],
      weeks("BLLL", [null, "promoted", "stayed", "stayed"]),
    );
    const shape = seasonShape(saved, NOW);

    expect(shape.kinds[7]).toBe("legend");
    expect(shape.legendDays).toEqual({ expected: 16, saved: 16 });
    expect(summary(saved)).toContain("16 Legend I days · Some battles may be missing");
  });

  it("says a missing Legend I day is missing", () => {
    const days = playedDays(8, 24, 5000, 9).filter((saved) => saved.dayNumber !== 10);
    const saved = player(days, weeks("BLLL", [null, "promoted", "stayed", "stayed"]));

    expect(summary(saved)).toContain(
      "16 Legend I days · 1 day missing · Some battles may be missing",
    );
  });

  it("leaves a whole Season in Legend I as it was", () => {
    const saved = player(
      playedDays(1, 24, 5000, 7),
      weeks("LLLL", ["season_start", "stayed", "stayed", "stayed"]),
    );
    const shape = seasonShape(saved, NOW);

    expect(shape.partial).toBe(false);
    expect(logGaps(shape)).toEqual([]);
    expect(summary(saved)).not.toContain("season-summary-meta");
  });

  it("reads nothing into a Season the record has not decided", () => {
    const saved = player(playedDays(1, 7, 5000, -45));
    const shape = seasonShape(saved, NOW);

    expect(shape.partial).toBe(false);
    expect(shape.kinds.slice(7, 24).every((kind) => kind === "unrecorded")).toBe(true);
  });
});
