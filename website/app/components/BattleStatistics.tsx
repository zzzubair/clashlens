import { battleStatistics } from "../lib/battle-statistics";
import type { PlayerPage } from "../lib/contracts";
import { dayEnd } from "../lib/player-lookup-text";
import { seasonShape } from "../lib/season-shape";
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
  strip,
}: {
  player: PlayerPage;
  now: number;
  menu?: React.ReactNode;
  strip?: React.ReactNode;
}) {
  const stats = battleStatistics(player, "season", now);
  const shape = seasonShape(player, now);
  // A player now outside Legend I keeps the rank and trophies they left on.
  const left = shape.outsideNow ? shape.lastLegendDay : null;
  const lastDay = left ? Date.parse(left.period.split(" – ")[0]) : stats.today - DAY;
  const dayFrom = (start: number) =>
    start >= stats.seasonStart
      ? [...player.seasonDays, ...player.recentDays].find(
          (day) => Date.parse(day.period.split(" – ")[0]) === start,
        )
      : undefined;
  const latest = dayFrom(lastDay);
  const before = latest?.resetRank == null && !left ? dayFrom(lastDay - DAY) : undefined;
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
  // A partial Season counts its Legend I days only: days outside Legend I,
  // or before the player signed up, are not missing (owner decision, 28
  // October 2026).
  const { expected, saved } = shape.partial
    ? shape.legendDays
    : { expected: stats.daysExpected, saved: stats.daysSaved };
  // Nor can a day before sign-up have lost a battle.
  const incomplete = shape.partial
    ? [...player.seasonDays, ...player.recentDays].some((day) => {
        const number =
          Math.round((Date.parse(day.period.split(" – ")[0]) - shape.seasonStart) / DAY) +
          1;
        return (
          !day.battlesComplete &&
          number < shape.today &&
          shape.kinds[number - 1] === "legend"
        );
      })
    : stats.incomplete;
  const flags = [
    incomplete || saved < expected ? "Some battles may be missing" : "",
    stats.attack.disputed || stats.defense.disputed ? "Conflicting reports" : "",
  ].filter(Boolean);
  const count = !shape.partial
    ? `${saved} of ${expected} days saved`
    : saved < expected
      ? `${saved} of ${expected} Legend I days saved`
      : `${expected} Legend I ${expected === 1 ? "day" : "days"}${flags.length ? "" : " · all saved"}`;
  return (
    <SeasonSummary
      title="Season summary"
      meta={
        expected === 0
          ? "No finished Legend days yet this Season"
          : [count, ...flags].join(" · ")
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
        lastDay < stats.seasonStart
          ? "No finished day yet"
          : end?.trophies == null
            ? "Unavailable"
            : end.trophies.toLocaleString("en-GB"),
        end?.trophies == null ? null : <DayMark status={end.status} />,
      ]}
      attack={side(stats.attack)}
      defense={side(stats.defense)}
      strip={strip}
    />
  );
}
