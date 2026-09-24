import type { TrackedPlayerEntry } from "../lib/contracts";

export function TrophyMark() {
  return (
    <svg className="trophy-mark" aria-hidden="true" viewBox="0 0 20 20">
      <path d="M6 3h8v3.5c0 2.6-1.6 4.7-4 4.7s-4-2.1-4-4.7V3Z" />
      <path d="M6 5H3.8v1.2c0 2 1.2 3.2 3.2 3.2M14 5h2.2v1.2c0 2-1.2 3.2-3.2 3.2M10 11.2V15m-3 2h6m-5.5-2h5" />
    </svg>
  );
}

export function latestObservation(entries: TrackedPlayerEntry[]) {
  return entries.reduce<string | null>((latest, entry) => {
    if (latest === null) return entry.freshness.observedAt;
    return Date.parse(entry.freshness.observedAt) > Date.parse(latest)
      ? entry.freshness.observedAt
      : latest;
  }, null);
}
