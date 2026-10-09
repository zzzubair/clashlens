import { seasonStartAt } from "../components/SeasonReread";
import type { PlayerPage, RankedBattleEvent } from "./contracts";
import { dayEvidence, presentDay } from "./player-lookup-text";

export type BattlePeriod = "7" | "14" | "season";
const DAY_MS = 86_400_000;
const RESET_MS = 5 * 3_600_000;

export function currentSeasonStart(player: PlayerPage, now: number) {
  const anchor = player.season ? Date.parse(player.season.anchor) : NaN;
  return anchor <= now && now < anchor + 28 * DAY_MS ? anchor : seasonStartAt(now);
}

// Use recorded battles, never daily trophy adjustments or unplayed defenses.
// Only finished Legend days count, never today, which is still being played.
// Each battle counts on the saved Legend day it belongs to. Counts and stars
// use every finished day; averages leave out Uncertain and not-signed-up days,
// and per-day averages divide by every other finished day, battles or not.
function summarize(
  events: (readonly [RankedBattleEvent, number])[],
  averaged: Set<number>,
) {
  const stars = [0, 0, 0, 0];
  let trophies = 0;
  let averagedTrophies = 0;
  let averagedCount = 0;
  for (const [event, day] of events) {
    stars[event.stars]++;
    trophies += Math.abs(event.trophyChange);
    if (averaged.has(day)) {
      averagedTrophies += Math.abs(event.trophyChange);
      averagedCount++;
    }
  }
  const per = (by: number) => (by === 0 ? null : averagedTrophies / by);
  return {
    count: events.length,
    stars,
    trophies,
    perDay: per(averaged.size),
    perBattle: per(averagedCount),
    disputed: events.some(([event]) => event.perspectiveDisagreement),
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
  const statuses = [...days].map(
    ([time, day]) => [time, presentDay(dayEvidence(day), false).status] as const,
  );
  const averaged = new Set(
    statuses
      .filter(([, status]) => status !== "Uncertain" && status !== "Not enrolled")
      .map(([time]) => time),
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
    today,
    seasonStart,
    daysSaved: days.size,
    daysExpected: Math.round((today - start) / DAY_MS),
    incomplete: [...days.values()].some((day) => !day.battlesComplete),
    uncertainDays: statuses.filter(([, status]) => status === "Uncertain").length,
    attack: summarize(events("offenseEvents"), averaged),
    defense: summarize(events("defenseEvents"), averaged),
  };
}
