/**
 * Season notebook: a Clasher's bases this Season, which base each of their
 * verified players ran on each Legend day, and each base's record, all
 * worked out by the private API. This file reads its answers and does what
 * the page does on the phone with no wait: search, filter and sort the
 * Library, draw each player's run strip, and word what a change did.
 */

import { validPlayerTag } from "./player-tag";

export type BaseStatus = "active" | "to_try" | "retired";

export const STATUS_LABEL: Record<BaseStatus, string> = {
  active: "Active",
  to_try: "To try",
  retired: "Retired",
};

/** Defenses on the counted Legend days a base ran: held means the attacker
 * did not get 3 stars. `stars` counts 0, 1, 2 and 3-star defenses. */
export interface BaseRecord {
  days: number;
  defenses: number;
  held: number;
  stars: [number, number, number, number];
  trophiesLost: number;
}

/** A stretch of Legend days one player ran one base. */
export interface NotebookRun {
  from: number;
  to: number;
  base: string;
  record: BaseRecord;
}

export interface NotebookPlayer {
  tag: string;
  name: string | null;
  league: string | null;
  runs: NotebookRun[];
  /** The base the player runs today. */
  current: string | null;
  /** A base picked from tomorrow. */
  next: { day: number; base: string | null } | null;
  picks: Array<{ day: number; base: string | null }>;
  /** The days up to tomorrow the player plays in Legend League: the only
   * days a base can be set on. */
  legendDays: number[];
}

export interface NotebookBase {
  /** The base ID, like B-7K3QX9. */
  id: string;
  name: string;
  status: BaseStatus;
  addedDay: number;
  lastRanDay: number | null;
  /** The later of when it last ran and when it was added or its status set. */
  recent: string;
  record: BaseRecord;
  /** Saved from Bases you attacked: the Legend day of the attack, and how
   * the base held for its player that day, null until the day is counted
   * (`counted`) or when their defenses aren't tracked. */
  them: { day: number; counted: boolean; record: BaseRecord | null } | null;
}

export interface Notebook {
  season: { id: string; start: string; end: string; today: number; tomorrow: number };
  /** The newest Legend day whose defenses are counted. */
  asOfDay: number | null;
  players: NotebookPlayer[];
  bases: NotebookBase[];
  /** One base's note, link and record on each player, when asked for. */
  base?: {
    id: string;
    note: string;
    link: string | null;
    players: Array<{ tag: string; record: BaseRecord }>;
  };
}

/** One change, as the private API takes it. `undo` is passed back as given. */
export interface NotebookChange {
  action: "add" | "pick" | "status" | "edit" | "delete" | "undo";
  base?: string | null;
  name?: string;
  link?: string;
  note?: string;
  status?: BaseStatus;
  tag?: string;
  day?: number;
  /** A base saved from Bases you attacked: the Clasher's player, whose base
   * it was, and the Legend day. */
  attacked?: { tag: string; opponent: string; day: number };
  undo?: unknown;
}

export interface NotebookSaved {
  base: string | null;
  changed: Array<{ base: string; status: BaseStatus }>;
  undo: unknown;
}

export const BASE_ID = /^B-[0-9A-HJKMNP-TV-Z]{6}$/;
const STATUSES = new Set(["active", "to_try", "retired"]);

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const isDay = (value: unknown): value is number =>
  Number.isInteger(value) && (value as number) >= 1 && (value as number) <= 28;
const isCount = (value: unknown): value is number =>
  Number.isInteger(value) && (value as number) >= 0;
const isCode = (value: unknown): value is string =>
  typeof value === "string" && BASE_ID.test(value);
const isTag = (value: unknown): value is string =>
  typeof value === "string" && validPlayerTag(value);
const isText = (value: unknown): value is string | null =>
  value === null || typeof value === "string";

function parseRecord(value: unknown): BaseRecord | null {
  if (!isRecord(value) || !Array.isArray(value.stars) || value.stars.length !== 4)
    return null;
  const { days, defenses, held, stars, trophies_lost: lost } = value;
  if (![days, defenses, held, lost, ...stars].every(isCount)) return null;
  return {
    days: days as number,
    defenses: defenses as number,
    held: held as number,
    stars: stars as BaseRecord["stars"],
    trophiesLost: lost as number,
  };
}

function parseRun(value: unknown): NotebookRun | null {
  if (!isRecord(value) || !isDay(value.from) || !isDay(value.to) || !isCode(value.base))
    return null;
  const record = parseRecord(value.record);
  return record && { from: value.from, to: value.to, base: value.base, record };
}

