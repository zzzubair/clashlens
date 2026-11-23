import type { IconName } from "../components/DashboardIcon";
export const VIEW_AS_PARAM = "view_as";

export type CardSize = "s" | "m" | "l";

export interface CardDefinition {
  title: string;
  size: CardSize;
  description: string;
  icon: IconName;
  settings?: true;
}

export const CARD_SIZE_LABELS: Record<CardSize, string> = {
  s: "Small",
  m: "Medium",
  l: "Large",
};

const CARD_LIST = {
  "legend-day": {
    title: "Legend day",
    size: "m",
    description:
      "Today's trophies, rank and Next Reset range, with your attacks and defenses.",
    icon: "cal",
  },
  "legend-clock": {
    title: "Legend clock",
    size: "s",
    description: "Your attacks and defenses on a 24-hour dial in your time zone.",
    icon: "clock",
  },
  bases: {
    title: "Bases you attacked",
    size: "l",
    description:
      "Each base you hit today, and how many trophies it gives up against other attackers.",
    icon: "swords",
    settings: true,
  },
  goal: {
    title: "Goal",
    size: "s",
    description:
      "Pick a rank to finish the Season at, and see the trophies a day you need.",
    icon: "flag",
    settings: true,
  },
  "armies-defense": {
    title: "Armies on defense",
    size: "m",
    description: "Which armies attack you, and how your base holds against each.",
    icon: "shield",
  },
  "rivals-watch": {
    title: "Rivals watch",
    size: "m",
    description: "The players just above and below you on the live leaderboard.",
    icon: "users",
  },
  "heat-map": {
    title: "Season heat map",
    size: "s",
    description: "Every day of the Season coloured by your gain or loss.",
    icon: "grid",
  },
  cutoffs: {
    title: "Cutoffs",
    size: "s",
    description: "Trophies needed for Top 10, Top 200 and more, and your gap to each.",
    icon: "trophy",
    settings: true,
  },
  "season-graph": {
    title: "Season graph",
    size: "m",
    description: "Your trophies or rank at every Reset this Season.",
    icon: "chart",
  },
  "recent-form": {
    title: "Recent form",
    size: "s",
    description:
      "Your average day over the last 3 or 7 days, and whether you are improving.",
    icon: "bars",
  },
  "offense-consistency": {
    title: "Offense consistency",
    size: "s",
    description: "How often you triple, and what your misses cost.",
    icon: "star",
  },
  "group-watch": {
    title: "Watchlist",
    size: "m",
    description: "How you compare with the players in one of your watchlists.",
    icon: "list",
    settings: true,
  },
  "armies-offense": {
    title: "Armies on offense",
    size: "s",
    description: "Your hit rate with each army, next to everyone else's.",
    icon: "target",
  },
} satisfies Record<string, CardDefinition>;

export type CardId = keyof typeof CARD_LIST;

export const CARDS: Record<CardId, CardDefinition> = CARD_LIST;

export const CARD_IDS = Object.keys(CARDS) as CardId[];

export const ACCOUNT_CARDS: readonly CardId[] = ["group-watch"];

export function dashboardQuery(query: Record<string, string>, viewAs: string | null) {
  return `/?index&${new URLSearchParams({ ...query, ...(viewAs ? { [VIEW_AS_PARAM]: viewAs } : {}) })}`;
}

export const DEFAULT_LAYOUT: readonly CardId[] = [
  "legend-day",
  "legend-clock",
  "bases",
  "rivals-watch",
  "heat-map",
  "goal",
  "armies-defense",
];

export const CUTOFF_LINES = [
  10, 20, 50, 100, 200, 300, 400, 500, 1000, 2000, 5000, 10000,
] as const;
export const CUTOFF_PICKS = 7;
export const DEFAULT_CUTOFFS: readonly number[] = [10, 50, 100, 200, 500, 1000, 5000];
export const DEFAULT_GOAL = 200;
export const MAX_GOAL = 10_000;

export type GroupWindow = "7" | "season";

export interface DashboardSettings {
  layout: "default" | CardId[] | null;
  goal: number;
  cutoffs: number[];
  group: string | null;
  groupWindow: GroupWindow;
  basesMode: BasesMode;
}

export function defaultSettings(): DashboardSettings {
  return {
    layout: null,
    goal: DEFAULT_GOAL,
    cutoffs: [...DEFAULT_CUTOFFS],
    group: null,
    groupWindow: "7",
    basesMode: "trophies",
  };
}

export function shownCards(layout: DashboardSettings["layout"]): CardId[] {
  return [...(Array.isArray(layout) ? layout : DEFAULT_LAYOUT)];
}

export function cardPlaces(
  cards: readonly CardId[],
  columns: number,
  fill = true,
): { span: number; order: number }[] {
  const width = (index: number) =>
    Math.min({ s: 1, m: 2, l: columns }[CARDS[cards[index]].size], columns);
  const places = cards.map(() => ({ span: 0, order: 0 }));
  const waiting = cards.map((_, index) => index);
  let order = 0;
  let used = 0;
  let last = -1;
  const place = (index: number) => {
    places[index] = { span: width(index), order: order++ };
    used = (used + width(index)) % columns;
    last = index;
  };
  while (waiting.length > 0) {
    const room = columns - used;
    if (width(waiting[0]) > room) {
      const large = waiting.findIndex((index) => CARDS[cards[index]].size === "l");
      const fits = fill
        ? waiting.findIndex(
            (index, at) => at > 0 && (large < 0 || at < large) && width(index) <= room,
          )
        : -1;
      if (fits > 0) {
        place(waiting.splice(fits, 1)[0]);
        continue;
      }
      places[last].span += room;
      used = 0;
    }
    place(waiting.shift()!);
  }
  if (used > 0) places[last].span += columns - used;
  return places;
}

