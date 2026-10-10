import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { battleStatistics } from "../../app/lib/battle-statistics";
import type {
  PlayerPage,
  RankedBattleEvent,
  RankedDaySummary,
} from "../../app/lib/contracts";
import { worstCasePlayer } from "../fixtures/player-worst-case";

// The same saved days the Python crew boards test reads
// (python/tests/test_crew_boards_postgres.py): both must give these totals.
type Battle = {
  lens: "offense" | "defense";
  battle_id: string;
  battle_timestamp: string;
  opponent: { tag: string; name: string | null };
  stars: number;
  destruction_percentage: number;
  trophy_change: number;
};
type Side = { total: number; days: number; battles: number };
const fixture = JSON.parse(
  readFileSync(
    fileURLToPath(new URL("../../../testdata/crew-boards-parity.json", import.meta.url)),
    "utf8",
  ),
) as {
  now: string;
  season_start: string;
  season_day: number;
  players: {
    tag: string;
    days: { start: string; partial_reasons: string[]; battles: Battle[] }[];
    expected: Record<"season" | "week", { attack: Side; defense: Side }>;
  }[];
};
const NOW = Date.parse(fixture.now);
const DAY = 86_400_000;

function event(battle: Battle): RankedBattleEvent {
  return {
    battleId: battle.battle_id,
    stars: battle.stars,
    destructionPercentage: battle.destruction_percentage,
    trophyChange: battle.trophy_change,
    battleTimestamp: battle.battle_timestamp,
    opponent: battle.opponent,
    perspectiveDisagreement: false,
  };
}

function page(saved: (typeof fixture.players)[number]): PlayerPage {
  const days = saved.days.map((day): RankedDaySummary => {
    const start = Date.parse(day.start);
    const side = (lens: Battle["lens"]) =>
      day.battles.filter((battle) => battle.lens === lens).map(event);
    return {
      dayNumber: null,
      label: "Legend day",
      period: `${new Date(start).toISOString()} – ${new Date(start + DAY).toISOString()}`,
      state: start + DAY > NOW ? "Live" : "Complete",
      offense: { attacks: null, threeStars: null, trophyGain: null },
      defense: { defenses: null, threeStarsAgainst: null, trophyLoss: null },
      trophyChange: null,
      offenseEvents: side("offense"),
      defenseEvents: side("defense"),
      completeness: { state: "complete", reason: "" },
      battlesComplete: true,
      uncertainty: day.partial_reasons,
    };
  });
  return {
    ...worstCasePlayer(saved.tag, NOW),
    season: {
      id: "1783918800",
      anchor: fixture.season_start,
      currentDayNumber: fixture.season_day,
      dayCount: 28,
      anchorSource: "official_league_history",
      anchorObservedAt: fixture.season_start,
    },
    currentDay: days.find((day) => day.state === "Live") ?? null,
    recentDays: days,
    seasonDays: days,
  };
}

describe("crew boards and the player page agree", () => {
  for (const saved of fixture.players) {
    it(`gives ${saved.tag} the same totals and Legend days`, () => {
      for (const [period, key] of [
        ["season", "season"],
        ["7", "week"],
      ] as const) {
        const stats = battleStatistics(page(saved), period, NOW);
        // The boards send the total and the Legend days; the page shows
        // their ratio.
        const side = (summary: typeof stats.attack) => ({
          total: summary.trophies,
          perDay: summary.perDay,
          battles: summary.count,
        });
        const expected = (want: Side) => ({
          total: want.total,
          perDay: want.total / want.days,
          battles: want.battles,
        });
        expect({ attack: side(stats.attack), defense: side(stats.defense) }).toEqual({
          attack: expected(saved.expected[key].attack),
          defense: expected(saved.expected[key].defense),
        });
      }
    });
  }
});