function parsePlayer(value: unknown): NotebookPlayer | null {
  if (!isRecord(value) || typeof value.tag !== "string") return null;
  const { name, league, runs, current, next, picks, legend_days: legend } = value;
  if (
    !isText(name) ||
    !isText(league) ||
    !Array.isArray(runs) ||
    !Array.isArray(picks) ||
    !Array.isArray(legend) ||
    !legend.every(isDay)
  )
    return null;
  if (current !== null && !isCode(current)) return null;
  if (
    next !== null &&
    !(isRecord(next) && isDay(next.day) && (next.base === null || isCode(next.base)))
  )
    return null;
  const parsedRuns = runs.map(parseRun);
  const parsedPicks = picks.map((pick) =>
    isRecord(pick) && isDay(pick.day) && (pick.base === null || isCode(pick.base))
      ? { day: pick.day, base: pick.base }
      : null,
  );
  if (parsedRuns.includes(null) || parsedPicks.includes(null)) return null;
  return {
    tag: value.tag,
    name,
    league,
    runs: parsedRuns as NotebookRun[],
    current,
    next: next as NotebookPlayer["next"],
    picks: parsedPicks as NotebookPlayer["picks"],
    legendDays: legend,
  };
}

function parseThem(value: unknown): NotebookBase["them"] | undefined {
  if (value === null) return null;
  if (!isRecord(value) || !isDay(value.day) || typeof value.counted !== "boolean")
    return undefined;
  const record = value.record === null ? null : parseRecord(value.record);
  if (record === null && value.record !== null) return undefined;
  return { day: value.day, counted: value.counted, record };
}

function parseBase(value: unknown): NotebookBase | null {
  if (!isRecord(value) || !isCode(value.id) || typeof value.name !== "string")
    return null;
  const { status, added_day: added, last_ran_day: last, recent } = value;
  const record = parseRecord(value.record);
  const them = parseThem(value.them);
  if (
    !record ||
    !STATUSES.has(status as string) ||
    !isDay(added) ||
    !(last === null || isDay(last)) ||
    typeof recent !== "string" ||
    !Number.isFinite(Date.parse(recent)) ||
    them === undefined
  )
    return null;
  return {
    id: value.id,
    name: value.name,
    status: status as BaseStatus,
    addedDay: added,
    lastRanDay: last,
    recent,
    record,
    them,
  };
}

/** The private API's notebook, or null when it does not fit the contract. */
export function parseNotebook(value: unknown): Notebook | null {
  if (!isRecord(value) || value.kind !== "notebook" || !isRecord(value.season))
    return null;
  const { id, start, end, today, tomorrow } = value.season;
  if (
    typeof id !== "string" ||
    typeof start !== "string" ||
    typeof end !== "string" ||
    !isDay(today) ||
    !isDay(tomorrow) ||
    !(value.as_of_day === null || isDay(value.as_of_day)) ||
    !Array.isArray(value.players) ||
    !Array.isArray(value.bases)
  )
    return null;
  const players = value.players.map(parsePlayer);
  const bases = value.bases.map(parseBase);
  if (players.includes(null) || bases.includes(null)) return null;
  const notebook: Notebook = {
    season: { id, start, end, today, tomorrow },
    asOfDay: value.as_of_day,
    players: players as NotebookPlayer[],
    bases: bases as NotebookBase[],
  };
  if (value.base !== undefined) {
    const base = value.base;
    if (
      !isRecord(base) ||
      !isCode(base.id) ||
      typeof base.note !== "string" ||
      !isText(base.link) ||
      !Array.isArray(base.players)
    )
      return null;
    const records = base.players.map((item) =>
      isRecord(item) && typeof item.tag === "string" && parseRecord(item.record)
        ? { tag: item.tag, record: parseRecord(item.record) as BaseRecord }
        : null,
    );
    if (records.includes(null)) return null;
    notebook.base = {
      id: base.id,
      note: base.note,
      link: base.link,
      players: records as Array<{ tag: string; record: BaseRecord }>,
    };
  }
  return notebook;
}

/** What a saved change did, or null when it does not fit the contract. */
export function parseSaved(value: unknown): NotebookSaved | null {
  if (
    !isRecord(value) ||
    value.kind !== "notebook-saved" ||
    !(value.base === null || isCode(value.base)) ||
    !Array.isArray(value.changed) ||
    !value.changed.every(
      (item) =>
        isRecord(item) && isCode(item.base) && STATUSES.has(item.status as string),
    )
  )
    return null;
  return {
    base: value.base,
    changed: value.changed as NotebookSaved["changed"],
    undo: value.undo ?? null,
  };
}

