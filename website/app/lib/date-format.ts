const dateFormats = new Map<string, Intl.DateTimeFormat>();

function formatDate(options: Intl.DateTimeFormatOptions, value: string | number | Date) {
  const date = new Date(value);
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
