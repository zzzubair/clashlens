import type { PlayerPage } from "../lib/contracts";
import { dayEnd } from "../lib/player-lookup-text";
import { DayMark } from "./DayStatus";
import { SeasonSummary } from "./SeasonSummary";

// The current Season's summary, from finished Legend days only. Rank is Clash
// Lens's board at the latest Reset this Season and trophies are that day's
// end, both from the day that Reset ended; nothing here is live. Until that
// Reset's board ranks the player (it publishes by 05:30), both come from the
// day before, if that day is in this Season and was ranked.
export function BattleStatistics({
  player,
  menu,
  heatMap,
}: {
  player: PlayerPage;
  now: number;
  menu?: React.ReactNode;
  heatMap?: React.ReactNode;
}) {
  const stats = player.battleStatistics!.season;
  const shape = player.seasonShape;
  const left = shape?.outsideNow ? shape.lastLegendDay : null;
  const last = [...player.seasonDays, ...player.recentDays].find(
    (day) => day.start === stats.resetDayStart,
  );
  const end = last ? dayEnd(last) : null;
  const side = ({ count, stars, perDay, perBattle }: typeof stats.attack) => ({
    count,
    stars,
    unknown: 0,
    perDay,
    perBattle,
  });
  const { daysExpected: expected, daysSaved: saved, incomplete } = stats;
  const flags = [
    incomplete || saved < expected ? "Some battles may be missing" : "",
    stats.attack.disputed || stats.defense.disputed ? "Conflicting reports" : "",
  ].filter(Boolean);
  // Only what the Clasher needs to weigh the numbers: days with no saved
  // result, and how many days a partial Season had in Legend I.
  const missing = expected - saved;
  const count = [
    shape?.partial ? `${expected} Legend I ${expected === 1 ? "day" : "days"}` : "",
    missing > 0 ? `${missing} ${missing === 1 ? "day" : "days"} missing` : "",
  ];
  return (
    <SeasonSummary
      title="Season summary"
      meta={
        expected === 0
          ? "No finished Legend days yet this Season"
          : [...count, ...flags].filter(Boolean).join(" · ") || undefined
      }
      controls={menu}
      rank={[
        left ? "Rank when they left" : "Reset rank",
        last?.resetRank == null
          ? "Not ranked yet"
          : last.resetRank.toLocaleString("en-GB"),
      ]}
      trophies={[
        left ? "Trophies when they left" : "Reset trophies",
        expected === 0
          ? "No finished day yet"
          : end?.trophies == null
            ? "Unavailable"
            : end.trophies.toLocaleString("en-GB"),
        end?.trophies == null ? null : <DayMark status={end.status} />,
      ]}
      attack={side(stats.attack)}
      defense={side(stats.defense)}
      heatMap={heatMap}
    />
  );
}
