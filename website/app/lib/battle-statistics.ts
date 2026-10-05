import { seasonStartAt } from "../components/SeasonReread";
import type { PlayerPage, RankedBattleEvent } from "./contracts";

export type BattlePeriod = "7" | "14" | "season";
const DAY_MS = 86_400_000;
const RESET_MS = 5 * 3_600_000;

// Use recorded battles, never daily trophy adjustments or unplayed defenses.
function summarize(events: RankedBattleEvent[]) {
  const count = events.length;
  const stars = [0, 0, 0, 0];
  let destruction = 0;
  let trophies = 0;
  for (const event of events) {
    stars[event.stars]++;
    destruction += event.destructionPercentage;
    trophies += Math.abs(event.trophyChange);
  }
  return {
    count,
    stars,
    averageStars: count ? (stars[1] + 2 * stars[2] + 3 * stars[3]) / count : null,
    averageDestruction: count ? destruction / count : null,
    averageTrophies: count ? trophies / count : null,
    tripleRate: count ? (100 * stars[3]) / count : null,
    holdRate: count ? (100 * (count - stars[3])) / count : null,
    disputed: events.some((event) => event.perspectiveDisagreement),
  };
}

export function battleStatistics(player: PlayerPage, period: BattlePeriod, now: number) {
  const today = Math.floor((now - RESET_MS) / DAY_MS) * DAY_MS + RESET_MS;
  const anchor = player.season ? Date.parse(player.season.anchor) : NaN;
  const seasonStart =
    anchor <= now && now < anchor + 28 * DAY_MS ? anchor : seasonStartAt(now);
  const start = period === "season" ? seasonStart : today - (Number(period) - 1) * DAY_MS;
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
      [...days.values()]
        .flatMap((day) => day[side])
        .filter((event) => {
          const time = Date.parse(event.battleTimestamp);
          return time >= start && time <= now;
        })
        .map((event) => [event.battleId, event]),
    ).values(),
  ];
  return {
    start,
    end: now,
    daysSaved: days.size,
    daysExpected: Math.round((today - start) / DAY_MS) + 1,
    incomplete: [...days.values()].some((day) => !day.battlesComplete),
    attack: summarize(events("offenseEvents")),
    defense: summarize(events("defenseEvents")),
  };
}
