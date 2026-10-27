import type { ArmyAnalytics } from "./contracts";

/** One Armies page row: an army, a variant of one, or a unit. */
export type ArmyRow = ArmyAnalytics["rows"][number];

/** Battles a row won with this many stars; past Seasons count them apart. */
export function starCount(row: ArmyRow, stars: 1 | 2 | 3) {
  return (
    row.starCounts?.[stars] ??
    [0, row.oneStarCount, row.twoStarCount, row.threeStarCount][stars] ??
    0
  );
}

export function starRate(row: ArmyRow, stars: 1 | 2 | 3) {
  return row.usageCount ? starCount(row, stars) / row.usageCount : 0;
}

export function formatRate(value: number) {
  return `${(value * 100).toFixed(1)}%`;
}

export function battles(count: number) {
  return `${count.toLocaleString()} ${count === 1 ? "battle" : "battles"}`;
}
