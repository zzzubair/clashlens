import { useState } from "react";
import { battleStatistics, type BattlePeriod } from "../lib/battle-statistics";
import type { PlayerPage } from "../lib/contracts";
import { dayEnd } from "../lib/player-lookup-text";
import { DayMark } from "./DayStatus";
import { SeasonSummary } from "./SeasonSummary";

const dateFormatter = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "short",
  timeZone: "UTC",
});
const date = (time: number) => dateFormatter.format(time).replace("Sept", "Sep");

// The current Season's summary, from finished Legend days only. Rank is Clash
// Lens's board at the latest Reset this Season and trophies are that day's
// end, both from the day that Reset ended; nothing here is live.
export function BattleStatistics({ player, now }: { player: PlayerPage; now: number }) {
  const [period, setPeriod] = useState<BattlePeriod>("season");
  const stats = battleStatistics(player, period, now);
  const finished = Math.round((stats.today - stats.seasonStart) / 86_400_000);
  const lastDay = stats.today - 86_400_000;
  const last =
    lastDay >= stats.seasonStart
      ? [...player.seasonDays, ...player.recentDays].find(
          (day) => Date.parse(day.period.split(" – ")[0]) === lastDay,
        )
      : undefined;
  // The same end of day as the Daily Legend log shows for that day.
  const end = last ? dayEnd(last, player, now) : null;
  const side = ({ count, stars, perDay, perBattle }: typeof stats.attack) => ({
    count,
    stars,
    unknown: 0,
    perDay,
    perBattle,
  });
  const flags = [
    stats.incomplete || stats.daysSaved < stats.daysExpected
      ? "Some battles may be missing"
      : "",
    stats.attack.disputed || stats.defense.disputed ? "Conflicting reports" : "",
  ].filter(Boolean);
  return (
    <SeasonSummary
      title="Season summary"
      meta={
        stats.daysExpected === 0
          ? "No finished Legend days yet this Season"
          : [
              stats.start === lastDay
                ? date(lastDay)
                : `${date(stats.start)} – ${date(lastDay)}`,
              `${stats.daysSaved} of ${stats.daysExpected} finished days saved`,
              ...flags,
            ].join(" · ")
      }
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
                {`Last ${days} days${finished < days ? ` (${finished} so far)` : ""}`}
              </option>
            ))}
          </select>
        </label>
      }
      rank={[
        "Rank at last Reset",
        last?.resetRank == null
          ? "Not ranked yet"
          : last.resetRank.toLocaleString("en-GB"),
      ]}
      trophies={[
        "Trophies at last Reset",
        lastDay < stats.seasonStart
          ? "No finished day yet"
          : end?.trophies == null
            ? "Unavailable"
            : end.trophies.toLocaleString("en-GB"),
        end?.trophies == null ? null : <DayMark status={end.status} />,
      ]}
      attack={side(stats.attack)}
      defense={side(stats.defense)}
    />
  );
}