/** A change the page posted, checked before it goes to the private API. */
export function parseChange(value: unknown): NotebookChange | null {
  if (!isRecord(value)) return null;
  const actions = ["add", "pick", "status", "edit", "delete", "undo"];
  const { action, base, name, link, note, status, tag, day, attacked, undo } = value;
  if (
    !actions.includes(action as string) ||
    !(base === undefined || base === null || isCode(base)) ||
    !(name === undefined || (typeof name === "string" && name.length <= 200)) ||
    !(link === undefined || (typeof link === "string" && link.length <= 400)) ||
    !(note === undefined || (typeof note === "string" && note.length <= 2000)) ||
    !(status === undefined || STATUSES.has(status as string)) ||
    !(tag === undefined || isTag(tag)) ||
    !(day === undefined || isDay(day)) ||
    !(
      attacked === undefined ||
      (isRecord(attacked) &&
        Object.keys(attacked).length === 3 &&
        isTag(attacked.tag) &&
        isTag(attacked.opponent) &&
        isDay(attacked.day))
    ) ||
    !(undo === undefined || isRecord(undo))
  )
    return null;
  return value as unknown as NotebookChange;
}

/** "held 31 of 50", or a plain word when no defense is counted yet. */
export function heldText(record: BaseRecord): string {
  return record.defenses
    ? `held ${record.held} of ${record.defenses}`
    : record.days
      ? "no defenses yet"
      : "no counted days yet";
}

/** Average trophies lost a defense, as "−24", or null with none. */
export function averageLost(record: BaseRecord): string | null {
  if (!record.defenses) return null;
  return `−${Math.round(record.trophiesLost / record.defenses)}`;
}

/** The player's name, or their tag when no name is saved. */
export function playerName(player: { tag: string; name: string | null }): string {
  return player.name?.trim() || player.tag;
}

/** "5 Oct – 2 Nov" for the Season starting and ending at those Resets. */
export function seasonLabel(start: string, end: string): string {
  const format = (value: string) =>
    new Date(value).toLocaleDateString("en-GB", {
      day: "numeric",
      month: "short",
      timeZone: "UTC",
    });
  return `${format(start)} – ${format(end)}`;
}

/** Up to two letters for a base's tile, while bases have no picture. */
export function baseInitials(name: string): string {
  const words = name.split(/[\s·-]+/).filter((word) => /\p{L}|\p{N}/u.test(word));
  const letters = words
    .slice(0, 2)
    .map((word) => [...word][0] ?? "")
    .join("");
  return (letters || name.slice(0, 2)).toUpperCase();
}

/** The name split around the first case-blind match of `query`, for
 * highlighting; null when it does not match. */
export function matchName(name: string, query: string): [string, string, string] | null {
  const needle = query.trim().toLocaleLowerCase();
  if (!needle) return [name, "", ""];
  const at = name.toLocaleLowerCase().indexOf(needle);
  if (at < 0) return null;
  return [
    name.slice(0, at),
    name.slice(at, at + needle.length),
    name.slice(at + needle.length),
  ];
}

export type LibraryFilter = "all" | BaseStatus;
export type LibrarySort = "recent" | "held" | "name" | "newest";
export const SORT_LABEL: Record<LibrarySort, string> = {
  recent: "Recent",
  held: "Best held",
  name: "Name",
  newest: "Newest",
};

/** The Library's rows for a search, filter and sort, and how many bases
 * match the search in each filter. */
