import type { Calendar } from "./contracts";

/** The supplied calendar is independent of a player's confirmed Season. */
export function parseCalendar(value: unknown): Calendar | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const v = value as Record<string, unknown>;
  const timestamp = (value: unknown): value is string =>
    typeof value === "string" &&
    /(?:Z|[+-]00:00)$/.test(value) &&
    Number.isFinite(Date.parse(value));
  if (
    !timestamp(v.as_of) ||
    !timestamp(v.day_start) ||
    !timestamp(v.day_end) ||
    !timestamp(v.season_start) ||
    !timestamp(v.season_end) ||
    typeof v.season_id !== "string" ||
    !Number.isSafeInteger(v.day_number) ||
    !Number.isSafeInteger(v.day_count)
  )
    return null;
  const day_number = v.day_number as number;
  const day_count = v.day_count as number;
  if (
    day_number < 1 ||
    day_number > day_count ||
    Date.parse(v.day_end) <= Date.parse(v.day_start)
  )
    return null;
  return {
    as_of: v.as_of,
    day_start: v.day_start,
    day_end: v.day_end,
    season_id: v.season_id,
    season_start: v.season_start,
    season_end: v.season_end,
    day_number,
    day_count,
  };
}
