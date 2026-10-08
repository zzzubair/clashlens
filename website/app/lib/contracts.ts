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
    unknownExcludedAttacks?: number;
  }>;
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
  // The newest profile, shown only on this page because its Season is 0.
  profile?: { name: string; clan: string | null; trophies: number };
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
    anchorSource: "official_league_history" | "daily_publication";
    anchorObservedAt: string;
  } | null;
  currentDay: RankedDaySummary | null;
  recentDays: RankedDaySummary[];
  seasonDays: RankedDaySummary[];
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
export type RefreshStatusResponse = RefreshStatus | RefreshError;

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
