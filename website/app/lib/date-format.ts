export function utcDate(
  value: string | number | Date,
  { weekday = false, year = false }: { weekday?: boolean; year?: boolean } = {},
): string {
  return new Date(value)
    .toLocaleDateString("en-GB", {
      ...(weekday ? { weekday: "short" } : {}),
      day: "numeric",
      month: "short",
      ...(year ? { year: "numeric" } : {}),
      timeZone: "UTC",
    })
    .replace("Sept", "Sep");
}

/** The days the bars cover, behind their ⓘ and in a chart's title:
 *  "22–28 Oct", or "29 Sep – 5 Oct" across a month's end. Null with no days. */
export function dayRange(starts: string[]): string | null {
  const first = starts.at(0);
  const last = starts.at(-1);
  if (first === undefined || last === undefined) return null;
  const day = (start: string) => new Date(start).getUTCDate();
  const month = (start: string) =>
    new Date(start)
      .toLocaleDateString("en-GB", { month: "short", timeZone: "UTC" })
      .replace("Sept", "Sep");
  return month(first) === month(last)
    ? `${day(first)}–${day(last)} ${month(last)}`
    : `${day(first)} ${month(first)} – ${day(last)} ${month(last)}`;
}
