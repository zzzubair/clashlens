import { seasonStartAt } from "../components/SeasonReread";
import type { PlayerPage, RankedBattleEvent } from "./contracts";

export type BattlePeriod = "7" | "14" | "season";
const DAY_MS = 86_400_000;
const RESET_MS = 5 * 3_600_000;

export function currentSeasonStart(player: PlayerPage, now: number) {
  const anchor = player.season ? Date.parse(player.season.anchor) : NaN;
  return anchor <= now && now < anchor + 28 * DAY_MS ? anchor : seasonStartAt(now);
}

// Use recorded battles, never daily trophy adjustments or unplayed defenses.
// Only finished Legend days count, never today, which is still being played.
// Each battle counts on the saved Legend day it belongs to. Per-day averages
// divide by the days with a battle on that side, so shielded or unplayed days
// don't pull them down (the owner's rule: 280 trophies over 5 played days of 7
// is 56 a day, not 40).
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
  const side = (key: "offenseEvents" | "defenseEvents") => {
    const events = new Map<string, RankedBattleEvent>();
    let played = 0;
    for (const [, saved] of [...days].sort(([a], [b]) => a - b)) {
      const before = events.size;
      for (const event of saved[key]) {
        const time = Date.parse(event.battleTimestamp);
        if (time < start || time > now || events.has(event.battleId)) continue;
        events.set(event.battleId, event);
      }
      if (events.size > before) played++;
    }
    return summarize([...events.values()], played);
  };
  return {
    start,
    today,
    seasonStart,
    daysSaved: days.size,
    daysExpected: Math.round((today - start) / DAY_MS),
    incomplete: [...days.values()].some((day) => !day.battlesComplete),
    attack: side("offenseEvents"),
    defense: side("defenseEvents"),
  };
}
