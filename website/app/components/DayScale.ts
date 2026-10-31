/**
 * A row's own scale for its daily bars, as Rankings draws its own: its
 * biggest gain or loss reaches the band's edge, up or down from the zero line
 * in the middle, so every row shows its shape however small its days.
 */
export function rowScale(nets: (number | null)[]): number {
  return Math.max(1, ...nets.map((net) => Math.abs(net ?? 0)));
}

/** A day's bar height, as a share of the band: half of it at the row's biggest day. */
export function dayBar(net: number, scale: number): string {
  return `${(Math.abs(net) / scale) * 50}%`;
}

/** The day labels under a chart of these days: "22 Oct". */
export function dayLabel(start: string): string {
  return new Date(start)
    .toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" })
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
