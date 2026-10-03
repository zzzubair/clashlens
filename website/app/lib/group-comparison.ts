/**
 * Response contract for GET /v1/account/groups/{id}/comparison, produced by
 * python/src/clashlens/api_groups.py. Totals arrive with their sample sizes;
 * averages are worked out here so a missing sample never becomes a zero.
 */

export const COMPARISON_DAYS = [3, 7, 14] as const;
export type ComparisonDays = (typeof COMPARISON_DAYS)[number];

export type MemberStatus =
  | "tracking"
  | "checking"
  | "not_found"
  | "not_in_legend"
  | "uncertain"
  | "failed"
  | "unknown";

export type DayState =
  "complete" | "correcting" | "partial" | "uncertain" | "missing" | "retired";

export interface DayResult {
  start: string;
  state: DayState;
  net: number | null;
}

export interface ComparedPlayer {
  tag: string;
  name: string | null;
  you: boolean;
  inGroup: boolean;
  status: MemberStatus;
  trophies: number | null;
  observedAt: string | null;
  ageSeconds: number | null;
  freshness: "fresh" | "stale" | null;
  today: {
    net: number | null;
    gained: number | null;
    lost: number | null;
    attacks: number | null;
    defenses: number | null;
  } | null;
  days: DayResult[];
  countedDays: number;
  countedAttacks: number;
  net: number | null;
  netPerDay: number | null;
  vsGroup: number | null;
  attack: {
    count: number;
    stars: number;
    destruction: number;
    threeStars: number;
    trophies: number;
  };
  defense: {
    count: number;
    stars: number;
    destruction: number;
    trophies: number;
    starCounts: [number, number, number, number];
  };
}

export interface GroupComparison {
  groupId: string;
  name: string;
  days: ComparisonDays;
  dayStarts: string[];
  todayStart: string;
  generatedAt: string;
  players: ComparedPlayer[];
}

const STATUSES: readonly MemberStatus[] = [
  "tracking",
  "checking",
  "not_found",
  "not_in_legend",
  "uncertain",
  "failed",
  "unknown",
];
const DAY_STATES: readonly DayState[] = [
  "complete",
  "correcting",
  "partial",
  "uncertain",
  "missing",
  "retired",
];

export function mapGroupComparison(value: unknown): GroupComparison | null {
  if (
    !isRecord(value) ||
    value.kind !== "group-comparison" ||
    !isString(value.group_id) ||
    !isString(value.name) ||
    !COMPARISON_DAYS.includes(value.days as ComparisonDays) ||
    !Array.isArray(value.day_starts) ||
    !value.day_starts.every(isTimestamp) ||
    value.day_starts.length !== value.days ||
    !isTimestamp(value.today_start) ||
    !isTimestamp(value.generated_at) ||
    !Array.isArray(value.players)
  )
    return null;
  const players: ComparedPlayer[] = [];
  for (const item of value.players) {
    const player = mapPlayer(item, value.day_starts.length);
    if (player === null) return null;
    players.push(player);
  }
  return {
    groupId: value.group_id,
    name: value.name,
    days: value.days as ComparisonDays,
    dayStarts: value.day_starts as string[],
    todayStart: value.today_start as string,
    generatedAt: value.generated_at as string,
    players,
  };
}

function mapPlayer(value: unknown, dayCount: number): ComparedPlayer | null {
  if (
    !isRecord(value) ||
    !isString(value.tag) ||
    !(value.name === null || isString(value.name)) ||
    typeof value.you !== "boolean" ||
    typeof value.in_group !== "boolean" ||
    !STATUSES.includes(value.status as MemberStatus) ||
    !isNullableCount(value.trophies) ||
    !(value.observed_at === null || isTimestamp(value.observed_at)) ||
    !isNullableCount(value.age_seconds) ||
    !(
      value.freshness === null ||
      value.freshness === "fresh" ||
      value.freshness === "stale"
    ) ||
    !Array.isArray(value.day_results) ||
    value.day_results.length !== dayCount ||
    !isCount(value.counted_days) ||
    !isCount(value.counted_attacks) ||
    !isNullableInteger(value.net) ||
    !isNullableNumber(value.net_per_day) ||
    !isNullableNumber(value.vs_group) ||
    !isRecord(value.attack) ||
    !isRecord(value.defense) ||
    !isRecord(value.defense.star_counts)
  )
    return null;
  const days: DayResult[] = [];
  for (const day of value.day_results) {
    if (
      !isRecord(day) ||
      !isTimestamp(day.start) ||
      !DAY_STATES.includes(day.state as DayState) ||
      !isNullableInteger(day.net)
    )
      return null;
    days.push({ start: day.start, state: day.state as DayState, net: day.net });
  }
  let today: ComparedPlayer["today"] = null;
  if (value.today !== null) {
    if (
      !isRecord(value.today) ||
      !isNullableInteger(value.today.net) ||
      !isNullableCount(value.today.gained) ||
      !isNullableCount(value.today.lost) ||
      !isNullableCount(value.today.attacks) ||
      !isNullableCount(value.today.defenses)
    )
      return null;
    today = {
      net: value.today.net,
      gained: value.today.gained,
      lost: value.today.lost,
      attacks: value.today.attacks,
      defenses: value.today.defenses,
    };
  }
  const attack = value.attack;
  const defense = value.defense;
  const starCounts = defense.star_counts as Record<string, unknown>;
  const counts = ["0", "1", "2", "3"].map((star) => starCounts[star]);
  if (
    ![attack.count, attack.stars, attack.destruction, attack.three_stars].every(
      isCount,
    ) ||
    !isInteger(attack.trophies) ||
    ![defense.count, defense.stars, defense.destruction, ...counts].every(isCount) ||
    !isInteger(defense.trophies)
  )
    return null;
  return {
    tag: value.tag,
    name: value.name,
    you: value.you,
    inGroup: value.in_group,
    status: value.status as MemberStatus,
    trophies: value.trophies,
    observedAt: value.observed_at,
    ageSeconds: value.age_seconds,
    freshness: value.freshness,
    today,
    days,
    countedDays: value.counted_days,
    countedAttacks: value.counted_attacks,
    net: value.net,
    netPerDay: value.net_per_day,
    vsGroup: value.vs_group,
    attack: {
      count: attack.count as number,
      stars: attack.stars as number,
      destruction: attack.destruction as number,
      threeStars: attack.three_stars as number,
      trophies: attack.trophies as number,
    },
    defense: {
      count: defense.count as number,
      stars: defense.stars as number,
      destruction: defense.destruction as number,
      trophies: defense.trophies as number,
      starCounts: counts as [number, number, number, number],
    },
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isString(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

function isTimestamp(value: unknown): value is string {
  return typeof value === "string" && !Number.isNaN(Date.parse(value));
}

function isInteger(value: unknown): value is number {
  return Number.isSafeInteger(value);
}

function isCount(value: unknown): value is number {
  return isInteger(value) && value >= 0;
}

function isNullableInteger(value: unknown): value is number | null {
  return value === null || isInteger(value);
}

function isNullableCount(value: unknown): value is number | null {
  return value === null || isCount(value);
}

function isNullableNumber(value: unknown): value is number | null {
  return value === null || (typeof value === "number" && Number.isFinite(value));
}
