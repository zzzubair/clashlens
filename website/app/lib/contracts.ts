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
  officialRank: number | null;
}

export interface TrackedLeaderboard {
  kind: "tracked-leaderboard";
  view: "live" | "daily";
  entries: TrackedPlayerEntry[];
  totalTracked: number;
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

export interface RankedDaySummary {
  dayNumber: number;
  label: string;
  period: string;
  state: "Live" | "Complete" | "Partial" | "Uncertain";
  offense: {
    attacks: number;
    threeStars: number;
    trophyGain: number;
  };
  defense: {
    defenses: number;
    threeStarsAgainst: number;
    trophyLoss: number;
  };
  trophyChange: number;
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
  };
  currentDay: RankedDaySummary;
  recentDays: RankedDaySummary[];
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
