import { describe, expect, it } from "vitest";
import type { PlayerPage, RankedDaySummary } from "../../app/lib/contracts";
import {
  dayEvidence,
  presentDay,
  selectPlayerHistory,
} from "../../app/lib/player-lookup-text";

const DAY = {
  net: null,
  state: "Partial",
  coverage: "partial",
  codes: ["missing_start_baseline", "not_enrolled"],
  attackGain: null,
  defenseLoss: null,
  attacks: 0,
  defenses: 0,
};

const SEASON_START = Date.parse("2026-09-07T05:00:00Z");

// A day before tracking began: no Reset readings and no battles.
const emptyDay = (number: number, codes: string[]): RankedDaySummary => {
  const start = SEASON_START + (number - 1) * 86_400_000;
  return {
    dayNumber: number,
    label: "Ranked day",
    period: `${new Date(start).toISOString()} – ${new Date(start + 86_400_000).toISOString()}`,
    state: "Partial",
    offense: { attacks: 0, threeStars: 0, trophyGain: null },
    defense: { defenses: 0, threeStarsAgainst: 0, trophyLoss: null },
    trophyChange: null,
    offenseEvents: [],
    defenseEvents: [],
    completeness: { state: "partial", reason: "Partial" },
    uncertainty: codes,
  };
};

describe("days before a late joiner signed up", () => {
  it("say the player was not enrolled instead of a missing result", () => {
    expect(presentDay(DAY, false)).toEqual({
      status: "Not enrolled",
      reasons: ["The player had not signed up for this Season yet."],
      battleNet: null,
    });
  });

  it("leave other incomplete days unchanged", () => {
    expect(presentDay({ ...DAY, codes: ["missing_start_baseline"] }, false).status).toBe(
      "Result unknown",
    );
  });

  it("stay in the history when the player was also not in Legend I", () => {
    const player = {
      season: {
        id: String(SEASON_START / 1000),
        anchor: "2026-09-07T05:00:00Z",
        currentDayNumber: 10,
        dayCount: 28,
        anchorSource: "official_league_history",
        anchorObservedAt: "2026-09-07T05:10:00Z",
      },
      currentDay: null,
      recentDays: [
        emptyDay(3, ["missing_start_baseline", "player_not_eligible", "not_enrolled"]),
        emptyDay(2, ["missing_start_baseline", "player_not_eligible"]),
      ],
      seasonDays: [],
    } as unknown as PlayerPage;
    const history = selectPlayerHistory(player, Date.parse("2026-09-16T06:00:00Z"));
    expect(history.map(({ seasonDay }) => seasonDay)).toEqual(["Day 3"]);
    expect(presentDay(dayEvidence(history[0].day), false).status).toBe("Not enrolled");
  });
});
