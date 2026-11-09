import { UNKNOWN_END, VERIFIED } from "../fixtures/player-route-data";
import { describe, expect, it } from "vitest";
import type { PlayerPage, RankedDaySummary } from "../../app/lib/contracts";
import {
  dayPresentation,
  selectPlayerHistory,
  showsCurrentSeason,
} from "../../app/lib/player-lookup-text";

const SEASON_START = Date.parse("2026-09-07T05:00:00Z");

// A day before tracking began: no Reset readings and no battles.
const emptyDay = (number: number): RankedDaySummary => {
  const start = SEASON_START + (number - 1) * 86_400_000;
  return {
    dayNumber: number,
    label: "Ranked day",
    start: `${new Date(start).toISOString()}`,
    end: `${new Date(start + 86_400_000).toISOString()}`,
    seasonId: null,
    presentation: {
      ...VERIFIED,
      status: "not_enrolled",
      battle_net: null,
      reason_codes: ["not_enrolled"],
    },
    displayEnd: UNKNOWN_END,
    state: "Partial",
    offense: { attacks: 0, threeStars: 0, trophyGain: null },
    defense: { defenses: 0, threeStarsAgainst: 0, trophyLoss: null },
    trophyChange: null,
    offenseEvents: [],
    defenseEvents: [],
    completeness: { state: "partial", reason: "Partial" },
  };
};

describe("days before a late joiner signed up", () => {
  it("say the player was not enrolled instead of a missing result", () => {
    expect(dayPresentation(emptyDay(3))).toEqual({
      status: "Not enrolled",
      reasons: ["The player had not signed up for this Season yet."],
      battleNet: null,
      cautions: [],
    });
  });

  it("leave other incomplete days unchanged", () => {
    expect(
      dayPresentation({
        ...emptyDay(3),
        presentation: {
          ...VERIFIED,
          status: "uncertain",
          reason_codes: ["missing_start_baseline"],
        },
      }).status,
    ).toBe("Uncertain");
  });

  it("stay in the history when the player was also not in Legend I", () => {
    const player = {
      season: {
        id: String(SEASON_START / 1000),
        anchor: "2026-09-07T05:00:00Z",
        currentDayNumber: 10,
        dayCount: 28,
        end: "2026-11-02T05:00:00Z",
        initialTrophies: 5000,
        anchorSource: "official_league_history",
        anchorObservedAt: "2026-09-07T05:10:00Z",
      },
      currentDay: null,
      recentDays: [emptyDay(3), emptyDay(2)],
      seasonDays: [],
    } as unknown as PlayerPage;
    const history = selectPlayerHistory({
      ...player,
      historyDays: [player.recentDays[0]],
      historyDayNumbers: { [player.recentDays[0].start]: 3 },
    });
    expect(history.map(({ seasonDay }) => seasonDay)).toEqual(["Day 3"]);
    expect(dayPresentation(history[0].day).status).toBe("Not enrolled");
  });

  it("give a player outside Legend I no current Season when they never played a Legend day", () => {
    const lookup = { tag: "#L82PYUC9J", state: "not_in_legend" } as const;
    const notEnrolled = emptyDay(3);
    const played = { ...emptyDay(4), offenseEvents: [{}] } as RankedDaySummary;
    expect(showsCurrentSeason(lookup, [{ day: notEnrolled }])).toBe(false);
    expect(showsCurrentSeason(lookup, [{ day: notEnrolled }, { day: played }])).toBe(
      true,
    );
    expect(showsCurrentSeason({ ...lookup, state: "tracking" }, [])).toBe(true);
  });
});
