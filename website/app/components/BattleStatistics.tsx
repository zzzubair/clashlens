import { battleStatistics } from "../lib/battle-statistics";
import type { PlayerPage } from "../lib/contracts";
import { dayEnd } from "../lib/player-lookup-text";
import { DayMark } from "./DayStatus";
import { SeasonSummary } from "./SeasonSummary";

const DAY = 86_400_000;

// The current Season's summary, from finished Legend days only. Rank is Clash
// Lens's board at the latest Reset this Season and trophies are that day's
// end, both from the day that Reset ended; nothing here is live. Until that
// Reset's board ranks the player (it publishes by 05:30), both come from the
// day before, if that day is in this Season and was ranked.
export function BattleStatistics({
  player,
  now,
  menu,
}: {
  player: PlayerPage;
  now: number;
  menu?: React.ReactNode;
}) {
  const stats = battleStatistics(player, "season", now);
  const lastDay = stats.today - DAY;
  const dayFrom = (start: number) =>
    start >= stats.seasonStart
      ? [...player.seasonDays, ...player.recentDays].find(
          (day) => Date.parse(day.period.split(" – ")[0]) === start,
        )
      : undefined;
  const latest = dayFrom(lastDay);
  const before = latest?.resetRank == null ? dayFrom(lastDay - DAY) : undefined;
  const last = before?.resetRank != null ? before : latest;
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
          : [`${stats.daysSaved} of ${stats.daysExpected} days saved`, ...flags].join(
              " · ",
            )
      }
      controls={menu}
      rank={[
        "Reset rank",
        last?.resetRank == null
          ? "Not ranked yet"
          : last.resetRank.toLocaleString("en-GB"),
      ]}
      trophies={[
        "Reset trophies",
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
