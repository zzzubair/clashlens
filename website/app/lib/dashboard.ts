/**
 * Dashboard cards and the saved layout.
 *
 * The layout lives in the account's existing `preferences` box under
 * `dashboard`, so it needs no database change. Saved layouts are read
 * leniently (unknown cards are dropped) so a card retired later never breaks
 * a page; a posted layout is checked strictly.
 */

import { normalizePlayerTag } from "./player-tag";

/** Small = 1 of the 3 columns, Medium = 2, Large = the full row. */
export type CardSize = "s" | "m" | "l";
export type DashboardTab = "today" | "season" | "crew";

/** How ready the card's data is, shown as a tag in the card picker. */
export type CardData = "ready" | "new-read" | "estimate" | "new-data" | "not-ready";

export interface CardDefinition {
  title: string;
  icon: IconName;
  /** The only tab the card can be placed on. */
  tab: DashboardTab;
  /** Each card has one size and one design. */
  size: CardSize;
  /** One line on what the card shows. */
  what: string;
  data: CardData;
  /** The card shows one Clash player, so it can be pinned to one. */
  perPlayer: boolean;
  /** Only in "Add a card", not on a new layout. */
  offByDefault?: boolean;
}

export type IconName =
  | "clock"
  | "shield"
  | "target"
  | "flag"
  | "bars"
  | "ghost"
  | "list"
  | "chart"
  | "swords"
  | "book"
  | "cal"
  | "grid"
  | "users"
  | "cup"
  | "link"
  | "shieldOff"
  | "trophy"
  | "star";

export const DASHBOARD_TABS: { id: DashboardTab; label: string }[] = [
  { id: "today", label: "Today" },
  { id: "season", label: "Season" },
  { id: "crew", label: "Crew" },
];

export const CARD_SIZE_LABELS: Record<CardSize, string> = {
  s: "Small",
  m: "Medium",
  l: "Large",
};

const card = (
  title: string,
  icon: IconName,
  tab: DashboardTab,
  size: CardSize,
  what: string,
  data: CardData,
  extra: { perPlayer?: boolean; offByDefault?: boolean } = {},
): CardDefinition => ({
  title,
  icon,
  tab,
  size,
  what,
  data,
  perPlayer: extra.perPlayer ?? true,
  ...(extra.offByDefault ? { offByDefault: true } : {}),
});

/** The cards in the order of the owner's card list. */
const CARD_LIST = {
  legendday: card(
    "Legend day",
    "trophy",
    "today",
    "m",
    "Live trophies, net today, rank at the last Reset, now and the next Reset range.",
    "ready",
  ),
  clock: card(
    "Legend clock",
    "clock",
    "today",
    "s",
    "A 24-hour dial in your time zone, Reset on top, your battles where they landed.",
    "ready",
  ),
  opponents: card(
    "Bases you attacked",
    "target",
    "today",
    "l",
    "Your hit on each base and how it held today against the Legends average.",
    "ready",
  ),
  cutoffs: card(
    "Cutoffs & goal",
    "bars",
    "today",
    "m",
    "Top 200 / 1,000 / #10,000 lines and your gap; your goal and what you need per day.",
    "new-data",
  ),
  shield: card(
    "Shield tomorrow?",
    "shield",
    "today",
    "s",
    "What playing tomorrow wins on an average day, and what a shield saves.",
    "new-read",
  ),
  around: card(
    "Players around you",
    "list",
    "today",
    "s",
    "The players above and below you on the live board.",
    "ready",
  ),
  ghost: card(
    "Ghost race",
    "ghost",
    "today",
    "m",
    "Your trophies today against the player above you or your goal pace.",
    "ready",
  ),
  saved: card(
    "Saved players",
    "users",
    "today",
    "s",
    "Live numbers for players you saved.",
    "ready",
    { perPlayer: false, offByDefault: true },
  ),
  daily: card(
    "Day by day",
    "bars",
    "season",
    "l",
    "Each finished day's gain or loss against your average, Season days 1–28.",
    "ready",
  ),
  rankreset: card(
    "Rank at each Reset",
    "chart",
    "season",
    "m",
    "Your rank at every Reset this Season.",
    "ready",
  ),
  trend: card(
    "Trophy trend",
    "chart",
    "season",
    "s",
    "Trophies gained or lost over the last 7 or 14 finished days.",
    "ready",
  ),
  attack: card(
    "Attack stats",
    "swords",
    "season",
    "s",
    "Hit rate, triples, average stars and destruction.",
    "ready",
  ),
  defense: card(
    "Defense stats",
    "shield",
    "season",
    "s",
    "Defenses held (not tripled) and average defense stars.",
    "ready",
  ),
  past: card(
    "Past Seasons",
    "cal",
    "season",
    "s",
    "Official final rank and trophies.",
    "ready",
  ),
  heat: card(
    "Season heat map",
    "grid",
    "season",
    "s",
    "Each day coloured against your own average.",
    "ready",
    { offByDefault: true },
  ),
  notebook: card(
    "Base notebook",
    "book",
    "season",
    "l",
    "On hold while the notebook is decided.",
    "not-ready",
    { offByDefault: true },
  ),
  rivals: card(
    "Rivals race",
    "users",
    "crew",
    "l",
    "Your crew's gains since Reset or over 7 days.",
    "new-read",
    { perPlayer: false },
  ),
  h2h: card(
    "Head-to-head",
    "swords",
    "crew",
    "m",
    "You against one crew member, side by side.",
    "new-read",
  ),
  invite: card(
    "Invite",
    "link",
    "crew",
    "s",
    "An invite link; joiners pick which of their accounts join.",
    "new-data",
    { perPlayer: false },
  ),
  cup: card(
    "Crew cup",
    "cup",
    "crew",
    "s",
    "A 7-day mini competition inside a crew.",
    "new-data",
    { perPlayer: false, offByDefault: true },
  ),
} satisfies Record<string, CardDefinition>;

