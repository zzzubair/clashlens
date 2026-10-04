import type { RefreshStatus, WebsiteErrorResponse } from "./contracts";

const CANONICAL_UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
export const MAX_SEARCH_QUERY_LENGTH = 80;

export function isCanonicalUuid(value: string): boolean {
  return CANONICAL_UUID_PATTERN.test(value);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

export function isWebsiteErrorResponse(value: unknown): value is WebsiteErrorResponse {
  if (!isRecord(value) || !isRecord(value.error)) return false;
  return (
    typeof value.error.code === "string" &&
    [
      "invalid_input",
      "missing",
      "forbidden",
      "conflict",
      "rate_limited",
      "uncertain",
      "malformed",
      "unavailable",
    ].includes(value.error.code) &&
    typeof value.error.message === "string"
  );
}

export function isRefreshStatusPayload(value: unknown): value is RefreshStatus {
  if (!isRecord(value) || value.kind !== "refresh-status") return false;
  return (
    typeof value.workId === "string" &&
    typeof value.tag === "string" &&
    ["queued", "running", "complete", "unavailable", "failed"].includes(
      value.state as string,
    ) &&
    typeof value.progressPercent === "number" &&
    Number.isFinite(value.progressPercent) &&
    typeof value.message === "string" &&
    (typeof value.publishedAt === "string" || value.publishedAt === null) &&
    "player" in value &&
    (value.player === null ||
      (isRecord(value.player) &&
        ["tracking", "not_in_legend", "uncertain"].includes(
          value.player.trackingState as string,
        )))
  );
}

// The API's trophy-range rule: whole numbers from 0 to 99,999, minimum no
// higher than maximum.
export const TROPHY_RANGE_LIMITS = [0, 99_999] as const;

export function trophyRangeProblem(minimum: string, maximum: string): string | null {
  const [lowest, highest] = TROPHY_RANGE_LIMITS;
  const values = [minimum, maximum].map((value) =>
    /^\d{1,5}$/.test(value.trim()) ? Number(value) : Number.NaN,
  );
  if (values.some((value) => !(value >= lowest && value <= highest)))
    return "Enter trophies as whole numbers from 0 to 99,999.";
  if (values[0] > values[1]) return "Minimum trophies can’t be above maximum trophies.";
  return null;
}

// The API's day-range rule: whole Legend days from 1 to 28, first no later
// than last.
export function dayRangeProblem(start: string, end: string): string | null {
  const days = [start, end].map((value) =>
    /^\d{1,2}$/.test(value.trim()) ? Number(value) : Number.NaN,
  );
  if (days.some((day) => !(day >= 1 && day <= 28)))
    return "Enter Legend days as whole numbers from 1 to 28.";
  if (days[0] > days[1]) return "From Legend day can’t be after To Legend day.";
  return null;
}
