/**
 * What each built dashboard card receives from the loader, and the numbers it
 * works out from that. Everything here is plain data and pure functions, so
 * the server and the browser give the same answers and tests need no page.
 */

import type { LinkedPlayerCard } from "./account-contracts";
import type { ClockBattle, OpponentRow, PlayerDay, RankRange } from "./dashboard";
import type { PlayerArmies } from "./dashboard-armies";

/** Every Legend I player starts a Season here (domain.py SEASON_START_TROPHIES). */
export const SEASON_START_TROPHIES = 5000;
export const SEASON_DAYS = 28;
export const DAILY_BATTLES = 8;
/** A three-star attack wins this many trophies. */
export const MAX_BATTLE_TROPHIES = 40;
export const GOAL_PACE_DAYS = 7;

/** One finished Legend day of this Season for the selected account. */
export interface SeasonDay {
  /** Season day number, 1-28. */
  day: number;
  attacks: number | null;
  defenses: number | null;
  /** Trophies won by attacks. */
  offense: number | null;
  /** Trophies lost on defense, as a negative number. */
  defense: number | null;
  /** The day's trophy change, including any automatic defense loss. */
  net: number | null;
  triples: number | null;
  /** Defenses that were not tripled. */
  held: number | null;
  /** Trophies at the Reset that ended the day. */
  endTrophies: number | null;
  /** Rank on the board saved at that Reset. */
  rank: number | null;
  /** Each attack's stars and trophies, oldest first; null when some may not be recorded. */
  hits: [stars: number, trophies: number][] | null;
}

export interface SeasonData {
  /** Today's Season day, 1-28; null when the Season is not known yet. */
  today: number | null;
  /** Finished days of this Season, oldest first; a day missing here has no saved result. */
  days: SeasonDay[];
}

export interface ClockData {
  battles: ClockBattle[];
  observedAtMs: number | null;
}

export interface SeasonGraphData extends SeasonData {
  /** The previous Season's official finish, never a tracked-only rank. */
  lastSeason: { rank: number | null; trophies: number | null } | null;
}

export interface GoalData {
  rank: number;
  name: string;
  /** Finished days this Season; 0 on Day 1. */
  finished: number;
  /** Days the pace numbers cover: up to 7. */
  window: number;
  /** Your trophies after the last Reset, and `window` days earlier: your pace. */
  you: number | null;
  youThen: number | null;
  /** Trophies at the goal rank on the last Reset board, and `window` days earlier. */
  line: number | null;
  lineThen: number | null;
  /**
   * Your trophies on each Reset board of the window, oldest first, taken
   * before the automatic defense loss like the line's: the gap and the chart.
   */
  history: (number | null)[];
}

export interface CutoffLine {
  top: number;
  trophies: number | null;
}

export interface CutoffsData {
  name: string;
  live: { lines: CutoffLine[]; you: number | null; observedAtMs: number | null };
  /**
   * The board saved at the last Reset, with your trophies on it; null before
   * the Season's first Reset.
   */
  reset: { lines: CutoffLine[]; you: number | null } | null;
}

export interface RivalRow {
  tag: string;
  name: string;
  rank: number;
  trophies: number;
  you: boolean;
  /** Places gained (positive) or lost since the last Reset. */
  moved: number | null;
  /** Share of attacks tripled, 0-1. */
  hitRate: number | null;
  /** Live: today's totals. Season: averages a day. */
  offense: number | null;
  defense: number | null;
}

export interface RivalsData {
  live: { rows: RivalRow[]; observedAtMs: number | null } | null;
  season: { rows: RivalRow[]; days: number } | null;
}

export interface GroupRow {
  tag: string;
  name: string;
  you: boolean;
  trophies: number | null;
  /** Averages a day over the days counted. */
  gain: number | null;
  hitRate: number | null;
  offense: number | null;
  defense: number | null;
}

