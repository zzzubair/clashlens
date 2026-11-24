import { utcDate } from "./date-format";
export interface LeftLegendI {
  leftAt: string;
  lastDayNumber: number | null;
  /** That day's end of day, which no weekly raise follows. */
  trophies: number | null;
}

/** The Python API's `left_legend_i`, or null when absent or malformed. */
export function parseLeftLegendI(value: unknown): LeftLegendI | null {
  if (typeof value !== "object" || value === null) return null;
  const { left_at, last_day_number, trophies } = value as Record<string, unknown>;
  const count = (item: unknown) => Number.isInteger(item) && (item as number) >= 0;
  if (
    typeof left_at !== "string" ||
    Number.isNaN(Date.parse(left_at)) ||
    !(last_day_number === null || count(last_day_number)) ||
    !(trophies === null || count(trophies))
  )
    return null;
  return {
    leftAt: left_at,
    lastDayNumber: last_day_number as number | null,
    trophies: trophies as number | null,
  };
}

export function leftDate(left: LeftLegendI) {
  return utcDate(left.leftAt, { weekday: true });
}

/** "Left Legend I at the Mon 12 Oct Reset · last 4,684 trophies". */
export function leftLine(left: LeftLegendI) {
  const line = `Left Legend I at the ${leftDate(left)} Reset`;
  return left.trophies === null
    ? line
    : `${line} · last ${left.trophies.toLocaleString("en-US")} trophies`;
}
