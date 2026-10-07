/**
 * Dashboard cards and the saved layout.
 *
 * The layout lives in the account's existing `preferences` box under
 * `dashboard`, so it needs no database change. Saved layouts are read
 * leniently (unknown cards are dropped, a bad size falls back to the card's
 * default) so a card retired later never breaks a page; a posted layout is
 * checked strictly.
 */

import { normalizePlayerTag } from "./player-tag";

export type CardSize = "s" | "l" | "xl";
export type DashboardTab = "today" | "season" | "crew";

/** How ready the card's data is, shown as a tag in the card picker. */
export type CardData = "ready" | "new-read" | "estimate" | "new-data" | "not-ready";

export interface CardDefinition {
  title: string;
  icon: IconName;
  /** The tab the card starts on, and its picker filter. */
  tab: DashboardTab;
  sizes: CardSize[];
  defaultSize: CardSize;
  /** One line on what the card shows. */
  what: string;
  data: CardData;
  /** The card shows one Clash player, so it can be pinned to one. */
  perPlayer: boolean;
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
  | "shieldOff";

export const DASHBOARD_TABS: { id: DashboardTab; label: string }[] = [
  { id: "today", label: "Today" },
  { id: "season", label: "Season" },
  { id: "crew", label: "Crew" },
];

export const CARD_SIZES: CardSize[] = ["s", "l", "xl"];

const CARD_LIST = {
  clock: {
    title: "Legend clock",
    icon: "clock",
    tab: "today",
    sizes: ["s", "l", "xl"],
    defaultSize: "l",
    what: "Time to Reset in your time zone, your battles, live trophies and rank.",
    data: "ready",
    perPlayer: true,
  },
  shield: {
    title: "Shield tomorrow?",
    icon: "shield",
    tab: "today",
    sizes: ["s", "l"],
    defaultSize: "s",
    what: "What a shielded day keeps or saves you.",
    data: "estimate",
    perPlayer: true,
  },
  around: {
    title: "Players around you",
    icon: "list",
    tab: "today",
    sizes: ["s", "l"],
    defaultSize: "s",
    what: "The live leaderboard two places above and below you.",
    data: "ready",
    perPlayer: true,
  },
  opponents: {
    title: "Bases you attacked",
    icon: "target",
    tab: "today",
    sizes: ["l", "xl"],
    defaultSize: "xl",
    what: "Today's opponents and how their base holds against the Legends average.",
    data: "new-read",
    perPlayer: true,
  },
  goal: {
    title: "Season goal",
    icon: "flag",
    tab: "today",
    sizes: ["s", "l"],
    defaultSize: "l",
    what: "Your Season-end goal and where your usual pace takes you.",
    data: "estimate",
    perPlayer: true,
  },
  cutoffs: {
    title: "Cutoffs right now",
    icon: "bars",
    tab: "today",
    sizes: ["s", "l"],
    defaultSize: "l",
    what: "Live Top 200, Top 1,000 and demotion lines, and your distance to each.",
    data: "ready",
    perPlayer: true,
  },
  ghost: {
    title: "Ghost race",
    icon: "ghost",
    tab: "today",
    sizes: ["l", "xl"],
    defaultSize: "xl",
    what: "Your trophies through the day against a ghost you pick.",
    data: "ready",
    perPlayer: true,
  },
  rankreset: {
    title: "Rank at each Reset",
    icon: "chart",
    tab: "season",
    sizes: ["s", "l", "xl"],
    defaultSize: "l",
    what: "Your rank at every Reset this Season, the next one estimated.",
    data: "ready",
    perPlayer: true,
  },
  daily: {
    title: "Day by day",
    icon: "bars",
    tab: "season",
    sizes: ["l", "xl"],
    defaultSize: "l",
    what: "Each Legend day's gain or loss against your own average.",
    data: "ready",
    perPlayer: true,
  },
  attack: {
    title: "Attack stats",
    icon: "swords",
    tab: "season",
    sizes: ["s", "l"],
    defaultSize: "s",
    what: "Triple rate, average stars and destruction. 7 days, 14 days or Season.",
    data: "ready",
    perPlayer: true,
  },
  defense: {
    title: "Defense stats",
    icon: "shield",
    tab: "season",
    sizes: ["s", "l"],
    defaultSize: "s",
    what: "Defenses held (not three-starred) and average defense stars.",
    data: "ready",
    perPlayer: true,
  },
  past: {
    title: "Past Seasons",
    icon: "cal",
    tab: "season",
    sizes: ["s", "l"],
    defaultSize: "l",
    what: "Your last three Season finishes.",
    data: "ready",
    perPlayer: true,
  },
  notebook: {
    title: "Base notebook",
    icon: "book",
    tab: "season",
    sizes: ["l", "xl"],
    defaultSize: "xl",
    what: "Bases you attacked and saved this Season, with Find This Base.",
    data: "new-data",
    perPlayer: true,
  },
  heat: {
    title: "Season heat map",
    icon: "grid",
    tab: "season",
    sizes: ["l", "xl"],
    defaultSize: "l",
    what: "Each day coloured against your own average.",
    data: "ready",
    perPlayer: true,
    offByDefault: true,
  },
  breaks: {
    title: "What breaks your defense",
    icon: "shieldOff",
    tab: "season",
    sizes: ["l"],
    defaultSize: "l",
    what: "Your defenses split by the attacking army type.",
    data: "not-ready",
    perPlayer: true,
    offByDefault: true,
  },
  rivals: {
    title: "Rivals race",
    icon: "users",
    tab: "crew",
    sizes: ["l", "xl"],
    defaultSize: "xl",
    what: "Who in your crew gained most since Reset, or over 7 days.",
    data: "new-read",
    perPlayer: false,
  },
  cup: {
    title: "Crew cup",
    icon: "cup",
    tab: "crew",
    sizes: ["l", "xl"],
    defaultSize: "l",
    what: "A small tournament inside your crew: most trophies gained wins.",
    data: "new-data",
    perPlayer: false,
  },
  invite: {
    title: "Invite",
    icon: "link",
    tab: "crew",
    sizes: ["s", "l"],
    defaultSize: "l",
    what: "A link that lets friends join with one or all of their accounts.",
    data: "new-data",
    perPlayer: false,
  },
} satisfies Record<string, CardDefinition>;

export type CardId = keyof typeof CARD_LIST;

export const CARDS: Record<CardId, CardDefinition> = CARD_LIST;

export const CARD_IDS = Object.keys(CARDS) as CardId[];

/** One card on a tab. `player` pins it to one Clash player; null follows the switcher. */
export interface PlacedCard {
  card: CardId;
  size: CardSize;
  player: string | null;
}

export interface DashboardLayout {
  tabs: Record<DashboardTab, PlacedCard[]>;
  /** An IANA time zone such as "Europe/London", or "auto" for the device's. */
  timeZone: string;
}

/** Enough for every card twice on one tab. */
export const MAX_CARDS_PER_TAB = 36;

const DEFAULT_TABS: Record<DashboardTab, CardId[]> = {
  today: ["clock", "shield", "around", "opponents", "goal", "cutoffs", "ghost"],
  season: ["rankreset", "daily", "attack", "defense", "past", "notebook"],
  crew: ["rivals", "cup", "invite"],
};

export function defaultTab(tab: DashboardTab): PlacedCard[] {
  return DEFAULT_TABS[tab].map((card) => ({
    card,
    size: CARDS[card].defaultSize,
    player: null,
  }));
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

function isCardSize(value: unknown): value is CardSize {
  return value === "s" || value === "l" || value === "xl";
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
 * The stored shape: `{ v: 1, tz, today: [[card, size, tag?], ...], ... }`.
 * Short keys keep a full layout well under the 4,096-byte preferences limit.
 */
export function serializeLayout(layout: DashboardLayout): Record<string, unknown> {
  const stored: Record<string, unknown> = { v: 1, tz: layout.timeZone };
  for (const { id } of DASHBOARD_TABS) {
    stored[id] = layout.tabs[id].map(({ card, size, player }) =>
      player ? [card, size, player] : [card, size],
    );
  }
  return stored;
}

/** Read a saved layout. Anything missing or unreadable falls back to the default. */
export function readSavedLayout(value: unknown): DashboardLayout {
  const layout = defaultLayout();
  if (!isRecord(value) || value.v !== 1) return layout;
  if (isTimeZone(value.tz)) layout.timeZone = value.tz;
  for (const { id } of DASHBOARD_TABS) {
    const saved = value[id];
    if (!Array.isArray(saved)) continue;
    layout.tabs[id] = saved.slice(0, MAX_CARDS_PER_TAB).flatMap((item) => {
      if (!Array.isArray(item) || !isCardId(item[0])) return [];
      const definition = CARDS[item[0]];
      const size = definition.sizes.includes(item[1])
        ? (item[1] as CardSize)
        : definition.defaultSize;
      const player =
        definition.perPlayer && typeof item[2] === "string"
          ? normalizePlayerTag(item[2])
          : null;
      return [{ card: item[0], size, player }];
    });
  }
  return layout;
}

/** Check a posted layout strictly. Returns null for anything the page would not send. */
export function parsePostedLayout(value: unknown): DashboardLayout | null {
  if (!isRecord(value) || value.v !== 1 || !isTimeZone(value.tz)) return null;
  const tabs = {} as Record<DashboardTab, PlacedCard[]>;
  for (const { id } of DASHBOARD_TABS) {
    const posted = value[id];
    if (!Array.isArray(posted) || posted.length > MAX_CARDS_PER_TAB) return null;
    const cards: PlacedCard[] = [];
    for (const item of posted) {
      if (!Array.isArray(item) || item.length < 2 || item.length > 3) return null;
      const [card, size, player] = item as unknown[];
      if (!isCardId(card) || !isCardSize(size)) return null;
      const definition = CARDS[card];
      if (!definition.sizes.includes(size)) return null;
      if (player === undefined) {
        cards.push({ card, size, player: null });
        continue;
      }
      if (!definition.perPlayer || typeof player !== "string") return null;
      const tag = normalizePlayerTag(player);
      if (tag !== player) return null;
      cards.push({ card, size, player: tag });
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
  trophyChange: number;
}

/** The current Legend day of one player, from their player page. */
export interface PlayerDay {
  dayNumber: number | null;
  dayCount: number | null;
  battles: ClockBattle[];
}

/** The Reset is 05:00 UTC. Returns the next Reset after `nowMs`, in milliseconds. */
export function nextResetMs(nowMs: number): number {
  const reset = new Date(nowMs);
  reset.setUTCHours(5, 0, 0, 0);
  if (reset.getTime() <= nowMs) reset.setUTCDate(reset.getUTCDate() + 1);
  return reset.getTime();
}
