/**
 * Notebook: the bases a Clasher's notebook points to, which base each of
 * their verified game accounts ran on each Legend day, and each base's
 * record from everyone who ran it, all worked out by the private API. This
 * file reads its answers and does what the page does on the phone with no
 * wait: search, filter and sort the bases, draw each account's run strip,
 * and word what a change did.
 */

/** Running: one of the Clasher's accounts runs it today or from tomorrow.
 * Retired: retired by hand, or 30 days with no counted day. Season: on this
 * Season's page otherwise. */
export type BaseStatus = "running" | "season" | "retired";

export const STATUS_LABEL: Record<BaseStatus, string> = {
  running: "Running",
  season: "This Season",
  retired: "Retired",
};

/** Defenses on the counted Legend days a base ran: held means the attacker
 * did not get 3 stars. `stars` counts 0, 1, 2 and 3-star defenses. `noData`
 * counts days it ran with nothing saved for that account. */
export interface BaseRecord {
  days: number;
  noData: number;
  defenses: number;
  held: number;
  stars: [number, number, number, number];
  trophiesLost: number;
}

/** A stretch of Legend days one account ran one base. */
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
  /** The base the account runs today. */
  current: string | null;
  /** A different base, or none, from tomorrow. */
  next: { day: number; base: string | null } | null;
  picks: Array<{ day: number; base: string | null }>;
  /** The days up to tomorrow the account plays in Legend League: the only
   * days a base can be set on. */
  legendDays: number[];
}

export interface NotebookBase {
  /** The base ID, like B-7K3QX9. */
  id: string;
  name: string;
  /** Made by this Clash Lens account, so it may edit it. */
  mine: boolean;
  /** Made or put in this notebook by this account: on a Crew's page, an
   * Uploader may retire or remove it. */
  own: boolean;
  status: BaseStatus;
  /** On this Season's page; a past page offers to bring it back. */
  here: boolean;
  /** The day it was added, when that was in the page's Season. */
  addedDay: number | null;
  lastRanDay: number | null;
  /** How many accounts ran it this Season, the Clasher's and everyone else's. */
  ranBy: number;
  record: BaseRecord;
}

export type CrewRole = "owner" | "admin" | "uploader" | "member";

/** A Crew and the Clasher's role in it. */
export interface NotebookCrew {
  id: string;
  name: string;
  role: CrewRole;
}

export interface Notebook {
  /** The Crew whose notebook this is, or null for the Clasher's own. */
  crew: NotebookCrew | null;
  /** The Clasher's Crews, for "Add to Crew". */
  crews: NotebookCrew[];
  season: {
    id: string;
    start: string;
    end: string;
    today: number;
    /** Tomorrow's day: 29 on Day 28, the next Season's Day 1. */
    tomorrow: number;
    /** This Season's page; a past one is read only. */
    current: boolean;
  };
  /** This Season and every earlier one with a page, newest first. */
  seasons: string[];
  /** The newest Legend day whose defenses are counted. */
  asOfDay: number | null;
  players: NotebookPlayer[];
  bases: NotebookBase[];
  /** One base's note, link and record on each of the Clasher's accounts,
   * when asked for. */
  base?: {
    id: string;
    note: string;
    link: string | null;
    players: Array<{ tag: string; record: BaseRecord }>;
    /** "mine" when it is in the Clasher's own notebook, and the ids of
     * their Crews it is in. */
    shelves: string[];
  };
}

/** One change, as the private API takes it. `undo` is passed back as given. */
export interface NotebookChange {
  action: "add" | "pick" | "retire" | "keep" | "edit" | "remove" | "undo";
  base?: string | null;
  name?: string;
  link?: string;
  note?: string;
  tag?: string;
  day?: number;
  /** The Season the page was on, so a change sent across a Reset is refused. */
  season?: string;
  /** A Crew's notebook, by the Crew's id; null for the Clasher's own. */
  crew?: string | null;
  undo?: unknown;
}

export interface NotebookSaved {
  base: string | null;
  changed: Array<{ base: string; status: BaseStatus }>;
  undo: unknown;
}

export const BASE_ID = /^B-[0-9A-HJKMNP-TV-Z]{6}$/;
export const SEASON_ID = /^[0-9]{1,12}$/;
export const CREW_ID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const ROLES = new Set(["owner", "admin", "uploader", "member"]);
const STATUSES = new Set(["running", "season", "retired"]);
const ACTIONS = ["add", "pick", "retire", "keep", "edit", "remove", "undo"];

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const isDay = (value: unknown): value is number =>
  Number.isInteger(value) && (value as number) >= 1 && (value as number) <= 29;
const isCount = (value: unknown): value is number =>
  Number.isInteger(value) && (value as number) >= 0;
const isCode = (value: unknown): value is string =>
  typeof value === "string" && BASE_ID.test(value);
const isText = (value: unknown): value is string | null =>
  value === null || typeof value === "string";

