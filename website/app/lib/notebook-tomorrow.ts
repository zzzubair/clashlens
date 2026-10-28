/**
 * The Tomorrow screen: every base a Clasher can reach, from their notebook
 * and every Crew they're in, ranked for one game account by how many of 8
 * defenses it should hold tomorrow against the armies that attack players
 * near that account's Reset trophies. The private API works it all out; this
 * file reads its answer.
 */

/** A base the account can reach: where it is, whether it is in the
 * Clasher's own notebook, and whether the account runs it from tomorrow. */
export interface ReachableBase {
  id: string;
  name: string;
  where: string[];
  mine: boolean;
  running: boolean;
}

/** One army near the account: its share of attacks there and, for a base,
 * the defenses it met near the account, how many it tripled, and the base's
 * chance to hold it. */
export interface WhyRow {
  name: string;
  share: number;
  defenses: number;
  tripled: number;
  holds: number;
}

/** Ranked: at least 24 defenses near the account; otherwise an early guess.
 * `width` is how far from the account's trophies defenses counted, doubled
 * (`widened`) when fewer than 24 were close enough. */
export interface ScoredBase extends ReachableBase {
  ranked: boolean;
  near: number;
  recent: number;
  width: number;
  widened: boolean;
  holds: number;
  low: number;
  high: number;
  lost: number;
  why: WhyRow[];
}

export interface Tomorrow {
  player: { tag: string; name: string | null };
  players: Array<{ tag: string; name: string | null }>;
  /** Tomorrow's Season day and weekday. */
  day: number;
  weekday: string;
  /** Null without live trophies for the account. */
  trophies: {
    now: number;
    attacksLeft: number;
    defensesLeft: number;
    about: number;
  } | null;
  width: number | null;
  gap: number | null;
  meta: Array<{ name: string; share: number }>;
  bases: ScoredBase[];
  untried: ReachableBase[];
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const isCount = (value: unknown): value is number =>
  Number.isInteger(value) && (value as number) >= 0;
const isShare = (value: unknown): value is number =>
  typeof value === "number" && value >= 0 && value <= 1;
const isHolds = (value: unknown): value is number =>
  typeof value === "number" && value >= 0 && value <= 8;
const isName = (value: unknown): value is string | null =>
  value === null || typeof value === "string";

function parsePlayer(value: unknown): Tomorrow["player"] | null {
  if (!isRecord(value) || typeof value.tag !== "string" || !isName(value.name))
    return null;
  return { tag: value.tag, name: value.name };
}

function parseReachable(value: unknown): ReachableBase | null {
  if (
    !isRecord(value) ||
    typeof value.id !== "string" ||
    typeof value.name !== "string" ||
    !Array.isArray(value.where) ||
    !value.where.every((item) => typeof item === "string") ||
    typeof value.mine !== "boolean" ||
    typeof value.running !== "boolean"
  )
    return null;
  return {
    id: value.id,
    name: value.name,
    where: value.where as string[],
    mine: value.mine,
    running: value.running,
  };
}

function parseWhy(value: unknown): WhyRow | null {
  if (
    !isRecord(value) ||
    typeof value.name !== "string" ||
    !isShare(value.share) ||
    !isCount(value.defenses) ||
    !isCount(value.tripled) ||
    !isShare(value.holds)
  )
    return null;
  return {
    name: value.name,
    share: value.share,
    defenses: value.defenses,
    tripled: value.tripled,
    holds: value.holds,
  };
}

function parseScored(value: unknown): ScoredBase | null {
  const base = parseReachable(value);
  if (
    base === null ||
    !isRecord(value) ||
    typeof value.ranked !== "boolean" ||
    !isCount(value.near) ||
    !isCount(value.recent) ||
    !isCount(value.width) ||
    typeof value.widened !== "boolean" ||
    !isHolds(value.holds) ||
    !isHolds(value.low) ||
    !isHolds(value.high) ||
    !isCount(value.lost) ||
    !Array.isArray(value.why)
  )
    return null;
  const why = value.why.map(parseWhy);
  if (why.some((row) => row === null)) return null;
  return {
    ...base,
    ranked: value.ranked,
    near: value.near,
    recent: value.recent,
    width: value.width,
    widened: value.widened,
    holds: value.holds,
    low: value.low,
    high: value.high,
    lost: value.lost,
    why: why as WhyRow[],
  };
}

function parseTrophies(value: unknown): Tomorrow["trophies"] | undefined {
  if (value === null) return null;
  if (
    !isRecord(value) ||
    !isCount(value.now) ||
    !isCount(value.attacks_left) ||
    !isCount(value.defenses_left) ||
    !isCount(value.about)
  )
    return undefined;
  return {
    now: value.now,
    attacksLeft: value.attacks_left,
    defensesLeft: value.defenses_left,
    about: value.about,
  };
}

/** The API's answer, or null when it doesn't fit the contract. */
export function parseTomorrow(value: unknown): Tomorrow | null {
  if (!isRecord(value) || value.kind !== "notebook-tomorrow") return null;
  const player = parsePlayer(value.player);
  const players = Array.isArray(value.players) ? value.players.map(parsePlayer) : [null];
  const trophies = parseTrophies(value.trophies);
  const meta = Array.isArray(value.meta)
    ? value.meta.map((item) =>
        isRecord(item) && typeof item.name === "string" && isShare(item.share)
          ? { name: item.name, share: item.share }
          : null,
      )
    : [null];
  const bases = Array.isArray(value.bases) ? value.bases.map(parseScored) : [null];
  const untried = Array.isArray(value.untried)
    ? value.untried.map(parseReachable)
    : [null];
  if (
    player === null ||
    players.some((item) => item === null) ||
    !Number.isInteger(value.day) ||
    typeof value.weekday !== "string" ||
    trophies === undefined ||
    !(value.width === null || isCount(value.width)) ||
    !(value.gap === null || isCount(value.gap)) ||
    meta.some((item) => item === null) ||
    bases.some((item) => item === null) ||
    untried.some((item) => item === null)
  )
    return null;
  return {
    player,
    players: players as Tomorrow["players"],
    day: value.day as number,
    weekday: value.weekday,
    trophies,
    width: value.width as number | null,
    gap: value.gap as number | null,
    meta: meta as Tomorrow["meta"],
    bases: bases as ScoredBase[],
    untried: untried as ReachableBase[],
  };
}

const number = new Intl.NumberFormat("en-GB");

/** "Holds 5.7 of 8". */
export function holdsText(base: ScoredBase): string {
  return `Holds ${base.holds.toFixed(1)} of 8`;
}

/** "likely 4.8–6.6 · about −150 trophies". */
export function rangeText(base: ScoredBase): string {
  return `likely ${base.low.toFixed(1)}–${base.high.toFixed(1)} · about −${number.format(base.lost)} trophies`;
}

/** "32 defenses within ±40 of you, 12 in the last 7 days". */
export function nearText(base: ScoredBase): string {
  const defenses = base.near === 1 ? "defense" : "defenses";
  const recent = base.recent
    ? `${base.recent} in the last 7 days`
    : "none in the last 7 days";
  return `${base.near} ${defenses} within ±${base.width} of you, ${recent}`;
}
