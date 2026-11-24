export type ArmyLens = "offense" | "defense";

export type ArmyDays = number | "season";

export const ARMY_DAY_CHOICES = [3, 7, "season"] as const;
export const ARMY_DEFAULT_DAYS = 7;
export const ARMY_MAX_DAYS = 28;

export interface ArmyHits {
  battles: number;
  threeStars: number;
}

export interface ArmyOutcome extends ArmyHits {
  trophies: number;
}

export interface ArmyRow extends ArmyOutcome {
  typeId: number;
  name: string;
  legends: ArmyHits | ArmyOutcome | null;
}

export interface PlayerArmies {
  calendar: import("./contracts").Calendar;
  lens: ArmyLens;
  days: number;
  season: boolean;
  coveredDays: number[];
  /** Named army types only, costliest first on defense, most used first on attack. */
  rows: ArmyRow[];
  total: ArmyOutcome;
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

export function heldPercent(outcome: ArmyOutcome): number | null {
  return outcome.battles
    ? Math.round((100 * (outcome.battles - outcome.threeStars)) / outcome.battles)
    : null;
}

export function hitPercent(outcome: ArmyHits): number | null {
  return outcome.battles
    ? Math.round((100 * outcome.threeStars) / outcome.battles)
    : null;
}

export function averageTrophies(outcome: ArmyOutcome): number | null {
  return outcome.battles ? Math.round(outcome.trophies / outcome.battles) : null;
}

export function armyWindowLabel(armies: PlayerArmies): string {
  const days = `${armies.days} day${armies.days === 1 ? "" : "s"}`;
  const label = armies.season ? `Whole Season · ${days}` : `Last ${days}`;
  const covered = armies.coveredDays.length;
  return covered === armies.days ? label : `${label} · ${covered} with army data`;
}