function parseRecord(value: unknown): BaseRecord | null {
  if (!isRecord(value) || !Array.isArray(value.stars) || value.stars.length !== 4)
    return null;
  const { days, no_data: noData, defenses, held, stars, trophies_lost: lost } = value;
  if (![days, noData, defenses, held, lost, ...stars].every(isCount)) return null;
  return {
    days: days as number,
    noData: noData as number,
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

function parseBase(value: unknown): NotebookBase | null {
  if (!isRecord(value) || !isCode(value.id) || typeof value.name !== "string")
    return null;
  const {
    mine,
    own,
    status,
    here,
    added_day: added,
    last_ran_day: last,
    ran_by: ranBy,
  } = value;
  const record = parseRecord(value.record);
  if (
    !record ||
    typeof mine !== "boolean" ||
    typeof own !== "boolean" ||
    typeof here !== "boolean" ||
    !STATUSES.has(status as string) ||
    !(added === null || isDay(added)) ||
    !(last === null || isDay(last)) ||
    !isCount(ranBy)
  )
    return null;
  return {
    id: value.id,
    name: value.name,
    mine,
    own,
    status: status as BaseStatus,
    here,
    addedDay: added,
    lastRanDay: last,
    ranBy,
    record,
  };
}

function parseCrew(value: unknown): NotebookCrew | null {
  return isRecord(value) &&
    typeof value.id === "string" &&
    CREW_ID.test(value.id) &&
    typeof value.name === "string" &&
    ROLES.has(value.role as string)
    ? { id: value.id, name: value.name, role: value.role as CrewRole }
    : null;
}

/** The private API's notebook, or null when it does not fit the contract. */
export function parseNotebook(value: unknown): Notebook | null {
  if (!isRecord(value) || value.kind !== "notebook" || !isRecord(value.season))
    return null;
  const { id, start, end, today, tomorrow, current } = value.season;
  if (
    typeof id !== "string" ||
    !SEASON_ID.test(id) ||
    typeof start !== "string" ||
    typeof end !== "string" ||
    !isDay(today) ||
    !isDay(tomorrow) ||
    typeof current !== "boolean" ||
    !Array.isArray(value.seasons) ||
    !value.seasons.every((item) => typeof item === "string" && SEASON_ID.test(item)) ||
    !(value.as_of_day === null || isDay(value.as_of_day)) ||
    !Array.isArray(value.players) ||
    !Array.isArray(value.bases) ||
    !Array.isArray(value.crews)
  )
    return null;
  const players = value.players.map(parsePlayer);
  const bases = value.bases.map(parseBase);
  const crews = value.crews.map(parseCrew);
  const crew = value.crew === null ? null : parseCrew(value.crew);
  if (
    players.includes(null) ||
    bases.includes(null) ||
    crews.includes(null) ||
    (value.crew !== null && !crew)
  )
    return null;
  const notebook: Notebook = {
    crew,
    crews: crews as NotebookCrew[],
    season: { id, start, end, today, tomorrow, current },
    seasons: value.seasons as string[],
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
      !Array.isArray(base.players) ||
      !Array.isArray(base.shelves) ||
      !base.shelves.every(
        (item) => item === "mine" || (typeof item === "string" && CREW_ID.test(item)),
      )
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
      shelves: base.shelves as string[],
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
  const { action, base, name, link, note, tag, day, season, crew, undo } = value;
  if (
    !ACTIONS.includes(action as string) ||
    !(base === undefined || base === null || isCode(base)) ||
    !(name === undefined || (typeof name === "string" && name.length <= 200)) ||
    !(link === undefined || (typeof link === "string" && link.length <= 400)) ||
    !(note === undefined || (typeof note === "string" && note.length <= 2000)) ||
    !(tag === undefined || (typeof tag === "string" && /^#[0-9A-Z]{2,14}$/.test(tag))) ||
    !(day === undefined || isDay(day)) ||
    !(season === undefined || (typeof season === "string" && SEASON_ID.test(season))) ||
    !(
      crew === undefined ||
      crew === null ||
      (typeof crew === "string" && CREW_ID.test(crew))
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

/** "3 days with no data", or null when every day had data. */
export function noDataText(record: BaseRecord): string | null {
  if (!record.noData) return null;
  return `${record.noData} ${record.noData === 1 ? "day" : "days"} with no data`;
}

/** The account's name, or its tag when no name is saved. */
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

/** "Oct 2026" for a Season starting at `id`, its start in Unix seconds:
 * the month most of its days fall in. */
export function seasonName(id: string): string {
  return new Date((Number(id) + 14 * 86_400) * 1000).toLocaleDateString("en-GB", {
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  });
}

/** "Day 12", with the next Season's Day 1 named as that. */
export function dayText(day: number): string {
  return day === 29 ? "Day 1 of next Season" : `Day ${day}`;
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

/** This Season shows running and on-this-Season bases; Retired the rest. */
export type LibraryFilter = "season" | "retired";
export type LibrarySort = "recent" | "held" | "name";
export const SORT_LABEL: Record<LibrarySort, string> = {
  recent: "Recent",
  held: "Best held",
  name: "Name",
};

/** The bases' rows for a search, filter and sort, and how many bases match
 * the search in each filter. A past Season's page has no filter. */
export function libraryView(
  bases: NotebookBase[],
  query: string,
  filter: LibraryFilter | null,
  sort: LibrarySort,
): { rows: NotebookBase[]; counts: Record<LibraryFilter, number> } {
  const found = bases.filter((base) => matchName(base.name, query) !== null);
  const inFilter = (base: NotebookBase, key: LibraryFilter) =>
    key === "retired" ? base.status === "retired" : base.status !== "retired";
  const counts: Record<LibraryFilter, number> = {
    season: found.filter((base) => inFilter(base, "season")).length,
    retired: found.filter((base) => inFilter(base, "retired")).length,
  };
  const order = new Map(bases.map((base, index) => [base.id, index]));
  const heldShare = (base: NotebookBase) =>
    base.record.defenses ? base.record.held / base.record.defenses : -1;
  // Recent: running first, then by the day it last ran or was added, newest
  // first, then newest added.
  const recent = (base: NotebookBase) =>
    (base.status === "running" ? 100 : 0) + (base.lastRanDay ?? base.addedDay ?? 0);
  const compare: Record<LibrarySort, (a: NotebookBase, b: NotebookBase) => number> = {
    recent: (a, b) =>
      recent(b) - recent(a) || (order.get(b.id) ?? 0) - (order.get(a.id) ?? 0),
    held: (a, b) => heldShare(b) - heldShare(a) || b.record.defenses - a.record.defenses,
    name: (a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" }),
  };
  const rows = found
    .filter((base) => filter === null || inFilter(base, filter))
    .sort((a, b) => compare[sort](a, b) || a.name.localeCompare(b.name));
  return { rows, counts };
}

/** A row's one line: who ran it, when it last ran and how it held, or when
 * it was added. */
export function baseLine(base: NotebookBase, today: number): string {
  const who = base.ranBy ? `${base.ranBy} ran it · ` : "";
  if (base.lastRanDay === null)
    return base.ranBy
      ? `${who}${heldText(base.record)}`
      : base.addedDay
        ? `added Day ${base.addedDay} · not run yet`
        : "not run this Season";
  const when = base.lastRanDay >= today ? "ran today" : `ran Day ${base.lastRanDay}`;
  return `${who}${when} · ${heldText(base.record)}`;
}

/** The base an account ran on `day`: the run that reaches it. Runs already
 * stop at each day out of Legend League and carry in from last Season. */
export function baseOnDay(player: NotebookPlayer, day: number): string | null {
  return runOn(player, day)?.base ?? null;
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

/** Bases that ran on an account near `day`, nearest first, for the
 * fix-a-day sheet: the one already said for that day leads. */
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

/** The last day, up to `today`, that an account keeps the base (or no base)
 * it has on `day`: it stops before a day that is not its Legend day. */
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

/** The run an account's day falls in. */
export function runOn(player: NotebookPlayer, day: number): NotebookRun | null {
  return player.runs.find((run) => run.from <= day && day <= run.to) ?? null;
}

/** Whether a base can be set on the account's `day`: a Legend day. */
export function isLegendDay(player: NotebookPlayer, day: number): boolean {
  return player.legendDays.includes(day);
}

/** The day's date, as "Tue 20 Oct", for the Season starting at `start`. */
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

/** An account's runs of one base, as "Days 5–9, Day 13". */
export function baseRuns(player: NotebookPlayer, code: string): string {
  return player.runs
    .filter((run) => run.base === code)
    .map((run) => daysText(run.from, run.to))
    .join(", ");
}

/** Where a notebook's page is: /notebook, or the Crew's Bases page. */
export function bookPath(notebook: Pick<Notebook, "crew">): string {
  return notebook.crew ? `/crews/${notebook.crew.id}/bases` : "/notebook";
}

/** A base's page in this notebook. */
export function basePath(notebook: Pick<Notebook, "crew">, code: string): string {
  return notebook.crew ? `${bookPath(notebook)}/${code}` : `/notebook/bases/${code}`;
}

/** Your own notebook is yours; in a Crew's, owners, admins and Uploaders
 * add bases. */
export function canAdd(notebook: Pick<Notebook, "crew">): boolean {
  return !notebook.crew || notebook.crew.role !== "member";
}

/** Retire, bring back or remove a base: in a Crew's notebook, owners and
 * admins any, an Uploader only bases they made or added. */
export function canManage(notebook: Pick<Notebook, "crew">, base: NotebookBase): boolean {
  const role = notebook.crew?.role;
  return (
    !role || role === "owner" || role === "admin" || (role === "uploader" && base.own)
  );
}
