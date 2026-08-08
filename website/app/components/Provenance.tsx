import type { DataProvenance, Freshness } from "../lib/contracts";
import { StateBadge } from "./StateBadge";

export function FreshnessText({ freshness }: { freshness: Freshness }) {
  return (
    <span className="freshness-text">
      <span className="sr-only">Observation freshness: </span>
      {capitalize(freshness.state)}; {formatAge(freshness.ageSeconds)} old
    </span>
  );
}

export function Provenance({ provenance }: { provenance: DataProvenance }) {
  return (
    <dl className="provenance" aria-label="Data provenance">
      <div>
        <dt>Source</dt>
        <dd>{provenance.source}</dd>
      </div>
      <div>
        <dt>Observed</dt>
        <dd>
          <time dateTime={provenance.observedAt}>
            {formatTimestamp(provenance.observedAt)}
          </time>
        </dd>
      </div>
      <div>
        <dt>Freshness</dt>
        <dd>
          <StateBadge state={provenance.freshness} />
        </dd>
      </div>
      <div>
        <dt>Coverage</dt>
        <dd>{provenance.coverage}</dd>
      </div>
      <div>
        <dt>Confidence</dt>
        <dd>{provenance.confidence}</dd>
      </div>
      <div>
        <dt>Contract</dt>
        <dd>{provenance.version}</dd>
      </div>
    </dl>
  );
}

export function formatAge(seconds: number): string {
  if (seconds < 60) return "less than 1 minute";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} minute${minutes === 1 ? "" : "s"}`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"}`;
  const days = Math.floor(hours / 24);
  return `${days} day${days === 1 ? "" : "s"}`;
}

export function formatTimestamp(value: string): string {
  return value.replace("T", " ").replace("Z", " UTC");
}

function capitalize(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1);
}