export interface GroupWatchData {
  /** Your groups, oldest first. */
  groups: { id: string; name: string; members: number }[];
  /** The group shown; null when you have none. */
  groupId: string | null;
  window: "7" | "season";
  /** Days the numbers cover. */
  days: number;
  /** Best average gain first. */
  rows: GroupRow[];
}

/**
 * A finished day with no attacks, no defenses and a trophy change of 0: a
 * shield day, left out of rankings.
 */
export function isShieldDay(day: SeasonDay): boolean {
  return day.attacks === 0 && day.defenses === 0 && day.net === 0;
}

function ratio(top: number, bottom: number): number | null {
  return bottom > 0 ? top / bottom : null;
}

/**
 * `top` over `bottom`, each summed over only the days where both are known,
 * so an unknown day never counts as zero. Without `bottom`, the average a day.
 */
export function rate(
  days: SeasonDay[],
  top: (day: SeasonDay) => number | null,
  bottom: (day: SeasonDay) => number | null = () => 1,
): number | null {
  let over = 0;
  let under = 0;
  for (const day of days) {
    const above = top(day);
    const below = bottom(day);
    if (above === null || below === null) continue;
    over += above;
    under += below;
  }
  return ratio(over, under);
}

/** The finished days from `first` to `last` that have a saved result. */
function between(season: SeasonData, first: number, last: number): SeasonDay[] {
  return season.days.filter((day) => day.day >= first && day.day <= last);
}

export interface DayWindow {
  first: number;
  last: number;
  /** Days the window spans; fewer than asked early in the Season. */
  span: number;
  days: SeasonDay[];
}

/** The last `size` finished days of this Season; empty on Day 1. */
export function lastDays(season: SeasonData, size: number): DayWindow | null {
  if (season.today === null || season.today < 2) return null;
  const last = season.today - 1;
  const first = Math.max(1, last - size + 1);
  return { first, last, span: last - first + 1, days: between(season, first, last) };
}

interface FormTotals {
  net: number | null;
  perAttack: number | null;
  tripled: number | null;
  perDefense: number | null;
  held: number | null;
}

function formTotals(days: SeasonDay[]): FormTotals {
  return {
    net: rate(days, (day) => day.net),
    perAttack: rate(
      days,
      (day) => day.offense,
      (day) => day.attacks,
    ),
    tripled: rate(
      days,
      (day) => day.triples,
      (day) => day.attacks,
    ),
    perDefense: rate(
      days,
      (day) => day.defense,
      (day) => day.defenses,
    ),
    held: rate(
      days,
      (day) => day.held,
      (day) => day.defenses,
    ),
  };
}

export interface RecentForm {
  window: DayWindow;
  /** Days with a known trophy change; the total and a-day numbers cover only these. */
  counted: number;
  total: number | null;
  now: FormTotals;
  /** The same numbers for the days before, once the Season has that many. */
  before: FormTotals | null;
}

export function recentForm(season: SeasonData, size: number): RecentForm | null {
  const window = lastDays(season, size);
  if (window === null || window.days.length === 0) return null;
  const earlier =
    window.span === size && window.first - size >= 1
      ? between(season, window.first - size, window.first - 1)
      : [];
  const nets = window.days.flatMap((day) => (day.net === null ? [] : [day.net]));
  return {
    window,
    counted: nets.length,
    total: nets.length ? nets.reduce((total, net) => total + net, 0) : null,
    now: formTotals(window.days),
    before: earlier.length ? formTotals(earlier) : null,
  };
}

export interface OffenseConsistency {
  window: DayWindow;
  attacks: number;
  /** Attack slots in the window, counting 8 a day with every attack recorded. */
  slots: number;
  triples: number;
  /** Attacks by stars: 3★, 2★, 1★, 0★. */
  stars: [number, number, number, number];
  perAttack: number | null;
  perMiss: number | null;
  /** Trophies left on the table against tripling every slot (zero or less). */
  missed: number;
  /** Null for a day whose attacks may not all be recorded. */
  perDay: { day: number; triples: number | null }[];
}

