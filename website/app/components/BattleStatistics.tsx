import { useState } from "react";
import {
  battleStatistics,
  currentSeasonStart,
  type BattlePeriod,
} from "../lib/battle-statistics";
import type { PlayerPage } from "../lib/contracts";
import { SeasonSummary, per } from "./SeasonSummary";

const dateFormatter = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "short",
  year: "numeric",
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
  const seasonStart = currentSeasonStart(player, now);
  // A window as long as the Season so far would repeat This Season.
  const windows = (["7", "14"] as const).filter(
    (days) => stats.today - seasonStart >= Number(days) * 86_400_000,
  );
  const lastDay = stats.today - 86_400_000;
  const rank =
    lastDay >= seasonStart
      ? [...player.seasonDays, ...player.recentDays].find(
          (day) => Date.parse(day.period.split(" – ")[0]) === lastDay,
        )?.resetRank
      : null;
  const side = ({
    count,
    stars,
    trophies,
    finishedTrophies,
    battleDays,
  }: typeof stats.attack) => ({
    count,
    stars,
    unknown: 0,
    trophies,
    perDay: per(finishedTrophies, battleDays),
  });
  return (
    <SeasonSummary
      title="Season summary"
      controls={
        windows.length > 0 ? (
          <label>
            Showing{" "}
            <select
              value={period}
              onChange={(event) => setPeriod(event.target.value as BattlePeriod)}
            >
              <option value="season">This Season</option>
              {windows.map((days) => (
                <option key={days} value={days}>
                  Last {days} days
                </option>
              ))}
            </select>
          </label>
        ) : null
      }
      rank={[
        "Rank at last Reset",
        rank == null ? "Not ranked yet" : rank.toLocaleString("en-GB"),
      ]}
      trophies={["Trophies now", trophies]}
      attack={side(stats.attack)}
      defense={side(stats.defense)}
    >
      <p className="section-note" aria-live="polite">
        {date(stats.start)} to {date(stats.end)}: {stats.daysSaved} of{" "}
        {stats.daysExpected} Legend days saved.
        {stats.incomplete || stats.daysSaved < stats.daysExpected
          ? " Some battles may be missing."
          : ""}
        {stats.attack.disputed || stats.defense.disputed
          ? " Some battles have conflicting reports."
          : ""}
      </p>
    </SeasonSummary>
  );
}
