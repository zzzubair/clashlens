import { utcDate } from "./date-format";

export function validDay(day: string | null): string | null {
  return day && /^\d{4}-\d{2}-\d{2}$/.test(day) && !Number.isNaN(Date.parse(day))
    ? day
    : null;
}

/** A Legend day's date, such as "26 Oct 2026", from its period or its key. */
export function legendDayDate(period: string): string {
  const date = new Date(period.split(" – ")[0]);
  return Number.isNaN(date.getTime())
    ? "Date unavailable"
    : utcDate(date, { year: true });
}

/** Places a day's Reset rank climbed since the day before's (a fall is negative). */
export function rankMoves(
  days: Array<{ dayNumber: number | null; resetRank?: number | null }>,
): Map<number, number> {
  const ranks = new Map(days.map((day) => [day.dayNumber, day.resetRank ?? null]));
  const moves = new Map<number, number>();
  for (const { dayNumber, resetRank } of days) {
    const before = dayNumber === null ? null : ranks.get(dayNumber - 1);
    if (dayNumber !== null && before != null && resetRank != null)
      moves.set(dayNumber, before - resetRank);
  }
  return moves;
}
