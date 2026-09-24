import { useEffect, useState } from "react";
import type { DataProvenance, Freshness } from "../lib/contracts";
import { StateBadge } from "./StateBadge";

const utcTimestampFormatter = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  timeZone: "UTC",
});
let localTimestampFormatter: Intl.DateTimeFormat | null = null;

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
          {provenance.observedAt ? (
            <time dateTime={provenance.observedAt}>
              {formatTimestamp(provenance.observedAt)}
            </time>
          ) : (
            "Unknown"
          )}
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
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Unknown";
  return `${utcTimestampFormatter.format(date)} UTC`;
}

export function LocalTimestamp({ value }: { value: string }) {
  const [localTime, setLocalTime] = useState<string | null>(null);
  const utcTime = formatTimestamp(value);

  // The browser knows the viewer's timezone. Keep the first render identical
  // to the server's UTC text, then switch after the page becomes interactive.
  useEffect(() => {
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      setLocalTime("Unknown");
      return;
    }
    localTimestampFormatter ??= new Intl.DateTimeFormat(undefined, {
      day: "numeric",
      month: "short",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      timeZoneName: "short",
    });
    setLocalTime(localTimestampFormatter.format(date));
  }, [value]);

  return (
    <time className="local-time" dateTime={value} title={utcTime}>
      {localTime ?? utcTime}
    </time>
  );
}

function capitalize(value: string): string {
  return value.charAt(0).toUpperCase() + value.slice(1);
}
