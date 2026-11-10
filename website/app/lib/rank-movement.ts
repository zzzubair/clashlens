/** Positive means the player climbed; null means there is no comparison. */
export function rankMovement(rank: number, atReset: number | null): number | null {
  return atReset === null ? null : atReset - rank;
}
