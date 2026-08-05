import { StateBadge } from "./StateBadge";
import type { TrackedLeaderboard } from "../lib/contracts";

export function CoverageSummary({
  coverage,
}: {
  coverage: TrackedLeaderboard["coverage"];
}) {
  return (
    <p className="coverage-summary">
      Coverage: <StateBadge state={coverage.state} /> {coverage.trackedPlayers} tracked
      players; {coverage.measuredPercent}% measured. {coverage.note}
    </p>
  );
}
