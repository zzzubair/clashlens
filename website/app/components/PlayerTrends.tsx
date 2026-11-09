import type { PlayerPage } from "../lib/contracts";
import { Metric, MetricCard } from "./MetricCard";

export function PlayerTrends({ player }: { player: PlayerPage }) {
  const trends = player.trophyTrends;
  if (trends === null) return null;
  return (
    <section className="data-section" aria-labelledby="player-trends-title">
      <h2 id="player-trends-title">Trophy trend</h2>
      <div className="metric-grid">
        {(["7", "14"] as const).map((window) => {
          const { span, counted, total } = trends[window];
          return (
            <MetricCard
              key={window}
              title={`Last ${window} days${span < Number(window) ? ` (${span} so far)` : ""}`}
            >
              <Metric
                label="Trophy change"
                value={
                  counted === 0
                    ? "Unavailable"
                    : `${total! > 0 ? "+" : ""}${total!.toLocaleString("en-GB")}`
                }
              />
              <Metric label="Days counted" value={`${counted} of ${span}`} />
            </MetricCard>
          );
        })}
      </div>
    </section>
  );
}
