import type { FreshnessState } from "../lib/contracts";

export type StateValue =
  | FreshnessState
  | "available"
  | "live"
  | "stale"
  | "uncertain"
  | "partial"
  | "complete"
  | "missing"
  | "unknown"
  | "unavailable"
  | "malformed"
  | "unclassified"
  | "rate-limited";

const labels: Record<StateValue, string> = {
  fresh: "Fresh",
  available: "Available",
  live: "Live",
  stale: "Stale",
  uncertain: "Uncertain",
  partial: "Partial",
  complete: "Complete",
  missing: "Missing",
  unknown: "Unknown",
  unavailable: "Unavailable",
  malformed: "Malformed",
  unclassified: "Unclassified",
  "rate-limited": "Rate-limited",
};

export function StateBadge({ state }: { state: StateValue }) {
  return (
    <span className={`state-badge state-${state}`}>
      <span aria-hidden="true" className="state-mark">
        {state === "fresh" ||
        state === "available" ||
        state === "live" ||
        state === "complete"
          ? "✓"
          : "!"}
      </span>
      <span className="state-label">{labels[state]}</span>
    </span>
  );
}
