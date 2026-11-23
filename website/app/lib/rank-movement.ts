export function rankMovement(rank: number, atReset: number | null): number | null {
  return atReset === null ? null : atReset - rank;
}
