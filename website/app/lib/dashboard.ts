/**
 * Dashboard cards, the default layout and each user's saved settings.
 *
 * Settings live in the account's existing `preferences` box under
 * `dashboard`, so they need no database change. Saved settings are read
 * leniently (unknown or unbuilt cards are dropped) so a card retired later
 * never breaks a page; posted settings are checked strictly.
 */

/** Small = 1 of the 3 columns, Medium = 2, Large = the full row. */
export type CardSize = "s" | "m" | "l";

export interface CardDefinition {
  title: string;
  size: CardSize;
  /** The one line shown in "+ Add a card". */
  description: string;
  /**
   * The card is on the page and in "+ Add a card". A card that is not built
   * yet stays out of both, even where the default layout names it.
   */
  built: boolean;
  /** The card has a "⚙ Settings" button while customising. */
  settings?: true;
}

export const CARD_SIZE_LABELS: Record<CardSize, string> = {
  s: "Small",
  m: "Medium",
  l: "Large",
};

/** The 13 cards in the order of the owner's card list (docs/dashboard.md). */
const CARD_LIST = {
  "legend-day": {
    title: "Legend day",
    size: "m",
    description:
      "Today's trophies, rank and Next Reset range, with your attacks and defenses.",
    built: false,
  },
  "legend-clock": {
    title: "Legend clock",
    size: "s",
    description: "Your attacks and defenses on a 24-hour dial in your time zone.",
    built: true,
  },
  bases: {
    title: "Bases you attacked",
    size: "l",
    description:
      "Each base you hit today, and how often it holds against other attackers.",
    built: false,
  },
  goal: {
    title: "Goal",
    size: "s",
    description:
      "Pick a rank to finish the Season at, and see the trophies a day you need.",
    built: true,
    settings: true,
  },
  "armies-defense": {
    title: "Armies on defense",
    size: "m",
    description: "Which armies attack you, and how your base holds against each.",
    built: false,
  },
  "rivals-watch": {
    title: "Rivals watch",
    size: "m",
    description: "The players just above and below you on the live leaderboard.",
    built: true,
  },
  "heat-map": {
    title: "Season heat map",
    size: "s",
    description: "Every day of the Season coloured by your gain or loss.",
    built: true,
  },
  cutoffs: {
    title: "Cutoffs",
    size: "s",
    description: "Trophies needed for Top 10, Top 200 and more, and your gap to each.",
    built: true,
    settings: true,
  },
  "season-graph": {
    title: "Season graph",
    size: "m",
    description: "Your trophies or rank at every Reset this Season.",
    built: true,
  },
  "recent-form": {
    title: "Recent form",
    size: "s",
    description:
      "Your average day over the last 3 or 7 days, and whether you are improving.",
    built: true,
  },
  "offense-consistency": {
    title: "Offense consistency",
    size: "s",
    description: "How often you triple, and what your misses cost.",
    built: true,
  },
  "group-watch": {
    title: "Group watch",
    size: "m",
    description: "How you compare with the players in one of your groups.",
    built: true,
    settings: true,
  },
  "armies-offense": {
    title: "Armies on offense",
    size: "s",
    description: "Your hit rate with each army, next to everyone else's.",
    built: false,
  },
} satisfies Record<string, CardDefinition>;

export type CardId = keyof typeof CARD_LIST;

export const CARDS: Record<CardId, CardDefinition> = CARD_LIST;

export const CARD_IDS = Object.keys(CARDS) as CardId[];

/**
 * The default layout, in order. Users on the default follow this list at
 * each release; cards not built yet are skipped until they are.
 */
export const DEFAULT_LAYOUT: readonly CardId[] = [
  "legend-day",
  "legend-clock",
  "bases",
  "goal",
  "armies-defense",
  "rivals-watch",
  "heat-map",
];

/** The 12 Top lines Cutoffs can show; a user picks 7. */
export const CUTOFF_LINES = [
  10, 20, 50, 100, 200, 300, 400, 500, 1000, 2000, 5000, 10000,
] as const;
export const CUTOFF_PICKS = 7;
export const DEFAULT_CUTOFFS: readonly number[] = [10, 50, 100, 200, 500, 1000, 5000];
export const DEFAULT_GOAL = 200;
/** The Goal can be any rank from 1 to 10,000, the owner's chosen range. */
export const MAX_GOAL = 10_000;

export type GroupWindow = "7" | "season";

export interface DashboardSettings {
  /** Null until the user picks a start in the first-visit pop-up. */
  layout: "default" | CardId[] | null;
  /** Goal: the finish rank. */
  goal: number;
  /** Cutoffs: 7 of CUTOFF_LINES, best first. */
  cutoffs: number[];
  /** Group watch: the group last picked on the card; null shows the first one made. */
  group: string | null;
  groupWindow: GroupWindow;
}

