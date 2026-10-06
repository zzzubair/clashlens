import { seasonStartAt } from "../components/SeasonReread";
import type { PlayerPage, RankedBattleEvent } from "./contracts";

export type BattlePeriod = "7" | "14" | "season";
const DAY_MS = 86_400_000;
const RESET_MS = 5 * 3_600_000;

// Use recorded battles, never daily trophy adjustments or unplayed defenses.
// Daily totals leave out today, which is still being played, and count each
// battle on the saved Legend day it belongs to. Per-day averages divide by the
// finished days with battles on that side, so shielded days do not lower them.
function summarize(events: (readonly [RankedBattleEvent, number])[], today: number) {
  const stars = [0, 0, 0, 0];
  let trophies = 0;
  let finishedTrophies = 0;
  const finishedDays = new Set<number>();
  for (const [event, day] of events) {
    stars[event.stars]++;
    trophies += Math.abs(event.trophyChange);
    if (day < today) {
      finishedTrophies += Math.abs(event.trophyChange);
      finishedDays.add(day);
    }
  }
  return {
    count: events.length,
    stars,
    trophies,
    finishedTrophies,
    battleDays: finishedDays.size,
    disputed: events.some(([event]) => event.perspectiveDisagreement),
  };
}

export function currentSeasonStart(player: PlayerPage, now: number) {
  const anchor = player.season ? Date.parse(player.season.anchor) : NaN;
  return anchor <= now && now < anchor + 28 * DAY_MS ? anchor : seasonStartAt(now);
}

// Recent windows stay inside the current Season.
export function battleStatistics(player: PlayerPage, period: BattlePeriod, now: number) {
  const today = Math.floor((now - RESET_MS) / DAY_MS) * DAY_MS + RESET_MS;
  const seasonStart = currentSeasonStart(player, now);
  const start =
    period === "season"
      ? seasonStart
      : Math.max(seasonStart, today - (Number(period) - 1) * DAY_MS);
  const days = new Map(
    [
      ...player.recentDays,
      ...player.seasonDays,
      ...(player.currentDay ? [player.currentDay] : []),
    ]
      .filter((day) => {
        const time = Date.parse(day.period.split(" – ")[0]);
        return time >= start && time <= today;
      })
      .map((day) => [Date.parse(day.period.split(" – ")[0]), day]),
  );
  const events = (side: "offenseEvents" | "defenseEvents") => [
    ...new Map(
      [...days]
        .flatMap(([day, saved]) => saved[side].map((event) => [event, day] as const))
        .filter(([event]) => {
          const time = Date.parse(event.battleTimestamp);
          return time >= start && time <= now;
        })
        .map(([event, day]) => [event.battleId, [event, day] as const]),
    ).values(),
  ];
  return {
    start,
    end: now,
    today,
    daysSaved: days.size,
    daysExpected: Math.round((today - start) / DAY_MS) + 1,
    incomplete: [...days.values()].some((day) => !day.battlesComplete),
    attack: summarize(events("offenseEvents"), today),
    defense: summarize(events("defenseEvents"), today),
  };
}