export type CardId = keyof typeof CARD_LIST;

export const CARDS: Record<CardId, CardDefinition> = CARD_LIST;

export const CARD_IDS = Object.keys(CARDS) as CardId[];

/** One card on a tab. `player` pins it to one Clash player; null shows the switcher's. */
export interface PlacedCard {
  card: CardId;
  player: string | null;
}

export interface DashboardLayout {
  tabs: Record<DashboardTab, PlacedCard[]>;
  /** An IANA time zone such as "Europe/London", or "auto" for the device's. */
  timeZone: string;
}

/** Enough for every card twice on one tab. */
export const MAX_CARDS_PER_TAB = 36;

export function defaultTab(tab: DashboardTab): PlacedCard[] {
  return CARD_IDS.filter((id) => CARDS[id].tab === tab && !CARDS[id].offByDefault).map(
    (id) => ({ card: id, player: null }),
  );
}

export function defaultLayout(): DashboardLayout {
  return {
    tabs: {
      today: defaultTab("today"),
      season: defaultTab("season"),
      crew: defaultTab("crew"),
    },
    timeZone: "auto",
  };
}

export function isDashboardTab(value: unknown): value is DashboardTab {
  return value === "today" || value === "season" || value === "crew";
}

function isCardId(value: unknown): value is CardId {
  return typeof value === "string" && Object.hasOwn(CARDS, value);
}

export function isTimeZone(value: unknown): value is string {
  if (value === "auto") return true;
  if (typeof value !== "string" || value.length === 0 || value.length > 64) return false;
  try {
    new Intl.DateTimeFormat("en-GB", { timeZone: value });
    return true;
  } catch {
    return false;
  }
}

/**
 * The stored shape: `{ v: 2, tz, today: [[card, tag?], ...], ... }`.
 * Short keys keep a full layout well under the 4,096-byte preferences limit.
 */
export function serializeLayout(layout: DashboardLayout): Record<string, unknown> {
  const stored: Record<string, unknown> = { v: 2, tz: layout.timeZone };
  for (const { id } of DASHBOARD_TABS) {
    stored[id] = layout.tabs[id].map(({ card, player }) =>
      player ? [card, player] : [card],
    );
  }
  return stored;
}

/**
 * Read a saved layout. Anything missing or unreadable falls back to the
 * default. Version 1 layouts also carried a size, which is now fixed per card.
 */
export function readSavedLayout(value: unknown): DashboardLayout {
  const layout = defaultLayout();
  if (!isRecord(value) || (value.v !== 1 && value.v !== 2)) return layout;
  const tagAt = value.v === 1 ? 2 : 1;
  if (isTimeZone(value.tz)) layout.timeZone = value.tz;
  for (const { id } of DASHBOARD_TABS) {
    const saved = value[id];
    if (!Array.isArray(saved)) continue;
    layout.tabs[id] = saved.slice(0, MAX_CARDS_PER_TAB).flatMap((item) => {
      if (!Array.isArray(item) || !isCardId(item[0])) return [];
      const definition = CARDS[item[0]];
      if (definition.tab !== id) return [];
      const player =
        definition.perPlayer && typeof item[tagAt] === "string"
          ? normalizePlayerTag(item[tagAt])
          : null;
      return [{ card: item[0], player }];
    });
  }
  return layout;
}

