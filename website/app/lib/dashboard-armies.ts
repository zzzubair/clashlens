/**
 * The two army cards: a player's attacks or defenses per army type over the
 * last finished Legend days, beside every tracked Legend player's with each
 * type over the same days.
 */

export type ArmyLens = "offense" | "defense";

/** The last N finished Legend days, or "season" for this Season's finished days. */
export type ArmyDays = number | "season";

/** Armies on offense's switch; Armies on defense takes any 1–28 days instead. */
export const ARMY_DAY_CHOICES = [3, 7, "season"] as const;
export const ARMY_DEFAULT_DAYS = 7;
export const ARMY_MAX_DAYS = 28;

/** Battles and three-star battles. */
export interface ArmyHits {
  battles: number;
  threeStars: number;
}

/** Also trophies won on attack or lost on defense. */
export interface ArmyOutcome extends ArmyHits {
  trophies: number;
}

export interface ArmyRow extends ArmyOutcome {
  typeId: number;
  name: string;
  /**
   * Every tracked Legend player's attacks with this type, or on defense their
   * defenses against it; null on defense until a covered day is counted.
   */
  legends: ArmyHits | ArmyOutcome | null;
}

export interface PlayerArmies {
  lens: ArmyLens;
  /** Finished Legend days in the window; with `season`, this Season's so far. */
  days: number;
  season: boolean;
  /** Finished Legend days in the window with this player's army records, Unix milliseconds. */
  coveredDays: number[];
  /** Named army types only, costliest first on defense, most used first on attack. */
  rows: ArmyRow[];
  /** Every battle, Other and unreadable armies included. */
  total: ArmyOutcome;
  /** Every tracked Legend player's defenses, on defense once a covered day is counted. */
  legendsTotal: ArmyOutcome | null;
}

export function isArmyDays(value: ArmyDays, lens: ArmyLens): boolean {
  return lens === "defense"
    ? typeof value === "number" &&
        Number.isInteger(value) &&
        value >= 1 &&
        value <= ARMY_MAX_DAYS
    : (ARMY_DAY_CHOICES as readonly ArmyDays[]).includes(value);
}

/** Share of battles not tripled, 0–100, or null with no battles. */
export function heldPercent(outcome: ArmyOutcome): number | null {
  return outcome.battles
    ? Math.round((100 * (outcome.battles - outcome.threeStars)) / outcome.battles)
    : null;
}

/** Share of battles tripled, 0–100, or null with no battles. */
export function hitPercent(outcome: ArmyHits): number | null {
  return outcome.battles
    ? Math.round((100 * outcome.threeStars) / outcome.battles)
    : null;
}

/** Average trophies per battle, or null with no battles. */
export function averageTrophies(outcome: ArmyOutcome): number | null {
  return outcome.battles ? Math.round(outcome.trophies / outcome.battles) : null;
}

/** "Last 7 days", naming the days without army records when some are missing. */
export function armyWindowLabel(armies: PlayerArmies): string {
  const days = `${armies.days} day${armies.days === 1 ? "" : "s"}`;
  const label = armies.season ? `Whole Season · ${days}` : `Last ${days}`;
  const covered = armies.coveredDays.length;
  return covered === armies.days ? label : `${label} · ${covered} with army data`;
}
