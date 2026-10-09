import { seasonStartAt } from "../components/SeasonReread";
import type { PlayerPage, RankedBattleEvent } from "./contracts";
import { isLegendDay } from "./player-lookup-text";

export type BattlePeriod = "7" | "14" | "season";
const DAY_MS = 86_400_000;
const RESET_MS = 5 * 3_600_000;

export function currentSeasonStart(player: PlayerPage, now: number) {
  const anchor = player.season ? Date.parse(player.season.anchor) : NaN;
  return anchor <= now && now < anchor + 28 * DAY_MS ? anchor : seasonStartAt(now);
}

// Use recorded battles, never daily trophy adjustments or unplayed defenses.
// Only finished Legend days count, never today, which is still being played.
// Each battle counts on the saved Legend day it belongs to. Counts, stars and
// averages all use every saved finished Legend day, leaving out battle-free
// days before sign-up; per-day averages divide by those days, battles or not.
function summarize(events: RankedBattleEvent[], days: number) {
  const stars = [0, 0, 0, 0];
  let trophies = 0;
  for (const event of events) {
    stars[event.stars]++;
    trophies += Math.abs(event.trophyChange);
  }
  const per = (by: number) => (by === 0 ? null : trophies / by);
  return {
    count: events.length,
    stars,
    trophies,
    perDay: per(days),
    perBattle: per(events.length),
    disputed: events.some((event) => event.perspectiveDisagreement),
  };
}

// Recent windows are the last finished days of the current Season; early on
// they are shorter, and on the Season's first day there are none.
export function battleStatistics(player: PlayerPage, period: BattlePeriod, now: number) {
  const today = Math.floor((now - RESET_MS) / DAY_MS) * DAY_MS + RESET_MS;
  const seasonStart = currentSeasonStart(player, now);
  const start =
    period === "season"
      ? seasonStart
      : Math.max(seasonStart, today - Number(period) * DAY_MS);
  const days = new Map(
    [...player.recentDays, ...player.seasonDays]
      .filter((day) => {
        const time = Date.parse(day.period.split(" – ")[0]);
        return time >= start && time < today;
      })
      .map((day) => [Date.parse(day.period.split(" – ")[0]), day]),
  );
  const legendDays = [...days.values()].filter((day) =>
    isLegendDay(day.uncertainty, day.offenseEvents.length + day.defenseEvents.length),
  );
  const events = (side: "offenseEvents" | "defenseEvents") => [
    ...new Map(
      legendDays
        .flatMap((saved) => saved[side])
        .filter((event) => {
          const time = Date.parse(event.battleTimestamp);
          return time >= start && time <= now;
        })
        .map((event) => [event.battleId, event] as const),
    ).values(),
  ];
  return {
    start,
    today,
    seasonStart,
    daysSaved: days.size,
    daysExpected: Math.round((today - start) / DAY_MS),
    incomplete: [...days.values()].some((day) => !day.battlesComplete),
    attack: summarize(events("offenseEvents"), legendDays.length),
    defense: summarize(events("defenseEvents"), legendDays.length),
  };
}
