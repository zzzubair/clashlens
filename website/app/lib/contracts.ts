import type { ArmyUnitNames } from "./army";

export type FreshnessState = "fresh" | "stale" | "unknown";
export type ConfidenceState = "high" | "partial" | "uncertain";
export type CoverageState = "complete" | "partial" | "missing" | "unknown";

export type WebsiteErrorCode =
  | "invalid_input"
  | "missing"
  | "forbidden"
  | "conflict"
  | "rate_limited"
  | "uncertain"
  | "malformed"
  | "unavailable";

export type WebsiteErrorResponse = {
  error: {
    code: WebsiteErrorCode;
    message: string;
    retryAfterSeconds?: number;
    fieldErrors?: Record<string, string>;
    affectedDays?: number[];
  };
};

export interface DataProvenance {
  source: string;
  observedAt: string | null;
  freshness: FreshnessState;
  confidence: ConfidenceState;
  coverage: CoverageState;
  version: string;
}

export interface Freshness {
  state: FreshnessState;
  observedAt: string;
  ageSeconds: number;
}

export interface TrackedPlayerEntry {
  rank: number;
  tag: string;
  name: string;
  clan: string;
  trophies: number;
  freshness: Freshness;
  state: "available" | "stale" | "uncertain";
  confidence: ConfidenceState;
  /** Another player on the board has the same trophies. */
  tied?: boolean;
  /** Place on the Daily board published at the last Reset, this Season; null
   * off that board, on a Season's first Legend day and on Daily boards. */
  resetRank: number | null;
  /** Trophies won or lost today so far, from the Legend day's settled start,
   * as the dashboard's Legend day card shows them; null until today's saved
   * battles are complete, and on Daily boards. */
  todayChange: number | null;
  /** This Season's last 7 finished Legend days, oldest first. Empty on a
   * Season's first Legend day and on Daily boards. */
  recentDays: RecentDay[];
}

/** One finished Legend day of a Live row's chart. */
export interface RecentDay {
  /** The Reset that started the day. */
  start: string;
  /** The day's result as the player page's Daily Legend log shows it; null
   * when it shows none, such as before the day's result is saved. */
  trophyChange: number | null;
}

export interface SnapshotSelector {
  officialSeasonId: string;
  dayNumber: number;
}

export interface LeaderboardSearch {
  exactTag: string | null;
  hasMore: boolean;
  results: Array<Pick<TrackedPlayerEntry, "tag" | "name" | "rank" | "trophies">>;
}

export interface TrackedLeaderboard {
  kind: "tracked-leaderboard";
  view: "live" | "daily";
  entries: TrackedPlayerEntry[];
  totalTracked: number;
  /** Tracked players left off the Live board until their profile names this Season. */
  seasonResetPending?: number;
  totalEntries: number;
  page: number;
  pageSize: number;
  pageCount: number;
  generatedAt: string;
  hasPrevious: boolean;
  hasNext: boolean;
  daily:
    | (SnapshotSelector & {
        resetAt: string;
        seasonStartAt: string;
        seasonEndAt: string;
        previousSnapshot: SnapshotSelector | null;
        nextSnapshot: SnapshotSelector | null;
      })
    | null;
  coverage: {
    state: CoverageState;
    trackedPlayers: number;
    measuredPercent: number;
    note: string;
  };
  provenance: DataProvenance;
  sourceObservations: {
    oldestObservedAt: string | null;
    newestObservedAt: string | null;
    staleCount: number;
  } | null;
  qualityStates: Array<
    | "missing"
    | "partial"
    | "stale"
    | "malformed"
    | "unclassified"
    | "uncertain"
    | "rate-limited"
    | "unavailable"
  >;
}

export interface KnownPlayerResult {
  tag: string;
  name: string;
  clan: string;
  /** Null while the latest profile is from before this player's Season reset. */
  trophies: number | null;
  freshness: Freshness;
  state: "available" | "stale" | "uncertain";
  context: string;
}

export interface SearchResponse {
  kind: "player-search";
  query: string;
  exactTag: string | null;
  results: KnownPlayerResult[];
  users: import("./account-contracts").PublicUserResult[];
  knownOnly: boolean;
}

export interface PlayerProfile {
  tag: string;
  name: string;
  clan: string;
  /** Null when the saved profile names no clan. */
  clanTag?: string | null;
  trophies: number;
  /** The profile still names an earlier Season, so `trophies` predates this player's Season reset. */
  seasonResetPending?: boolean;
  /** The Season the profile names; its trophies count only for that Season's days. */
  currentLeagueSeasonId?: string | null;
  freshness: Freshness;
  battleHistoryUpdatedAt?: string | null;
  /** When Clash of Clans last did not find this player, if after its last successful check. */
  notFoundAt?: string | null;
  confidence: ConfidenceState;
  coverage: CoverageState;
  eligibility: "legend-i" | "uncertain";
}