export function defaultSettings(): DashboardSettings {
  return {
    layout: null,
    goal: DEFAULT_GOAL,
    cutoffs: [...DEFAULT_CUTOFFS],
    group: null,
    groupWindow: "7",
  };
}

/** The cards a user sees, in order: the default list or their own, built cards only. */
export function shownCards(layout: DashboardSettings["layout"]): CardId[] {
  return (Array.isArray(layout) ? layout : DEFAULT_LAYOUT).filter(
    (id) => CARDS[id].built,
  );
}

/** Cards "+ Add a card" offers: every built card not already shown. */
export function addableCards(shown: readonly CardId[]): CardId[] {
  return CARD_IDS.filter((id) => CARDS[id].built && !shown.includes(id));
}

function isCardId(value: unknown): value is CardId {
  return typeof value === "string" && Object.hasOwn(CARDS, value);
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

function isGoal(value: unknown): value is number {
  return (
    Number.isSafeInteger(value) && (value as number) >= 1 && (value as number) <= MAX_GOAL
  );
}

function isCutoffs(value: unknown): value is number[] {
  return (
    Array.isArray(value) &&
    value.length === CUTOFF_PICKS &&
    value.every(
      (line, index) =>
        (CUTOFF_LINES as readonly unknown[]).includes(line) &&
        (index === 0 || (value[index - 1] as number) < line),
    )
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * The stored shape: `{ v: 3, layout: "default" | [card, ...], goal?, cutoffs?,
 * group?, window? }`. Settings left at their default are left out, which keeps
 * the box far under its 4,096-byte limit.
 */
export function serializeSettings(settings: DashboardSettings): Record<string, unknown> {
  const stored: Record<string, unknown> = { v: 3, layout: settings.layout ?? "default" };
  if (settings.goal !== DEFAULT_GOAL) stored.goal = settings.goal;
  if (settings.cutoffs.join() !== DEFAULT_CUTOFFS.join())
    stored.cutoffs = settings.cutoffs;
  if (settings.group) stored.group = settings.group;
  if (settings.groupWindow !== "7") stored.window = settings.groupWindow;
  return stored;
}

/**
 * Read saved settings. Anything missing or unreadable falls back to its
 * default; earlier test layouts (versions 1 and 2) count as a first visit.
 */
export function readSettings(value: unknown): DashboardSettings {
  const settings = defaultSettings();
  if (!isRecord(value) || value.v !== 3) return settings;
  if (value.layout === "default") settings.layout = "default";
  else if (Array.isArray(value.layout)) {
    settings.layout = [...new Set(value.layout.filter(isCardId))].filter(
      (id) => CARDS[id].built,
    );
  }
  if (isGoal(value.goal)) settings.goal = value.goal;
  if (isCutoffs(value.cutoffs)) settings.cutoffs = [...value.cutoffs];
  if (typeof value.group === "string" && UUID.test(value.group))
    settings.group = value.group;
  if (value.window === "season") settings.groupWindow = "season";
  return settings;
}

/** Check posted settings strictly. Returns null for anything the page would not send. */
export function parsePostedSettings(value: unknown): DashboardSettings | null {
  if (!isRecord(value) || value.v !== 3) return null;
  const allowed = new Set(["v", "layout", "goal", "cutoffs", "group", "window"]);
  if (Object.keys(value).some((key) => !allowed.has(key))) return null;
  const { layout } = value;
  if (
    layout !== "default" &&
    !(
      Array.isArray(layout) &&
      layout.every((id) => isCardId(id) && CARDS[id].built) &&
      new Set(layout).size === layout.length
    )
  )
    return null;
  if (value.goal !== undefined && !isGoal(value.goal)) return null;
  if (value.cutoffs !== undefined && !isCutoffs(value.cutoffs)) return null;
  if (
    value.group !== undefined &&
    !(typeof value.group === "string" && UUID.test(value.group))
  )
    return null;
  if (value.window !== undefined && value.window !== "season") return null;
  return readSettings(value);
}

/** The Reset is 05:00 UTC. Returns the next Reset after `nowMs`, in milliseconds. */
export function nextResetMs(nowMs: number): number {
  const reset = new Date(nowMs);
  reset.setUTCHours(5, 0, 0, 0);
  if (reset.getTime() <= nowMs) reset.setUTCDate(reset.getUTCDate() + 1);
  return reset.getTime();
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
  /** The profile's trophies, read with `observedAtMs`; null until the Season's reset shows. */
  trophies: number | null;
  /** The older of the profile and battle reads, Unix milliseconds. */
  observedAtMs: number | null;
  /** When today's battles were published, Unix milliseconds. */
  battlesObservedAtMs: number | null;
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
  const points = 100 * (held * legends.defenses - legends.held * defenses.length);
  const band = BASE_STRENGTH_BAND * defenses.length * legends.defenses;
  if (points > band) return "hard";
  if (points < -band) return "easy";
  return "average";
}

/** The ranks a player can still finish the Legend day between, best first. */
export interface RankRange {
  best: number;
  worst: number;
}
