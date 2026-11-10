import type {
  HistoricalSeasonSummary,
  PlayerPage,
  RankedDaySummary,
  RefreshStatus,
  SummarizedSeasonRef,
} from "../../app/lib/contracts";

export const CALENDAR = {
  as_of: "2026-10-08T12:00:00Z",
  day_start: "2026-10-08T05:00:00Z",
  day_end: "2026-10-09T05:00:00Z",
  season_id: "1791176400",
  season_start: "2026-10-05T05:00:00Z",
  season_end: "2026-11-02T05:00:00Z",
  day_number: 4,
  day_count: 28,
};
export const VERIFIED = {
  heatmap_kind: "played" as const,
  status: "verified" as const,
  battle_net: 40,
  battle_net_complete: 40,
  is_current: false,
  show_in_player_log: true,
  show_in_day_summary: true,
  live_routine: false,
  reason_codes: [] as string[],
  caution_codes: [] as string[],
};
export const UNKNOWN_END = {
  before_reset: null,
  after_reset: null,
  player_log: null,
  dashboard: null,
  status: "uncertain" as const,
  conflict: null,
};
export const EMPTY_STATISTICS = {
  start: "2026-10-05T05:00:00Z",
  end: "2026-10-08T05:00:00Z",
  daysSaved: 0,
  daysExpected: 3,
  incomplete: false,
  resetDayStart: null,
  attack: {
    count: 0,
    stars: [0, 0, 0, 0],
    trophies: 0,
    playedDays: 0,
    perDay: null,
    perBattle: null,
    disputed: false,
  },
  defense: {
    count: 0,
    stars: [0, 0, 0, 0],
    trophies: 0,
    playedDays: 0,
    perDay: null,
    perBattle: null,
    disputed: false,
  },
};
export const EMPTY_PLAYER_FACTS = {
  dashboardGoal: null,
  trophyTrends: null,
  dashboardSeason: {
    dayCount: 28,
    endMs: null,
    windows: { "3": null, "7": null, season: null },
    averages: { offense: null, defense: null },
    today: null,
    startMs: null,
    days: [] as import("../../app/lib/dashboard-cards").SeasonDay[],
  },
  rivalSeason: {
    resetRank: null,
    hitRate: null,
    offense: null,
    defense: null,
    netPerDay: null,
    finishedDays: null,
  },
  calendar: CALENDAR,
  seasonShape: null,
  historyDayNumbers: {} as Record<string, number>,
  historyDays: [] as RankedDaySummary[],
  battleStatistics: {
    "7": EMPTY_STATISTICS,
    "14": EMPTY_STATISTICS,
    season: EMPTY_STATISTICS,
  },
};
export const EMPTY_SUMMARY_FACTS = {
  dayCount: 28,
  lastDayStart: "2026-10-04T05:00:00Z",
  seasonShape: null,
  summaryStatistics: {
    attack: { perDay: null, perBattle: null },
    defense: { perDay: null, perBattle: null },
  },
};

export const RAW_STATISTICS = {
  start: CALENDAR.season_start,
  end: CALENDAR.day_start,
  days_saved: 0,
  days_expected: 3,
  incomplete: false,
  reset_day_start: null,
  attack: {
    count: 0,
    stars: [0, 0, 0, 0],
    trophies: 0,
    played_days: 0,
    per_day: null,
    per_battle: null,
    disputed: false,
  },
  defense: {
    count: 0,
    stars: [0, 0, 0, 0],
    trophies: 0,
    played_days: 0,
    per_day: null,
    per_battle: null,
    disputed: false,
  },
};
export const RAW_PLAYER_FACTS = {
  dashboard_windows: { "3": null, "7": null, season: null },
  trophy_trends: null,
  dashboard_season: null,
  rival_season: {
    reset_rank: null,
    hit_rate: null,
    offense: null,
    defense: null,
    net_per_day: null,
    finished_days: null,
  },
  calendar: CALENDAR,
  season_shape: null,
  stats_day_starts: null,
  history_day_numbers: {} as Record<string, number>,
  history_day_starts: [] as string[],
  battle_statistics: {
    "7": RAW_STATISTICS,
    "14": RAW_STATISTICS,
    season: RAW_STATISTICS,
  },
};
export const RAW_DAY_FACTS = {
  official_season_id: null,
  presentation: VERIFIED,
  display_end: UNKNOWN_END,
  display_start: { trophies: 5000, source: "saved", calculation: null },
};
export const RAW_SUMMARY_FACTS = {
  day_count: 28,
  last_day_start: "2026-10-04T05:00:00Z",
  season_shape: null,
  summary_statistics: {
    attack: { per_day: null, per_battle: null },
    defense: { per_day: null, per_battle: null },
  },
};
export const TAG = "#2PP";