export function libraryView(
  bases: NotebookBase[],
  query: string,
  filter: LibraryFilter,
  sort: LibrarySort,
): { rows: NotebookBase[]; counts: Record<LibraryFilter, number> } {
  const found = bases.filter((base) => matchName(base.name, query) !== null);
  const counts: Record<LibraryFilter, number> = {
    all: found.length,
    active: 0,
    to_try: 0,
    retired: 0,
  };
  for (const base of found) counts[base.status] += 1;
  const order = new Map(bases.map((base, index) => [base.id, index]));
  const heldShare = (base: NotebookBase) =>
    base.record.defenses ? base.record.held / base.record.defenses : -1;
  const compare: Record<LibrarySort, (a: NotebookBase, b: NotebookBase) => number> = {
    recent: (a, b) => Date.parse(b.recent) - Date.parse(a.recent),
    held: (a, b) => heldShare(b) - heldShare(a) || b.record.defenses - a.record.defenses,
    name: (a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" }),
    newest: (a, b) => (order.get(b.id) ?? 0) - (order.get(a.id) ?? 0),
  };
  const rows = found
    .filter((base) => filter === "all" || base.status === filter)
    .sort((a, b) => compare[sort](a, b) || a.name.localeCompare(b.name));
  return { rows, counts };
}

/** How a base saved from an attack held for them, as "held 6 of 8 for
 * them on Day 9"; null when that isn't known. */
export function themText(them: NotebookBase["them"]): string | null {
  if (!them?.record?.defenses) return null;
  return `held ${them.record.held} of ${them.record.defenses} for them on Day ${them.day}`;
}

/** The name a base saved from an attack starts with, as "Day 9 hit · 5,712",
 * from the Legend day and the base's trophies at that day's Reset. */
export function attackedName(day: number, trophies: number | null): string {
  return trophies === null
    ? `Day ${day} hit`
    : `Day ${day} hit · ${trophies.toLocaleString("en-US")}`;
}

/** A Library row's one line: when it last ran and how it held, or how it
 * held for them, or when it was added. */
export function baseLine(base: NotebookBase, today: number): string {
  if (base.lastRanDay === null)
    return themText(base.them) ?? `added Day ${base.addedDay}`;
  const when = base.lastRanDay === today ? "ran today" : `ran Day ${base.lastRanDay}`;
  return `${when} · ${heldText(base.record)}`;
}

/** The base a player ran on `day`: their latest pick on or before it, which
 * stops at the first day they are out of Legend League. */
export function baseOnDay(player: NotebookPlayer, day: number): string | null {
  let base: string | null = null;
  for (let at = 1; at <= day; at += 1) {
    const pick = player.picks.find((item) => item.day === at);
    if (pick) base = pick.base;
    if (!isLegendDay(player, at)) base = null;
  }
  return base;
}

export interface StripBlock {
  from: number;
  to: number;
  base: string | null;
  kind: "past" | "now" | "none" | "next";
}

/** The run strip's blocks over the Season's 28 days: past runs, today's
 * run, days with no base, and the days still to come. */
export function stripBlocks(player: NotebookPlayer, today: number): StripBlock[] {
  const blocks: StripBlock[] = [];
  let day = 1;
  for (const run of player.runs) {
    if (run.from > day)
      blocks.push({ from: day, to: run.from - 1, base: null, kind: "none" });
    blocks.push({
      from: run.from,
      to: run.to,
      base: run.base,
      kind: run.to >= today ? "now" : "past",
    });
    day = run.to + 1;
  }
  if (day <= today) blocks.push({ from: day, to: today, base: null, kind: "none" });
  if (today < 28) blocks.push({ from: today + 1, to: 28, base: null, kind: "next" });
  return blocks;
}

/** Bases that ran on a player near `day`, nearest first, for the fix-a-day
 * sheet: the one already said for that day leads. */
export function basesNear(player: NotebookPlayer, day: number): string[] {
  const said = baseOnDay(player, day);
  const near = player.runs
    .map((run) => ({
      base: run.base,
      distance: day < run.from ? run.from - day : day > run.to ? day - run.to : 0,
    }))
    .sort((a, b) => a.distance - b.distance)
    .map((item) => item.base);
  return [...new Set([...(said ? [said] : []), ...near])].slice(0, 4);
}

/** The last day, up to `today`, that a player keeps the base (or no base)
 * they have on `day`: it stops before a day that is not their Legend day. */
export function sameUntil(player: NotebookPlayer, day: number, today: number): number {
  const base = baseOnDay(player, day);
  let last = day;
  while (
    last < today &&
    isLegendDay(player, last + 1) &&
    baseOnDay(player, last + 1) === base
  )
    last += 1;
  return last;
}

/** "Days 9–16", or "Day 9" for one day. */
export function daysText(from: number, to: number): string {
  return from === to ? `Day ${from}` : `Days ${from}–${to}`;
}

/** The run a player's day falls in. */
export function runOn(player: NotebookPlayer, day: number): NotebookRun | null {
  return player.runs.find((run) => run.from <= day && day <= run.to) ?? null;
}

/** Whether a base can be set on the player's ``day``: a Legend day. */
export function isLegendDay(player: NotebookPlayer, day: number): boolean {
  return player.legendDays.includes(day);
}

/** The day's date, as "Tue 20 Oct", for the Season starting at ``start``. */
export function dayDate(start: string, day: number): string {
  return new Date(Date.parse(start) + (day - 1) * 86_400_000).toLocaleDateString(
    "en-GB",
    {
      weekday: "short",
      day: "numeric",
      month: "short",
      timeZone: "UTC",
    },
  );
}

/** The players' runs of one base, as "Days 5–9, Day 13". */
export function baseRuns(player: NotebookPlayer, code: string): string {
  return player.runs
    .filter((run) => run.base === code)
    .map((run) => daysText(run.from, run.to))
    .join(", ");
}