/**
 * Hit rate assumes 8 attacks a day with every attack recorded, so an attack
 * never made counts as a miss; a day that may be missing attacks is left
 * out. Averages use the attacks actually made.
 */
export function offenseConsistency(
  season: SeasonData,
  size: number,
): OffenseConsistency | null {
  const window = lastDays(season, size);
  const recorded = window?.days.filter((day) => day.hits !== null) ?? [];
  if (window === null || recorded.length === 0) return null;
  const hits = recorded.flatMap((day) => day.hits ?? []);
  const stars: [number, number, number, number] = [0, 0, 0, 0];
  for (const [star] of hits) stars[3 - Math.max(0, Math.min(3, star))]!++;
  const trophies = hits.reduce((total, [, gained]) => total + gained, 0);
  const misses = hits.filter(([star]) => star < 3);
  const slots = recorded.length * DAILY_BATTLES;
  return {
    window,
    attacks: hits.length,
    slots,
    triples: stars[0],
    stars,
    perAttack: ratio(trophies, hits.length),
    perMiss: ratio(
      misses.reduce((total, [, gained]) => total + gained, 0),
      misses.length,
    ),
    missed: Math.min(0, trophies - slots * MAX_BATTLE_TROPHIES),
    perDay: window.days.map((day) => ({
      day: day.day,
      triples: day.hits === null ? null : day.hits.filter(([star]) => star === 3).length,
    })),
  };
}

export type HeatMode = "net" | "offense" | "defense";

export function heatValue(day: SeasonDay, mode: HeatMode): number | null {
  return mode === "net" ? day.net : mode === "offense" ? day.offense : day.defense;
}

export interface HeatCell {
  day: number;
  kind: "played" | "shield" | "missing" | "today" | "future";
  value: number | null;
  /** 0 for your worst played day up to 1 for your best; null when not ranked. */
  shade: number | null;
}

/** Each Season day as a square; played days are ranked against each other. */
export function heatCells(season: SeasonData, mode: HeatMode): HeatCell[] {
  const today = season.today ?? 1;
  const byDay = new Map(season.days.map((day) => [day.day, day]));
  const ranked = season.days
    .filter((day) => !isShieldDay(day) && heatValue(day, mode) !== null)
    .map((day) => heatValue(day, mode) as number)
    .sort((a, b) => a - b);
  // Equal values share the middle of their places, so ties get one colour.
  const shade = (value: number) => {
    if (ranked.length < 2) return 0.5;
    const first = ranked.indexOf(value);
    const last = ranked.lastIndexOf(value);
    return (first + last) / 2 / (ranked.length - 1);
  };
  return Array.from({ length: SEASON_DAYS }, (_, index): HeatCell => {
    const number = index + 1;
    if (number === today) return { day: number, kind: "today", value: null, shade: null };
    if (number > today) return { day: number, kind: "future", value: null, shade: null };
    const saved = byDay.get(number);
    const value = saved ? heatValue(saved, mode) : null;
    if (!saved || value === null)
      return { day: number, kind: "missing", value: null, shade: null };
    if (isShieldDay(saved)) return { day: number, kind: "shield", value, shade: null };
    return { day: number, kind: "played", value, shade: shade(value) };
  });
}

const HEAT_STOPS = [
  [224, 49, 49],
  [255, 138, 31],
  [255, 212, 71],
  [47, 179, 68],
] as const;

/** Red for your worst day, then orange, yellow and green for your best. */
export function heatColour(shade: number): [number, number, number] {
  const position = Math.max(0, Math.min(1, shade)) * 3;
  const step = Math.min(2, Math.floor(position));
  const part = position - step;
  const from = HEAT_STOPS[step]!;
  const to = HEAT_STOPS[step + 1]!;
  return [0, 1, 2].map((channel) =>
    Math.round(from[channel]! + (to[channel]! - from[channel]!) * part),
  ) as [number, number, number];
}

