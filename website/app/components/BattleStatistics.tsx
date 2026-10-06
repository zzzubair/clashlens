import { useState } from "react";
import { battleStatistics, type BattlePeriod } from "../lib/battle-statistics";
import type { PlayerPage } from "../lib/contracts";
import { SeasonSummary, per } from "./SeasonSummary";

const dateFormatter = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "short",
  timeZone: "UTC",
});
const date = (time: number) => dateFormatter.format(time).replace("Sept", "Sep");

// The current Season's summary. Rank is Clash Lens's board at the latest Reset
// this Season, from the day that Reset ended, because the page has no live rank.
export function BattleStatistics({
  player,
  now,
  trophies,
}: {
  player: PlayerPage;
  now: number;
  trophies: string;
}) {
  const [period, setPeriod] = useState<BattlePeriod>("season");
  const stats = battleStatistics(player, period, now);
  const elapsed = Math.round((stats.today - stats.seasonStart) / 86_400_000) + 1;
  const lastDay = stats.today - 86_400_000;
  const rank =
    lastDay >= stats.seasonStart
      ? [...player.seasonDays, ...player.recentDays].find(
          (day) => Date.parse(day.period.split(" – ")[0]) === lastDay,
        )?.resetRank
      : null;
  const side = ({
    count,
    stars,
    trophies,
    finishedTrophies,
    activeDays,
  }: typeof stats.attack) => ({
    count,
    stars,
    unknown: 0,
    trophies,
    perDay: per(finishedTrophies, activeDays),
  });
  const flags = [
    stats.incomplete || stats.daysSaved < stats.daysExpected
      ? "Some battles missing"
      : "",
    stats.attack.disputed || stats.defense.disputed ? "Conflicting reports" : "",
  ].filter(Boolean);
  return (
    <SeasonSummary
      title="Season summary"
      meta={[
        date(stats.start) === date(stats.today)
          ? date(stats.today)
          : `${date(stats.start)} – ${date(stats.today)}`,
        `${stats.daysSaved} of ${stats.daysExpected} days saved`,
        ...flags,
      ].join(" · ")}
      controls={
        <label>
          Showing{" "}
          <select
            value={period}
            onChange={(event) => setPeriod(event.target.value as BattlePeriod)}
          >
            <option value="season">This Season</option>
            {[7, 14].map((days) => (
              <option key={days} value={days}>
                {`Last ${days} days${elapsed < days ? ` (${elapsed} so far)` : ""}`}
              </option>
            ))}
          </select>
        </label>
      }
      rank={[
        "Rank at last Reset",
        rank == null ? "Not ranked yet" : rank.toLocaleString("en-GB"),
      ]}
      trophies={["Trophies now", trophies]}
      attack={side(stats.attack)}
      defense={side(stats.defense)}
    />
  );
}
