import type { RecentDay } from "../lib/contracts";
import { normalizePlayerTag } from "../lib/player-tag";

export class PythonApiError extends Error {
  readonly status: number;
  readonly payload: unknown;

  constructor(status: number, payload: unknown) {
    super("private Python service request failed");
    this.name = "PythonApiError";
    this.status = status;
    this.payload = payload;
  }
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

export function isString(value: unknown): value is string {
  return typeof value === "string";
}

export function isUtcTimestamp(value: unknown): value is string {
  return (
    isString(value) && Number.isFinite(Date.parse(value)) && /(?:Z|\+00:00)$/.test(value)
  );
}

export function isNullableString(value: unknown): value is string | null {
  return value === null || isString(value);
}

export function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

export function isInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value);
}

export function isOneOf<T extends string>(
  value: unknown,
  values: readonly T[],
): value is T {
  return isString(value) && values.includes(value as T);
}

export function isCanonicalPlayerTag(value: unknown): value is string {
  return isString(value) && normalizePlayerTag(value) === value;
}

/** A Live row's last 7 finished Legend days, each by the Reset that started
 *  it, with its result or null; null when malformed. */
export function mapRecentDays(value: unknown): RecentDay[] | null {
  if (!Array.isArray(value) || value.length > 7) return null;
  const days: RecentDay[] = [];
  for (const day of value) {
    if (
      !isRecord(day) ||
      !isUtcTimestamp(day.ranked_day_start) ||
      !(day.net_trophy_change === null || isInteger(day.net_trophy_change))
    )
      return null;
    days.push({ start: day.ranked_day_start, trophyChange: day.net_trophy_change });
  }
  return days;
}

export function isSnakeProvenance(value: unknown): value is Record<string, unknown> {
  return (
    isRecord(value) &&
    isString(value.source) &&
    isUtcTimestamp(value.observed_at) &&
    isOneOf(value.freshness, ["fresh", "stale", "unknown"] as const) &&
    isOneOf(value.confidence, ["high", "partial", "uncertain"] as const) &&
    isOneOf(value.coverage, ["complete", "partial", "missing", "unknown"] as const) &&
    isString(value.version)
  );
}
