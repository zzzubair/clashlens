import { useState } from "react";
import { battleStatistics, type BattlePeriod } from "../lib/battle-statistics";
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
// this Season, because the page has no live rank.
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
  const seasonStart =
    period === "season" ? stats.start : battleStatistics(player, "season", now).start;
  const rank = [...player.seasonDays, ...player.recentDays]
    .filter((day) => Date.parse(day.period.split(" – ")[0]) >= seasonStart)
    .sort((a, b) => b.period.localeCompare(a.period))
    .find((day) => day.resetRank != null)?.resetRank;
  const side = ({ count, stars, trophies, finishedTrophies }: typeof stats.attack) => ({
    count,
    stars,
    unknown: 0,
    trophies,
    perDay: per(finishedTrophies, stats.finishedDays),
  });
  return (
    <SeasonSummary
      title="Season summary"
      controls={
        <label>
          Showing{" "}
          <select
            value={period}
            onChange={(event) => setPeriod(event.target.value as BattlePeriod)}
          >
            <option value="season">This Season</option>
            <option value="7">Last 7 days</option>
            <option value="14">Last 14 days</option>
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
    >
      <p className="section-note" aria-live="polite">
        {date(stats.start)} to {date(stats.end)}: {stats.daysSaved} of{" "}
        {stats.daysExpected} Legend days saved. Per-day averages leave out today; losses
        leave out the automatic loss at Reset.
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
