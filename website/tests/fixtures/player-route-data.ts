import type {
  HistoricalSeasonSummary,
  PlayerPage,
  RankedDaySummary,
  RefreshStatus,
  SummarizedSeasonRef,
} from "../../app/lib/contracts";

export const TAG = "#2PP";

export const SEASON = "1785714000";

export const SUMMARY: HistoricalSeasonSummary = {
  kind: "player-season-summary",
  tag: TAG,
  seasonId: SEASON,
  seasonStart: "2026-05-01T05:00:00+00:00",
  seasonEnd: "2026-05-29T05:00:00+00:00",
  startTrophies: 6000,
  endTrophies: 6280,
  finalRank: null,
  attackCount: 56,
  attackGain: 840,
  defenseCount: 28,
  defenseLoss: 560,
  netTrophyChange: 280,
  attackStars: { "0": 0, "1": 0, "2": 28, "3": 28 },
  defenseStars: { "0": 0, "1": 28, "2": 0, "3": 0 },
  attackStarsUnknown: 0,
  defenseStarsUnknown: 0,
  daysObserved: 28,
  daysMissing: [],
  coverageState: "partial",
  unresolvedFlags: [],
  dailyEntries: [],
  publishedAt: "2026-05-29T06:00:00+00:00",
  source: "tracked_summary",
  officialHistory: null,
};

export const SEASONS: SummarizedSeasonRef[] = [
  {
    seasonId: SEASON,
    coverageState: "partial",
    daysObserved: 28,
    daysMissing: 0,
    source: "tracked_summary",
    officialHistory: null,
  },
];

export const PLAYER = {
  kind: "player-page",
  tag: TAG,
  trackingState: "tracking",
  profile: {
    tag: TAG,
    name: "Nova",
    clan: "Example",
    trophies: 6000,
    freshness: { state: "fresh", observedAt: "2026-09-07T12:00:00Z", ageSeconds: 0 },
    confidence: "high",
    coverage: "complete",
    eligibility: "legend-i",
  },
  season: null,
  currentDay: null,
  recentDays: [],
  seasonDays: [],
  dataQuality: [],
  provenance: {
    source: "api_player_daily_logs",
    observedAt: "2026-09-07T12:00:00Z",
    freshness: "fresh",
    confidence: "high",
    coverage: "complete",
    version: "v1",
  },
} satisfies PlayerPage;

export const NEWER_PROFILE = {
  ...PLAYER.profile,
  freshness: {
    ...PLAYER.profile.freshness,
    observedAt: "2026-09-07T12:00:01Z",
  },
};

export const SAVED_DAY: RankedDaySummary = {
  dayNumber: null,
  label: "Ranked day",
  period: "2026-09-07T05:00:00Z – 2026-09-08T05:00:00Z",
  state: "Complete",
  startTrophies: 6000,
  offense: { attacks: 1, threeStars: 1, trophyGain: 40 },
  defense: { defenses: 0, threeStarsAgainst: 0, trophyLoss: 0 },
  trophyChange: 40,
  offenseEvents: [
    {
      battleId: "saved-attack",
      battleTimestamp: "2026-09-07T13:00:00Z",
      opponent: { tag: "#2PY", name: "Saved opponent" },
      destructionPercentage: 100,
      stars: 3,
      trophyChange: 40,
      perspectiveDisagreement: false,
      armyShareCode: null,
    },
  ],
  defenseEvents: [],
  completeness: { state: "complete", reason: "Complete" },
  uncertainty: ["player_not_eligible"],
};

export const REFRESH_STATUS: RefreshStatus = {
  kind: "refresh-status",
  workId: "work_1",
  tag: TAG,
  state: "complete",
  progressPercent: 100,
  message: "Complete",
  publishedAt: "2026-09-07T12:00:00Z",
  player: PLAYER,
};
