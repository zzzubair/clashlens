/**
 * The two army cards: a player's attacks or defenses per army type over the
 * last finished Legend days.
 */

export type ArmyLens = "offense" | "defense";

/** The card switches; Armies on defense also takes any 1–28 days. */
export const ARMY_DAY_CHOICES = [3, 7] as const;
export const ARMY_DEFAULT_DAYS = 7;
export const ARMY_MAX_DAYS = 28;

/** Battles, three-star battles, and trophies won on attack or lost on defense. */
export interface ArmyOutcome {
  battles: number;
  threeStars: number;
  trophies: number;
}

export interface ArmyRow extends ArmyOutcome {
  typeId: number;
  name: string;
}

export interface PlayerArmies {
  lens: ArmyLens;
  days: number;
  /** Finished Legend days in the window with this player's army records, Unix milliseconds. */
  coveredDays: number[];
  /** Named army types only, costliest first on defense, most used first on attack. */
  rows: ArmyRow[];
  /** Every battle, Other and unreadable armies included. */
  total: ArmyOutcome;
}

export function isArmyDays(value: number, lens: ArmyLens): boolean {
  return lens === "defense"
    ? Number.isInteger(value) && value >= 1 && value <= ARMY_MAX_DAYS
    : (ARMY_DAY_CHOICES as readonly number[]).includes(value);
}

/** Share of battles not tripled, 0–100, or null with no battles. */
export function heldPercent(outcome: ArmyOutcome): number | null {
  return outcome.battles
    ? Math.round((100 * (outcome.battles - outcome.threeStars)) / outcome.battles)
    : null;
}

/** Share of battles tripled, 0–100, or null with no battles. */
export function hitPercent(outcome: ArmyOutcome): number | null {
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
  const label = `Last ${armies.days} day${armies.days === 1 ? "" : "s"}`;
  const covered = armies.coveredDays.length;
  return covered === armies.days ? label : `${label} · ${covered} with army data`;
}
