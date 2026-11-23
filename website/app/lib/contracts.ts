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

export interface RecentDay {
  start: string;
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
  calendar: Calendar | null;
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

export interface UpdateStatus {
  checkedAt: string;
  /** Set when no Clash of Clans API answer arrived for the delay limit. */
  lastCollectedAt: string | null;
  oldestWaitingSavedAt: string | null;
}

export interface ArmyAnalytics {
  calendar: Calendar | null;
  seasonBounds: SeasonBounds | null;
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
    variants?: ArmyAnalytics["rows"];
    typeId?: number;
    /** A variant: its id (0 for its type's Other). */
    variantId?: number;
  }>;
}

export interface ArmyVariantExamples {
  rule: string;
  groups: Array<{
    attacks: number;
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
  presentation: DayPresentation | null;
  displayEnd: DayEnd | null;
  start: string;
  end: string | null;
  seasonId: string | null;
  state: "Live" | "Complete" | "Partial" | "Uncertain";
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
  automaticDefenseLoss?: number | null;
  resetAdjustment?: { kind: "weekly" | "Season"; amount: number } | null;
  resetRank?: number | null;
  battlesComplete?: boolean;
  offenseEvents: RankedBattleEvent[];
  defenseEvents: RankedBattleEvent[];
  completeness: {
    state: "complete" | "partial" | "uncertain";
    reason: string;
  };
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
  reason?:
    | "pending"
    | "no_legend_battles"
    | "season_unconfirmed"
    | "unknown_tier"
    | "profile_rejected";
  profile?: {
    name: string;
    clan: string | null;
    clan_tag?: string | null;
    trophies: number;
    league?: string;
  };
}

export interface SeasonWeek {
  start: string;
  league: "legend_i" | "below_legend_i" | "unknown";
  entry: "season_start" | "stayed" | "promoted" | "first_seen" | "unknown" | null;
}

export interface PlayerPage {
  trophyTrends: Record<
    "7" | "14",
    { span: number; counted: number; total: number | null }
  > | null;
  dashboardGoal: Omit<import("./dashboard-cards").GoalData, "name"> | null;
  dashboardSeason: import("./dashboard-cards").SeasonData | null;
  rivalSeason: {
    resetRank: number | null;
    hitRate: number | null;
    offense: number | null;
    defense: number | null;
    netPerDay: number | null;
    finishedDays: number | null;
  } | null;
  calendar: Calendar;
  seasonShape: import("./season-shape").SeasonShape | null;
  historyDays: RankedDaySummary[];
  historyDayNumbers: Record<string, number>;
  battleStatistics: Record<"7" | "14" | "season", BattleStatisticsFact> | null;
  kind: "player-page";
  tag: string;
  trackingState: "tracking" | "not_in_legend" | "uncertain";
  profile: PlayerProfile;
  season: {
    id: string;
    anchor: string;
    end: string;
    initialTrophies: number;
    currentDayNumber: number;
    dayCount: number;
    anchorSource:
      "official_league_history" | "daily_publication" | "weekly_league_record";
    anchorObservedAt: string;
  } | null;
  currentDay: RankedDaySummary | null;
  currentDayPublishedAt?: string | null;
  recentDays: RankedDaySummary[];
  seasonDays: RankedDaySummary[];
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

export type EodProofState = "accepted" | "provisional" | null;

export interface HistoricalSeasonDayEntry {
  presentation: DayPresentation;
  dayNumber: number | null;
  start: string;
  end: string | null;
  seasonId: string | null;
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
  confidence?: "exact" | "inferred" | "partial" | "uncertain" | null;
  coverage: string;
  hasAdjustment: boolean;
  adjustmentTotal: number | null;
  flags: string[];
  resetRank?: number | null;
}

export interface HistoricalSeasonSummary {
  summaryStatistics: {
    attack: { perDay: number | null; perBattle: number | null };
    defense: { perDay: number | null; perBattle: number | null };
  };
  dayCount: number;
  lastDayStart: string | null;
  seasonShape: import("./season-shape").SeasonShape<HistoricalSeasonDayEntry> | null;
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
  seasonWeeks?: SeasonWeek[];
}

export interface OfficialSeasonHistory {
  observedAt: string;
  eodTrophies: number | null;
  finalPlacement: number | null;
}

export interface SummarizedSeasonRef {
  seasonStart: string | null;
  seasonEnd: string | null;
  lastDayStart: string | null;
  dayCount: number;
  seasonId: string;
  coverageState: "complete" | "partial";
  daysObserved: number;
  daysMissing: number;
  source: "tracked_summary" | "official_league_history";
  officialHistory: OfficialSeasonHistory | null;
}

export interface PastSeasonFinish {
  seasonId: string;
  seasonStart: string | null;
  seasonEnd: string | null;
  trophies: number | null;
  globalRank: number | null;
}

export interface Calendar {
  as_of: string;
  day_start: string;
  day_end: string;
  season_id: string;
  season_start: string;
  season_end: string;
  day_number: number;
  day_count: number;
}
export interface SeasonBounds {
  id: string;
  start: string;
  end: string;
  last_day_start: string;
  day_count: number;
}
export type DayStatus =
  "in_progress" | "verified" | "calculated" | "uncertain" | "not_enrolled";
export interface DayPresentation {
  heatmap_kind: "played" | "shield" | "missing";
  status: DayStatus;
  battle_net: number | null;
  battle_net_complete: number | null;
  is_current: boolean;
  show_in_player_log: boolean;
  live_routine: boolean;
  reason_codes: string[];
  caution_codes: string[];
}
export interface DayEnd {
  before_reset: number | null;
  after_reset: number | null;
  player_log: number | null;
  status: DayStatus;
  conflict: null | {
    battle_end: number;
    reset_kind: "weekly" | "season" | null;
    reset_amount: number;
    next_start: number;
  };
}
export interface BattleStatisticsFact {
  start: string;
  end: string;
  daysSaved: number;
  daysExpected: number;
  incomplete: boolean;
  resetDayStart: string | null;
  attack: BattleSide;
  defense: BattleSide;
}
export interface BattleSide {
  count: number;
  stars: number[];
  trophies: number;
  playedDays: number;
  perDay: number | null;
  perBattle: number | null;
  disputed: boolean;
}