export function isDashboardSave(
  formMethod: string | undefined,
  formAction: string | undefined,
): boolean {
  return (
    formMethod?.toUpperCase() === "POST" &&
    formAction !== undefined &&
    new URL(formAction, "http://dashboard.invalid").pathname === "/"
  );
}

export function addableCards(shown: readonly CardId[]): CardId[] {
  return CARD_IDS.filter((id) => !shown.includes(id));
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

export function serializeSettings(settings: DashboardSettings): Record<string, unknown> {
  const stored: Record<string, unknown> = { v: 3, layout: settings.layout ?? "default" };
  if (settings.goal !== DEFAULT_GOAL) stored.goal = settings.goal;
  if (settings.cutoffs.join() !== DEFAULT_CUTOFFS.join())
    stored.cutoffs = settings.cutoffs;
  if (settings.group) stored.group = settings.group;
  if (settings.groupWindow !== "7") stored.window = settings.groupWindow;
  if (settings.basesMode !== "trophies") stored.bases = settings.basesMode;
  return stored;
}

export function readSettings(value: unknown): DashboardSettings {
  const settings = defaultSettings();
  if (!isRecord(value) || value.v !== 3) return settings;
  if (value.layout === "default") settings.layout = "default";
  else if (Array.isArray(value.layout)) {
    settings.layout = [...new Set(value.layout.filter(isCardId))];
  }
  if (isGoal(value.goal)) settings.goal = value.goal;
  if (isCutoffs(value.cutoffs)) settings.cutoffs = [...value.cutoffs];
  if (typeof value.group === "string" && UUID.test(value.group))
    settings.group = value.group;
  if (value.window === "season") settings.groupWindow = "season";
  if (value.bases === "held") settings.basesMode = "held";
  return settings;
}

export function parsePostedSettings(value: unknown): DashboardSettings | null {
  if (!isRecord(value) || value.v !== 3) return null;
  const allowed = new Set(["v", "layout", "goal", "cutoffs", "group", "window", "bases"]);
  if (Object.keys(value).some((key) => !allowed.has(key))) return null;
  const { layout } = value;
  if (
    layout !== "default" &&
    !(
      Array.isArray(layout) &&
      layout.every(isCardId) &&
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
  if (value.bases !== undefined && value.bases !== "held" && value.bases !== "trophies")
    return null;
  return readSettings(value);
}

export interface ClockBattle {
  at: number;
  kind: "attack" | "defense";
  stars: number;
  destruction: number;
  trophyChange: number;
  opponent: string | null;
}

export interface PlayerDay {
  dayStartMs: number;
  dayEndMs: number;
  dayNumber: number | null;
  dayCount: number | null;
  battles: ClockBattle[];
  complete: boolean;
  net: number | null;
  gained: number | null;
  lost: number | null;
  attacks: number | null;
  defenses: number | null;
  lastResetRank: number | null;
  trophies: number | null;
  observedAtMs: number | null;
  battlesObservedAtMs: number | null;
  openDefenses: number | null;
  autoDefenseEach: number | null;
}

export interface OpponentRow {
  tag: string;
  name: string | null;
  resetTrophies: number | null;
  hit: { stars: number; destruction: number; trophyChange: number; at: number };
  defenses: { stars: number; trophyChange: number; yours: boolean }[];
  observedAtMs: number | null;
  legends: LegendsHeld | null;
}

/**
 * Today's held share and trophies lost on defense among tracked Legend players
 * whose day started near the base's Reset trophies: within 20, widened until
 * 200 defenses but never past 300, and at least 50 defenses.
 */
export interface LegendsHeld {
  held: number;
  defenses: number;
  lost: number | null;
}

export type BaseStrength = "hard" | "average" | "easy" | "unavailable" | "early";
export type BasesMode = "held" | "trophies";

export const BASE_STRENGTH_BAND = 15;
export const BASE_TROPHY_BAND = 1.25;
export const BASE_STRENGTH_MIN_DEFENSES = 3;

export function trophiesGivenUp(defenses: OpponentRow["defenses"]): number {
  return -defenses.reduce((total, defense) => total + defense.trophyChange, 0);
}

export function baseStrength(
  defenses: OpponentRow["defenses"],
  legends: LegendsHeld | null,
  mode: BasesMode = "held",
): BaseStrength {
  if (defenses.length < BASE_STRENGTH_MIN_DEFENSES) return "early";
  if (!legends || legends.defenses === 0) return "unavailable";
  if (mode === "trophies") {
    if (legends.lost === null) return "unavailable";
    // Compared without rounding: trophies fewer than the Legends' over all
    // the base's defenses, both scaled by the Legends' defense count.
    const fewer =
      legends.lost * defenses.length - trophiesGivenUp(defenses) * legends.defenses;
    const band = BASE_TROPHY_BAND * defenses.length * legends.defenses;
    if (fewer > band) return "hard";
    if (fewer < -band) return "easy";
    return "average";
  }
  // Compared without rounding: the base's share against the Legends', both
  // scaled by both defense counts.
  const held = defenses.filter((defense) => defense.stars < 3).length;
  const points = 100 * (held * legends.defenses - legends.held * defenses.length);
  const band = BASE_STRENGTH_BAND * defenses.length * legends.defenses;
  if (points > band) return "hard";
  if (points < -band) return "easy";
  return "average";
}

export interface RankRange {
  best: number;
  worst: number;
}