/** Shown site-wide when new data is not reaching players. */
export interface UpdateStatus {
  checkedAt: string;
  /** Set when no Clash of Clans API answer arrived for the delay limit. */
  lastCollectedAt: string | null;
  /** When the oldest data still waiting to be processed was saved, once that wait passes the delay limit. */
  oldestWaitingSavedAt: string | null;
}

export interface ArmyAnalytics {
  pagination?: { offset: number; totalRows: number; nextOffset: number | null };
  kind: "army-analytics";
  selection: {
    lens: "offense" | "defense";
    season: string;
    startDay: number;
    endDay: number;
    population: string;
    category: string;
    sort: string;
  };
  totalAttacks: number;
  usableArmySample: number;
  armyStates: Record<string, number>;
  armyStatesSumConfirmed: boolean;
  unknownAffectedAttacks: number;
  unknownComponentOccurrences: number;
  perspectiveDisagreementCount: number;
  missingTrophyMembershipEvidence: number;
  cohortEvidence: {
    cohortPlayers: number;
    staleOrUncertainCohortMembers: number;
    streakExcludedPlayers: number;
    shieldedPlayerDays: number;
  };
  collectionCoverage: {
    state: string;
    completedDays: number;
    coveredDays?: number[];
    streakGapDays?: number[];
  };
  freshness: { state: string };
  reproducibility: {
    officialSeasonId: string;
    legendDays: [number, number];
    snapshotVersions: number[];
  };
  versions: { decoder: string; catalog: string; analytics: string };
  publicationIdentity: string;
  rows: Array<{
    key: string;
    label: string;
    usageCount: number;
    usageDenominator: number;
    usageRate: number;
    quantity?: number;
    oneStarCount?: number;
    twoStarCount?: number;
    threeStarCount?: number;
    starCounts?: [number, number, number, number];
    starRates?: [number, number, number, number];
    threeStarRate?: number;
    averageStars?: number;
    averageDestruction?: number;
    /** Armies only: trophies an attack won, and a non-3-star attack (null without one). */
    averageTrophies?: number;
    averageMissTrophies?: number | null;
    unknownExcludedAttacks?: number;
    /** Armies only: the type's variants by hero equipment, its Other last. */
    variants?: ArmyAnalytics["rows"];
    /** Armies only: the type's id. */
    typeId?: number;
    /** A variant: its id (0 for its type's Other). */
    variantId?: number;
  }>;
}

/** A variant's most-used armies, for the same players and Legend days as its row. */
export interface ArmyVariantExamples {
  /** What decides the variant, or what falls in its type's Other. */
  rule: string;
  /** Most used first, at most 3: armies alike but for small fillers. */
  groups: Array<{
    attacks: number;
    /** The group's most-used exact army: a real army, as Copy army copies it. */
    armyShareCode: string;
  }>;
  armyUnits: ArmyUnitNames;
}

export interface RankedBattleEvent {
  battleId: string;
  battleTimestamp: string;
  opponent: {
    tag: string;
    name: string | null;
  };
  destructionPercentage: number;
  stars: number;
  trophyChange: number;
  perspectiveDisagreement: boolean;
  armyShareCode?: string | null;
}

export interface RankedDaySummary {
  dayNumber: number | null;
  label: string;
  period: string;
  state: "Live" | "Complete" | "Partial" | "Uncertain";
  // How the saved day was proven: "exact" when its Reset reading matched
  // every part of its calculation, "inferred" when a part is calculated.
  confidence?: "exact" | "inferred" | "partial" | "uncertain" | null;
  // Whether the day's battle logs form an unbroken chain, so no battle can
  // be missing; separate from completeness, which also needs the readings.
  logCoverage?: "complete" | "partial";
  startTrophies?: number | null;
  startTrophiesCalculation?: { trophies: number; netChange: number };
  startTrophiesSource?: "Calculated" | "Season rule";
  offense: {
    attacks: number | null;
    threeStars: number | null;
    trophyGain: number | null;
  };
  defense: {
    defenses: number | null;
    threeStarsAgainst: number | null;
    trophyLoss: number | null;
  };
  trophyChange: number | null;
  // Trophies the game took at this day's closing Reset for unplayed
  // defenses, which the trophy change includes; null when there was none.
  automaticDefenseLoss?: number | null;
  // The weekly or Season reset's raise or cut at this day's closing Reset,
  // which the trophy change leaves out; null when there was none.
  resetAdjustment?: { kind: "weekly" | "Season"; amount: number } | null;
  // Clash Lens rank on the frozen board saved at this day's closing Reset.
  resetRank?: number | null;
  // Python found every battle of the day among the recorded ones: so far for
  // the day in progress, or all 8 of each for a finished day.
  battlesComplete?: boolean;
  offenseEvents: RankedBattleEvent[];
  defenseEvents: RankedBattleEvent[];
  completeness: {
    state: "complete" | "partial" | "uncertain";
    reason: string;
  };
  uncertainty: string[];
}

