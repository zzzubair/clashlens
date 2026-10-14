import { describe, expect, it } from "vitest";
import type { PlayerPage, RankedDaySummary } from "../../app/lib/contracts";
import {
  dayEvidence,
  presentDay,
  selectPlayerHistory,
  showsCurrentSeason,
} from "../../app/lib/player-lookup-text";

const NOT_CONFIRMED =
  "Clash Lens has not confirmed this player was in Legend I this week, so this day may not count.";

const DAY = {
  net: 40,
  state: "Complete",
  confidence: "exact",
  coverage: "complete",
  codes: ["league_not_confirmed"],
  attackGain: 80,
  defenseLoss: 40,
  attacks: 8,
  defenses: 8,
};

const SEASON_START = Date.parse("2026-09-07T05:00:00Z");

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

describe("days in a week whose league is not confirmed", () => {
  it("say the league is not confirmed and keep their numbers", () => {
    expect(presentDay(DAY, false)).toEqual({
      status: "League not confirmed",
      reasons: [NOT_CONFIRMED],
      battleNet: 40,
    });
  });

  it("say so on today's card too, while it is in progress", () => {
    const today = presentDay({ ...DAY, state: "Live" }, true);
    expect(today.status).toBe("In progress");
    expect(today.reasons).toContain(NOT_CONFIRMED);
  });

  it("leave other days unchanged", () => {
    expect(presentDay({ ...DAY, codes: [] }, false).status).toBe("Verified");
  });

  it("keep every day Python sends in the history, battles or not", () => {
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
        emptyDay(3, ["missing_start_baseline", "league_not_confirmed"]),
        emptyDay(2, ["missing_start_baseline", "player_not_eligible"]),
      ],
      seasonDays: [],
    } as unknown as PlayerPage;
    const history = selectPlayerHistory(player, Date.parse("2026-09-16T06:00:00Z"));
    expect(history.map(({ seasonDay }) => seasonDay)).toEqual(["Day 3", "Day 2"]);
    expect(presentDay(dayEvidence(history[0].day), false).status).toBe(
      "League not confirmed",
    );
  });

  it("give a player outside Legend I a current Season only with a saved Legend day", () => {
    const lookup = { tag: "#L82PYUC9J", state: "not_in_legend" } as const;
    expect(showsCurrentSeason(lookup, [])).toBe(false);
    expect(showsCurrentSeason(lookup, [{ day: emptyDay(4, []) }])).toBe(true);
    expect(showsCurrentSeason({ ...lookup, state: "tracking" }, [])).toBe(true);
  });
});
