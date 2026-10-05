import { useState } from "react";
import { battleStatistics, type BattlePeriod } from "../lib/battle-statistics";
import type { PlayerPage } from "../lib/contracts";
import { Metric, MetricCard } from "./MetricCard";

const dateFormatter = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "short",
  year: "numeric",
  timeZone: "UTC",
});
const date = (time: number) => dateFormatter.format(time).replace("Sept", "Sep");
const average = (value: number | null, suffix = "") =>
  value === null ? "Unavailable" : `${value.toFixed(2)}${suffix}`;
const rate = (value: number | null) =>
  value === null ? "Unavailable" : `${value.toFixed(1)}%`;

export function BattleStatistics({ player, now }: { player: PlayerPage; now: number }) {
  const [period, setPeriod] = useState<BattlePeriod>("season");
  const stats = battleStatistics(player, period, now);
  return (
    <section
      className="data-section battle-statistics"
      aria-labelledby="battle-statistics-title"
    >
      <h2 id="battle-statistics-title">Attack and defense stats</h2>
      <label>
        Period{" "}
        <select
          value={period}
          onChange={(event) => setPeriod(event.target.value as BattlePeriod)}
        >
          <option value="7">Last 7 days</option>
          <option value="14">Last 14 days</option>
          <option value="season">This Season</option>
        </select>
      </label>
      <div aria-live="polite">
        <p className="section-note">
          {date(stats.start)} at 05:00 UTC to {date(stats.end)} so far. Includes today.
          Legend days start at 05:00 UTC.
        </p>
        <p className="section-note">
          {stats.daysSaved} of {stats.daysExpected} Legend days have saved logs. Averages
          use only the recorded battles below. Automatic defense loss at Reset is
          excluded.
          {stats.incomplete || stats.daysSaved < stats.daysExpected
            ? " Some battle details may be missing."
            : ""}
          {stats.attack.disputed || stats.defense.disputed
            ? " Some recorded battles have conflicting reports."
            : ""}
        </p>
        <div className="metric-grid">
          <MetricCard title="Attack">
            <Metric label="Attacks in sample" value={String(stats.attack.count)} />
            <Metric label="Triple rate" value={rate(stats.attack.tripleRate)} />
            <Metric label="Average stars" value={average(stats.attack.averageStars)} />
            <Metric
              label="Average destruction"
              value={average(stats.attack.averageDestruction, "%")}
            />
            <Metric
              label="Trophies per attack"
              value={average(stats.attack.averageTrophies)}
            />
          </MetricCard>
          <MetricCard title="Defense">
            <Metric label="Defenses in sample" value={String(stats.defense.count)} />
            <Metric label="Hold rate" value={rate(stats.defense.holdRate)} />
            <Metric
              label="Average stars given up"
              value={average(stats.defense.averageStars)}
            />
            <Metric
              label="Average destruction given up"
              value={average(stats.defense.averageDestruction, "%")}
            />
            <Metric
              label="Trophies given up per defense"
              value={average(stats.defense.averageTrophies)}
            />
            {stats.defense.stars.map((count, stars) => (
              <Metric
                key={stars}
                label={`${stars}-star defenses`}
                value={String(count)}
              />
            ))}
          </MetricCard>
        </div>
        <p className="section-note">
          A triple is a three-star attack. A hold is a recorded defense that avoided three
          stars.
        </p>
      </div>
    </section>
  );
}
