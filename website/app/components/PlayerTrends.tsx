import { currentSeasonStart } from "../lib/battle-statistics";
import type { PlayerPage } from "../lib/contracts";
import { Metric, MetricCard } from "./MetricCard";

const DAY_MS = 24 * 60 * 60 * 1000;
const RESET_MS = 5 * 60 * 60 * 1000;

export function PlayerTrends({ player, now }: { player: PlayerPage; now: number }) {
  const todayStart = Math.floor((now - RESET_MS) / DAY_MS) * DAY_MS + RESET_MS;
  // Finished days of the current Season only, so last Season never counts.
  const seasonDays = Math.round((todayStart - currentSeasonStart(player, now)) / DAY_MS);
  // Daily changes already exclude the Season reset, unlike subtracting profiles.
  // Count each day once and follow the group comparison's completeness rule.
  const recent = new Map(
    player.recentDays.map((day) => [Date.parse(day.period.split(" – ")[0]), day]),
  );
  // A 14-day card that covers the same days as the 7-day card is left out.
  const windows = [7, 14].filter((window) => window === 7 || seasonDays > 7);
  if (seasonDays <= 0) return null;
  return (
    <section className="data-section" aria-labelledby="player-trends-title">
      <h2 id="player-trends-title">Trophy trend</h2>
      <div className="metric-grid">
        {windows.map((window) => {
          const days = Math.min(window, seasonDays);
          let total = 0;
          let counted = 0;
          for (const [start, day] of recent) {
            if (
              start >= todayStart - days * DAY_MS &&
              start < todayStart &&
              (start - RESET_MS) % DAY_MS === 0 &&
              Date.parse(day.period.split(" – ")[1]) === start + DAY_MS &&
              day.completeness.state === "complete" &&
              day.trophyChange !== null
            ) {
              total += day.trophyChange;
              counted += 1;
            }
          }
          return (
            <MetricCard key={window} title={`Last ${window} days`}>
              <Metric
                label="Trophy change"
                value={
                  counted === 0
                    ? "Unavailable"
                    : `${total > 0 ? "+" : ""}${total.toLocaleString("en-GB")}`
                }
              />
              <Metric label="Days counted" value={`${counted} of ${days}`} />
            </MetricCard>
          );
        })}
      </div>
    </section>
  );
}
