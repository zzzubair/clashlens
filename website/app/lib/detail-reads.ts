/**
 * The small reads a card or sheet makes after the page's first read, when a
 * Clasher picks another time range, day or variant. Each only shows an
 * answer that still matches what is picked.
 */
import type { ArmyVariantExamples } from "./contracts";
import type { ArmyDays, PlayerArmies } from "./dashboard-armies";

/**
 * The days chosen on an Armies card and their numbers:
 *
 * - `ready`: the chosen days' numbers; null when they could not be read.
 * - `loading`: the chosen days are being read; the numbers on screen when
 *   they were asked for stay, faded.
 *
 * Events: `choose` (the page's own days come back at once, with no read) and
 * `answered`. Each choice is a new ask; only the newest ask's answer lands.
 * A failed read shows as could not be read; choosing again retries. The card
 * owns it, and drops it when the page goes.
 */
export type ArmyDaysRead =
  | { phase: "ready"; ask: number; days: ArmyDays; armies: PlayerArmies | null }
  | { phase: "loading"; ask: number; days: ArmyDays; shown: PlayerArmies | null };

export type ArmyDaysEvent =
  | { type: "choose"; ask: number; days: ArmyDays; initial: PlayerArmies }
  | { type: "answered"; ask: number; days: ArmyDays; armies: PlayerArmies | null };

export function armyDaysStart(initial: PlayerArmies): ArmyDaysRead {
  return { phase: "ready", ask: 0, days: initial.days, armies: initial };
}

/** The only place an Armies card's days move; see `ArmyDaysRead`. */
export function armyDaysStep(state: ArmyDaysRead, event: ArmyDaysEvent): ArmyDaysRead {
  if (event.type === "answered") {
    return event.ask === state.ask
      ? { phase: "ready", ask: event.ask, days: event.days, armies: event.armies }
      : state;
  }
  if (event.days === event.initial.days) {
    return { phase: "ready", ask: event.ask, days: event.days, armies: event.initial };
  }
  const shown = state.phase === "ready" ? state.armies : state.shown;
  return { phase: "loading", ask: event.ask, days: event.days, shown };
}

/** The numbers an Armies card shows: while loading, the ones already shown. */
export function armyDaysShown(state: ArmyDaysRead): PlayerArmies | null {
  return state.phase === "ready" ? state.armies : state.shown;
}

/**
 * One earlier Legend day on Bases you attacked:
 *
 * - `today`: no earlier day is picked; the card's own data shows.
 * - `unread`: not read yet; it is read once clicking pauses for 0.4 seconds.
 * - `asking`: its read is on its way; a day has one read at a time.
 * - `read`: its bases are kept while the account's tab shows.
 * - `failed`: it could not be read; picking it again asks again.
 *
 * Events: picking a day, the pause ending, and the read's answer. The
 * dashboard owns the days, as its follow-up reads build the card again.
 */
export type BasesDayRead = "today" | "unread" | "asking" | "read" | "failed";

export function basesDayRead<Bases>(
  day: number | null,
  read: ReadonlyMap<number, Bases | null>,
  asking: ReadonlySet<number>,
): BasesDayRead {
  if (day === null) return "today";
  if (read.get(day)) return "read";
  if (asking.has(day)) return "asking";
  return read.has(day) ? "failed" : "unread";
}

/**
 * A variant's example armies, loaded again each time the variant opens;
 * closing it drops what loaded. `loading` until the answer, then `ready`, or
 * `failed`, which says to close and open the variant to try again.
 */
export type VariantExamplesRead =
  | { phase: "loading" }
  | { phase: "failed" }
  | { phase: "ready"; examples: ArmyVariantExamples };

export function variantExamplesRead(
  answer: { examples: ArmyVariantExamples | null } | undefined,
): VariantExamplesRead {
  const examples = answer?.examples;
  if (examples === undefined) return { phase: "loading" };
  return examples === null ? { phase: "failed" } : { phase: "ready", examples };
}