export interface PlayerLookup {
  tag: string;
  state:
    | "unknown"
    | "checking"
    | "tracking"
    | "not_found"
    | "not_in_legend"
    | "uncertain"
    | "failed";
  // Why a tracked player has no current results yet.
  reason?:
    | "pending"
    | "no_legend_battles"
    | "season_unconfirmed"
    | "unknown_tier"
    | "profile_rejected";
  // The newest profile: a tracked player's only on this page because its
  // Season is 0; for a player outside Legend I, with its league when known.
  profile?: {
    name: string;
    clan: string | null;
    clan_tag?: string | null;
    trophies: number;
    league?: string;
  };
}

/** One week, Monday Reset to Monday Reset, of the weekly league record. */
export interface SeasonWeek {
  start: string;
  league: "legend_i" | "below_legend_i" | "unknown";
  /** How a Legend I week began; null for any other week. */
  entry: "season_start" | "stayed" | "promoted" | "first_seen" | "unknown" | null;
}

export interface PlayerPage {
  kind: "player-page";
  tag: string;
  trackingState: "tracking" | "not_in_legend" | "uncertain";
  profile: PlayerProfile;
  season: {
    id: string;
    anchor: string;
    currentDayNumber: number;
    dayCount: number;
    anchorSource:
      "official_league_history" | "daily_publication" | "weekly_league_record";
    anchorObservedAt: string;
  } | null;
  /** The calendar Season's weeks the weekly league record has decided, oldest first. */
  seasonWeeks?: SeasonWeek[];
  currentDay: RankedDaySummary | null;
  /** When `currentDay` was published. */
  currentDayPublishedAt?: string | null;
  recentDays: RankedDaySummary[];
  seasonDays: RankedDaySummary[];
  /** Python's name for each unit in these days' armies, by saved id. */
  armyUnits?: ArmyUnitNames;
  dataQuality: Array<{
    code:
      | "stale"
      | "partial"
      | "uncertain"
      | "unavailable"
      | "malformed"
      | "unclassified"
      | "rate-limited";
    label: string;
    detail: string;
  }>;
  provenance: DataProvenance;
}

export type RefreshState = "queued" | "running" | "complete" | "unavailable" | "failed";

export interface RefreshWork {
  kind: "refresh-work" | "refresh-status";
  workId: string;
  tag: string;
  state: RefreshState;
  progressPercent: number;
  message: string;
  publishedAt: string | null;
}

export interface RefreshStatus extends RefreshWork {
  kind: "refresh-status";
  player: PlayerPage | null;
}

export type RefreshError = WebsiteErrorResponse;

/** Null when the reply has no proof state, for example an older summary. */
export type EodProofState = "accepted" | "provisional" | null;

export interface HistoricalSeasonDayEntry {
  dayNumber: number | null;
  period: string;
  startTrophies: number | null;
  endTrophies: number | null;
  eodState: EodProofState;
  eodChange: number | null;
  eodChangeState: EodProofState;
  attackGain: number | null;
  defenseLoss: number | null;
  netChange: number | null;
  attacks: number | null;
  defenses: number | null;
  state: string;
  confidence?: RankedDaySummary["confidence"];
  coverage: string;
  hasAdjustment: boolean;
  adjustmentTotal: number | null;
  flags: string[];
  resetRank?: number | null;
}

export interface HistoricalSeasonSummary {
  kind: "player-season-summary";
  tag: string;
  seasonId: string;
  seasonStart: string | null;
  seasonEnd: string | null;
  startTrophies: number | null;
  endTrophies: number | null;
  finalRank: number | null;
  attackCount: number | null;
  attackGain: number | null;
  defenseCount: number | null;
  defenseLoss: number | null;
  netTrophyChange: number | null;
  attackStars: Record<string, number | null>;
  defenseStars: Record<string, number | null>;
  attackStarsUnknown: number | null;
  defenseStarsUnknown: number | null;
  daysObserved: number;
  daysMissing: number[];
  coverageState: "complete" | "partial";
  unresolvedFlags: string[];
  dailyEntries: HistoricalSeasonDayEntry[];
  publishedAt: string | null;
  source: "tracked_summary" | "official_league_history";
  officialHistory: OfficialSeasonHistory | null;
  /** The Season's weeks the weekly league record decided, oldest first. */
  seasonWeeks?: SeasonWeek[];
}

export interface OfficialSeasonHistory {
  observedAt: string;
  eodTrophies: number | null;
  finalPlacement: number | null;
}

export interface SummarizedSeasonRef {
  seasonId: string;
  coverageState: "complete" | "partial";
  daysObserved: number;
  daysMissing: number;
  source: "tracked_summary" | "official_league_history";
  officialHistory: OfficialSeasonHistory | null;
}

// Official Legend history takes precedence over ClashKing for each Season.
// Calendar-month seasons from before 28-day Seasons have no start or end.
export interface PastSeasonFinish {
  seasonId: string;
  seasonStart: string | null;
  seasonEnd: string | null;
  trophies: number | null;
  globalRank: number | null;
}
