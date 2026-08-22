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
  };
};

export interface DataProvenance {
  source: string;
  observedAt: string;
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

export interface TrackedLeaderboard {
  kind: "tracked-leaderboard";
  view: "live" | "daily";
  entries: TrackedPlayerEntry[];
  totalTracked: number;
  totalEntries: number;
  page: number;
  pageSize: number;
  pageCount: number;
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
  trophies: number;
  freshness: Freshness;
  state: "available" | "stale" | "uncertain";
  context: string;
}

export interface SearchResponse {
  kind: "player-search";
  query: string;
  exactTag: string | null;
  results: KnownPlayerResult[];
  knownOnly: boolean;
}

export interface PlayerProfile {
  tag: string;
  name: string;
  clan: string;
  trophies: number;
  freshness: Freshness;
  confidence: ConfidenceState;
  coverage: CoverageState;
  eligibility: "legend-i" | "uncertain";
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
}

export interface RankedDaySummary {
  dayNumber: number | null;
  label: string;
  period: string;
  state: "Live" | "Complete" | "Partial" | "Uncertain";
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
  offenseEvents: RankedBattleEvent[];
  defenseEvents: RankedBattleEvent[];
  completeness: {
    state: "complete" | "partial" | "uncertain";
    reason: string;
  };
  uncertainty: string[];
}

export interface PlayerPage {
  kind: "player-page";
  tag: string;
  profile: PlayerProfile;
  season: {
    id: string;
    anchor: string;
    currentDayNumber: number;
    dayCount: number;
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
