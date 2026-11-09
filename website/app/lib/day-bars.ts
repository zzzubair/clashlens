/**
 * A row's own scale for its daily bars, as Rankings draws its own: its
 * biggest gain or loss reaches the band's edge, up or down from the zero line
 * in the middle, so every row shows its shape however small its days.
 */
export function rowScale(nets: (number | null)[], minimum = 1): number {
  return Math.max(minimum, ...nets.map((net) => Math.abs(net ?? 0)));
}

/** A day's bar height, as a share of the band: half of it at the row's biggest day. */
export function dayBar(net: number, scale: number): string {
  return `${(Math.abs(net) / scale) * 50}%`;
}