export const SEASON = "1785714000";

export const SUMMARY: HistoricalSeasonSummary = {
  kind: "player-season-summary",
  ...EMPTY_SUMMARY_FACTS,
  tag: TAG,
  seasonId: SEASON,
  seasonShape: {
    seasonStart: Date.parse("2026-08-03T05:00:00Z"),
    today: 29,
    kinds: Array(28).fill("legend"),
    runs: [{ first: 1, last: 28, signedUp: 1, promoted: false, dropped: false }],
    partial: false,
    outsideNow: false,
    legendDays: { expected: 28, saved: 28 },
    lastLegendDay: null,
  },
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
    seasonStart: SUMMARY.seasonStart,
    seasonEnd: SUMMARY.seasonEnd,
    dayCount: 28,
    lastDayStart: SUMMARY.lastDayStart,
    coverageState: "partial",
    daysObserved: 28,
    daysMissing: 0,
    source: "tracked_summary",
    officialHistory: null,
  },
];

export function playerPage({
  tag = TAG,
  name = "Nova",
  clan = "Example",
  clanTag,
  trophies = 6000,
  observedAt = "2026-09-07T12:00:00Z",
}: {
  tag?: string;
  name?: string;
  clan?: string;
  clanTag?: string | null;
  trophies?: number;
  observedAt?: string;
} = {}) {
  return {
    kind: "player-page",
    ...EMPTY_PLAYER_FACTS,
    tag,
    trackingState: "tracking",
    profile: {
      tag,
      name,
      clan,
      ...(clanTag === undefined ? {} : { clanTag }),
      trophies,
      freshness: { state: "fresh", observedAt, ageSeconds: 0 },
      confidence: "high",
      coverage: "complete",
      eligibility: "legend-i",
    },
    season: null,
    currentDay: null,
    recentDays: [],
    seasonDays: [],
    dataQuality: [],
  } satisfies PlayerPage;
}

export const PLAYER = playerPage();

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
  start: "2026-09-07T05:00:00Z",
  end: "2026-09-08T05:00:00Z",
  seasonId: null,
  presentation: {
    ...VERIFIED,
    status: "calculated",
    reason_codes: ["player_not_eligible"],
  },
  displayEnd: { ...UNKNOWN_END, status: "calculated" },
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

/** A fake reply's final log selection, supplied explicitly by the example. */
export function logFacts(historyDays: RankedDaySummary[], numbers: number[]) {
  return {
    historyDays,
    historyDayNumbers: Object.fromEntries(
      historyDays.map((day, index) => [day.start, numbers[index]]),
    ),
  };
}

export const SEASON_BOUNDS = {
  "1785714000": {
    id: "1785714000",
    start: "2026-08-02T23:40:00+00:00",
    end: "2026-08-30T23:40:00+00:00",
    last_day_start: "2026-08-29T23:40:00+00:00",
    day_count: 28,
  },
  "1783296000": {
    id: "1783296000",
    start: "2026-07-06T00:00:00+00:00",
    end: "2026-08-03T00:00:00+00:00",
    last_day_start: "2026-08-02T00:00:00+00:00",
    day_count: 28,
  },
  "1788757200": {
    id: "1788757200",
    start: "2026-09-07T05:00:00+00:00",
    end: "2026-10-05T05:00:00+00:00",
    last_day_start: "2026-10-04T05:00:00+00:00",
    day_count: 28,
  },
  "1791176400": {
    id: "1791176400",
    start: "2026-10-05T05:00:00+00:00",
    end: "2026-11-02T05:00:00+00:00",
    last_day_start: "2026-11-01T05:00:00+00:00",
    day_count: 28,
  },
  "1786338000": {
    id: "1786338000",
    start: "2026-08-10T05:00:00+00:00",
    end: "2026-09-07T05:00:00+00:00",
    last_day_start: "2026-09-06T05:00:00+00:00",
    day_count: 28,
  },
  "1781499600": {
    id: "1781499600",
    start: "2026-06-15T05:00:00+00:00",
    end: "2026-07-13T05:00:00+00:00",
    last_day_start: "2026-07-12T05:00:00+00:00",
    day_count: 28,
  },
};