/** Check a posted layout strictly. Returns null for anything the page would not send. */
export function parsePostedLayout(value: unknown): DashboardLayout | null {
  if (!isRecord(value) || value.v !== 2 || !isTimeZone(value.tz)) return null;
  const tabs = {} as Record<DashboardTab, PlacedCard[]>;
  for (const { id } of DASHBOARD_TABS) {
    const posted = value[id];
    if (!Array.isArray(posted) || posted.length > MAX_CARDS_PER_TAB) return null;
    const cards: PlacedCard[] = [];
    for (const item of posted) {
      if (!Array.isArray(item) || item.length < 1 || item.length > 2) return null;
      const [card, player] = item as unknown[];
      if (!isCardId(card) || CARDS[card].tab !== id) return null;
      if (player === undefined) {
        cards.push({ card, player: null });
        continue;
      }
      if (!CARDS[card].perPlayer || typeof player !== "string") return null;
      const tag = normalizePlayerTag(player);
      if (tag !== player) return null;
      cards.push({ card, player: tag });
    }
    tabs[id] = cards;
  }
  return { tabs, timeZone: value.tz };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** One battle of the current Legend day, placed on the Legend clock. */
export interface ClockBattle {
  /** Unix milliseconds. */
  at: number;
  kind: "attack" | "defense";
  stars: number;
  destruction: number;
  trophyChange: number;
  /** The other player's in-game name, when known. */
  opponent: string | null;
}

/** The current Legend day of one player, from their player page. */
export interface PlayerDay {
  dayNumber: number | null;
  dayCount: number | null;
  battles: ClockBattle[];
  /** Every battle of the day so far is in `battles`. */
  complete: boolean;
  net: number | null;
  attacks: number | null;
  defenses: number | null;
  /** Rank on the board frozen at the Reset that started this Legend day. */
  lastResetRank: number | null;
  /** When the player's newest profile was read, Unix milliseconds. */
  observedAtMs: number | null;
  /** Defense slots still open; the game charges each at Reset. */
  openDefenses: number | null;
  /** The game's automatic loss for each defense still open at Reset, when known. */
  autoDefenseEach: number | null;
}

/** One of the user's attacks today and how that base has held today. */
export interface OpponentRow {
  tag: string;
  name: string | null;
  /** The opponent's trophies at the Reset that started today. */
  resetTrophies: number | null;
  hit: { stars: number; destruction: number; trophyChange: number; at: number };
  /** The opponent's defenses today in time order; `yours` marks this attack. */
  defenses: { stars: number; yours: boolean }[];
  /** When the opponent's newest battle log was read, Unix milliseconds. */
  observedAtMs: number | null;
}

/** Today's held share across all tracked Legend players. */
export interface LegendsHeld {
  held: number;
  defenses: number;
}

export type BaseStrength = "hard" | "average" | "easy" | "early";

/** Average is within 15 points of today's Legends held share; under 3 defenses is too early. */
export const BASE_STRENGTH_BAND = 15;
export const BASE_STRENGTH_MIN_DEFENSES = 3;

export function baseStrength(
  defenses: OpponentRow["defenses"],
  legends: LegendsHeld | null,
): BaseStrength {
  if (
    defenses.length < BASE_STRENGTH_MIN_DEFENSES ||
    !legends ||
    legends.defenses === 0
  ) {
    return "early";
  }
  const held = defenses.filter((defense) => defense.stars < 3).length;
  const points = (held / defenses.length) * 100 - (legends.held / legends.defenses) * 100;
  if (points > BASE_STRENGTH_BAND) return "hard";
  if (points < -BASE_STRENGTH_BAND) return "easy";
  return "average";
}

/** The ranks a player can still finish the Legend day between, best first. */
export interface RankRange {
  best: number;
  worst: number;
}

/** The Reset is 05:00 UTC. Returns the next Reset after `nowMs`, in milliseconds. */
export function nextResetMs(nowMs: number): number {
  const reset = new Date(nowMs);
  reset.setUTCHours(5, 0, 0, 0);
  if (reset.getTime() <= nowMs) reset.setUTCDate(reset.getUTCDate() + 1);
  return reset.getTime();
}
