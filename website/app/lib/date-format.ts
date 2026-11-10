// One formatter per option set: building an Intl formatter costs far more
// than formatting with one, and pages format dozens of dates.
const dateFormats = new Map<string, Intl.DateTimeFormat>();

function formatDate(options: Intl.DateTimeFormatOptions, value: string | number | Date) {
  const date = new Date(value);
  // As toLocaleDateString does; a formatter throws on an invalid date.
  if (Number.isNaN(date.getTime())) return "Invalid Date";
  const key = JSON.stringify(options);
  let format = dateFormats.get(key);
  if (!format) {
    format = new Intl.DateTimeFormat("en-GB", { ...options, timeZone: "UTC" });
    dateFormats.set(key, format);
  }
  return format.format(date);
}

export function utcDate(
  value: string | number | Date,
  { weekday = false, year = false }: { weekday?: boolean; year?: boolean } = {},
): string {
  return formatDate(
    {
      ...(weekday ? { weekday: "short" } : {}),
      day: "numeric",
      month: "short",
      ...(year ? { year: "numeric" } : {}),
    },
    value,
  ).replace("Sept", "Sep");
}

/** The days the bars cover, behind their ⓘ and in a chart's title:
 *  "22–28 Oct", or "29 Sep – 5 Oct" across a month's end. Null with no days. */
export function dayRange(starts: string[]): string | null {
  const first = starts.at(0);
  const last = starts.at(-1);
  if (first === undefined || last === undefined) return null;
  const day = (start: string) => new Date(start).getUTCDate();
  const month = (start: string) =>
    formatDate({ month: "short" }, start).replace("Sept", "Sep");
  return month(first) === month(last)
    ? `${day(first)}–${day(last)} ${month(last)}`
    : `${day(first)} ${month(first)} – ${day(last)} ${month(last)}`;
}