export interface GoalPlan {
  /** Trophies a day needed from the last Reset to finish on the line. */
  need: number;
  pace: number;
  /** How fast the goal line moves, trophies a day. */
  drift: number;
  /** Your trophies minus the line's at the last Reset. */
  gap: number;
  daysLeft: number;
  status: "ahead" | "on" | "behind";
  /** Shield lengths that still leave a day to play, with what each needs. */
  shields: { days: number; need: number; onPace: boolean }[];
}

/**
 * Shield days count as 0 for you while the line keeps moving, so a day
 * needed = (gap + line speed × days left) ÷ (days left − shield days).
 */
export function goalPlan(data: GoalData): GoalPlan | null {
  const onBoard = data.history.at(-1) ?? null;
  if (
    data.finished < 1 ||
    data.window < 1 ||
    data.you === null ||
    data.youThen === null ||
    data.line === null ||
    data.lineThen === null ||
    onBoard === null
  )
    return null;
  const daysLeft = SEASON_DAYS - data.finished;
  const pace = (data.you - data.youThen) / data.window;
  const drift = (data.line - data.lineThen) / data.window;
  const behind = data.line - onBoard;
  const need = behind / daysLeft + drift;
  const shields = [1, 2, 3]
    .filter((days) => days < daysLeft)
    .map((days) => {
      const shielded = (behind + drift * daysLeft) / (daysLeft - days);
      return { days, need: shielded, onPace: pace >= shielded };
    });
  return {
    need,
    pace,
    drift,
    gap: -behind,
    daysLeft,
    status: behind <= 0 && need <= 0 ? "ahead" : pace >= need ? "on" : "behind",
    shields,
  };
}

/** The rows a card shows around one row: `before` above and `after` below, edges filled from the other side. */
export function around<T>(rows: T[], index: number, before: number, after: number): T[] {
  const size = before + after + 1;
  if (index < 0 || rows.length <= size) return rows;
  const start = Math.max(0, Math.min(index - before, rows.length - size));
  return rows.slice(start, start + size);
}

/** The data each built card is drawn from. A card added later adds its entry here. */
export interface LegendDayData {
  player: LinkedPlayerCard;
  /** Null when the player page could not be read. */
  day: PlayerDay | null;
  range: RankRange | null;
  /** When the board behind `range` was read; null without a range. */
  rangeAtMs: number | null;
  /** Read before the shared board or while the projected place is worked out: asks again. */
  boardPending?: true;
  /** The projected place at the next Reset, inside `range`; null until it can be worked out. */
  projected: number | null;
}

export interface BasesData {
  /** Null until the player's battles for today are saved. */
  rows: OpponentRow[] | null;
  /** When the comparisons with Legends at similar trophies were read; null without them. */
  comparedAtMs: number | null;
  /** Read before the shared board: the page asks again for the comparisons. */
  boardPending?: true;
}

/** The army cards' first read: the last 7 finished days. */
export interface ArmiesData {
  tag: string;
  armies: PlayerArmies;
}

export interface CardDataMap {
  "legend-day": LegendDayData;
  bases: BasesData;
  "armies-defense": ArmiesData;
  "armies-offense": ArmiesData;
  "legend-clock": ClockData;
  goal: GoalData;
  "rivals-watch": RivalsData;
  "heat-map": SeasonData;
  cutoffs: CutoffsData;
  "season-graph": SeasonGraphData;
  "recent-form": SeasonData;
  "offense-consistency": SeasonData;
  "group-watch": GroupWatchData;
}

/** Null when the card's data could not be read this time. */
export type CardDataSet = { [Card in keyof CardDataMap]?: CardDataMap[Card] | null };

/** Each card's data, or its read still on the way. */
export type CardReads = {
  [Card in keyof CardDataMap]?:
    CardDataMap[Card] | null | Promise<CardDataMap[Card] | null>;
};
