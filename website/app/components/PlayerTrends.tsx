import type { RankedDaySummary } from "../lib/contracts";
import { Metric, MetricCard } from "./MetricCard";

const DAY_MS = 24 * 60 * 60 * 1000;
const RESET_MS = 5 * 60 * 60 * 1000;

export function PlayerTrends({ days, now }: { days: RankedDaySummary[]; now: number }) {
  const todayStart = Math.floor((now - RESET_MS) / DAY_MS) * DAY_MS + RESET_MS;
  // Use recent days directly: the visible log filters out the previous Season.
  // Daily changes already exclude the Season reset, unlike subtracting profiles.
  // Count each day once and follow the group comparison's completeness rule.
  const recent = new Map(
    days.map((day) => [Date.parse(day.period.split(" – ")[0]), day]),
  );
  return (
    <section className="data-section" aria-labelledby="player-trends-title">
      <h2 id="player-trends-title">Trophy trend</h2>
      <p className="section-note">
        Finished Legend days only, ending at 05:00 UTC. Adds complete daily trophy changes
        across Seasons; the reset to 5,000 is excluded. Missing or uncertain days are left
        out. The latest day may still change.
      </p>
      <div className="metric-grid">
        {[7, 14].map((window) => {
          let total = 0;
          let counted = 0;
          for (const [start, day] of recent) {
            if (
              start >= todayStart - window * DAY_MS &&
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
              <Metric label="Days counted" value={`${counted} of ${window}`} />
            </MetricCard>
          );
        })}
      </div>
    </section>
  );
}
