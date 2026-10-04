import type {
  HistoricalSeasonDayEntry,
  HistoricalSeasonSummary,
  PastSeasonFinish,
  PlayerPage,
  RankedBattleEvent,
  RankedDaySummary,
  SummarizedSeasonRef,
} from "../../app/lib/contracts";

// The hardest real player data the player page must still lay out: names up to
// Clash's 15 characters in right-to-left, CJK, Thai, Vietnamese, styled letters
// and emoji, one-letter and missing names, counts at 0, 1 and above 8, unknown
// values and very old timestamps.
export const WORST_TAG = "#2PP0JLQ8VV";
export const WORST_NAMES = ["xXDragonSlayerX", "عبدالرحمن_الملك", "𝕂𝕀𝕟𝕘 ᴷᴵᴺᴳ 👑"];

const OPPONENTS: Array<string | null> = [
  "عبدالله الشمري",
  "王者荣耀之最强部落",
  "한국최강전사클래시",
  "นักรบผู้ยิ่งใหญ่",
  "Nguyễn Thị Ngọc",
  "𝕂𝕀𝕟𝕘 ᴷᴵᴺᴳ",
  "xXDragonSlayerX",
  "J",
  null,
  "👑🔥⚔️🐉💀",
  "ⓜⓐⓡⓘⓞ★彡",
  "WWE_MegaWarlord",
];
const OPPONENT_TAGS = ["#2PP0JLQ8VV", "#9GJ2YRQ0C", "#LQQPGRV8", "#P0Y", "#88CUYJ2LQ"];
const ARMY =
  "u1000x14-60x4-48x2-29x8-110x1s1000x2-35x1-70x2-53x1h88-4-1-3-0-2-17-9-6-3-0-0";

const DAY_MS = 24 * 60 * 60 * 1000;

function dayStart(now: number, offset: number): Date {
  const start = new Date(now - 5 * 60 * 60 * 1000);
  start.setUTCHours(5, 0, 0, 0);
  return new Date(start.getTime() - offset * DAY_MS);
}

function period(start: Date): string {
  return `${start.toISOString()} – ${new Date(start.getTime() + DAY_MS).toISOString()}`;
}

function battles(start: Date, count: number, kind: "a" | "d"): RankedBattleEvent[] {
  return Array.from({ length: count }, (_, index) => ({
    battleId: `worst-${kind}-${start.getTime()}-${index}`,
    battleTimestamp: new Date(
      start.getTime() + (index + 1) * 2.5 * 60 * 60 * 1000,
    ).toISOString(),
    opponent: {
      tag: OPPONENT_TAGS[index % OPPONENT_TAGS.length],
      name: OPPONENTS[(index + (kind === "d" ? 5 : 0)) % OPPONENTS.length],
    },
    destructionPercentage: [100, 0, 99, 47, 100, 100, 85, 100, 63][index % 9],
    stars: [3, 0, 2, 1, 3, 3, 2, 3, 2][index % 9],
    trophyChange: (kind === "a" ? 1 : -1) * [40, 0, 32, 5, 40, 40, 16, 40, 24][index % 9],
    perspectiveDisagreement: index === 2,
    armyShareCode: index % 3 === 0 ? ARMY : null,
  }));
}

function day(
  now: number,
  offset: number,
  dayNumber: number | null,
  attacks: number,
  defenses: number,
  overrides: Partial<RankedDaySummary> = {},
): RankedDaySummary {
  const start = dayStart(now, offset);
  const offenseEvents = battles(start, attacks, "a");
  const defenseEvents = battles(start, defenses, "d");
  const gain = offenseEvents.reduce((sum, event) => sum + event.trophyChange, 0);
  const loss = -defenseEvents.reduce((sum, event) => sum + event.trophyChange, 0);
  return {
    dayNumber,
    label: `Day ${dayNumber ?? "unknown"}`,
    period: period(start),
    state: "Complete",
    startTrophies: 6460,
    offense: { attacks, threeStars: 3, trophyGain: gain },
    defense: { defenses, threeStarsAgainst: 1, trophyLoss: loss },
    trophyChange: gain - loss,
    offenseEvents,
    defenseEvents,
    completeness: { state: "complete", reason: "" },
    uncertainty: [],
    ...overrides,
  };
}

const LONG_DETAIL =
  "missing_start_battle_log_baseline; battle_log_overlap_gap; trophy_equation_mismatch; attack_count_exceeds_eight";

export function worstCasePlayer(tag = WORST_TAG, now = Date.now()): PlayerPage {
  const currentDay = day(now, 0, 28, 9, 1, {
    state: "Live",
    trophyChange: null,
    completeness: { state: "partial", reason: LONG_DETAIL },
    uncertainty: LONG_DETAIL.split("; "),
    startTrophiesCalculation: { trophies: 6512, netChange: 52 },
  });
  const seasonDays = [
    day(now, 1, 27, 8, 8),
    day(now, 2, 26, 0, 0, {
      startTrophies: null,
      trophyChange: null,
      offense: { attacks: null, threeStars: null, trophyGain: null },
      defense: { defenses: null, threeStarsAgainst: null, trophyLoss: null },
      state: "Uncertain",
      completeness: { state: "uncertain", reason: "malformed_evidence" },
      uncertainty: ["malformed_evidence", "season_anchor_conflict", "truncated_reasons"],
    }),
    day(now, 3, 25, 1, 1, { startTrophies: 4812, trophyChange: -1288 }),
    day(now, 4, null, 12, 8),
  ];
  return {
    kind: "player-page",
    tag,
    trackingState: "tracking",
    profile: {
      tag,
      name: WORST_NAMES[0],
      clan: "عشاق الأساطير ⚔️",
      trophies: 6512,
      freshness: {
        state: "stale",
        observedAt: new Date(now - 1284 * DAY_MS).toISOString(),
        ageSeconds: 30,
      },
      battleHistoryUpdatedAt: null,
      confidence: "partial",
      coverage: "partial",
      eligibility: "legend-i",
    },
    season: null,
    currentDay,
    recentDays: [day(now, 40, null, 1, 0)],
    seasonDays,
    dataQuality: [
      { code: "partial", label: "Incomplete ranked-day data", detail: LONG_DETAIL },
      {
        code: "stale",
        label: "Saved profile is more than three years old",
        detail: "Clash of Clans has not answered for this player since it was saved.",
      },
    ],
    provenance: {
      source: "api_player_daily_logs",
      observedAt: null,
      freshness: "stale",
      confidence: "uncertain",
      coverage: "partial",
      version: "v1",
    },
  };
}

export const WORST_SEASONS: SummarizedSeasonRef[] = Array.from(
  { length: 14 },
  (_, index) => ({
    seasonId: String(1785714000 - index * 28 * 86400),
    coverageState: index % 2 ? "partial" : "complete",
    daysObserved: index % 2 ? 1 : 28,
    daysMissing: index % 2 ? 27 : 0,
    source: index === 13 ? "official_league_history" : "tracked_summary",
    officialHistory: null,
  }),
);

function seasonDay(dayNumber: number): HistoricalSeasonDayEntry {
  const unknown = dayNumber % 5 === 0;
  return {
    dayNumber: dayNumber === 28 ? null : dayNumber,
    period: new Date(Date.UTC(2026, 7, dayNumber + 1, 5)).toISOString(),
    startTrophies: unknown ? null : 4812 + dayNumber * 60,
    endTrophies: unknown ? null : 4852 + dayNumber * 60,
    eodState: dayNumber % 2 ? "accepted" : "provisional",
    eodChange: unknown ? null : -1288,
    eodChangeState: dayNumber % 3 ? "provisional" : null,
    attackGain: unknown ? null : 320,
    defenseLoss: unknown ? null : 280,
    netChange: unknown ? null : 1040,
    attacks: dayNumber === 1 ? 1 : unknown ? null : 8,
    defenses: dayNumber === 1 ? 0 : 12,
    state: unknown ? "Uncertain" : "Complete",
    coverage: unknown ? "partial" : "complete",
    hasAdjustment: dayNumber % 4 === 0,
    adjustmentTotal: dayNumber % 8 === 0 ? null : -1288,
    flags: unknown ? ["trophy_equation_mismatch", "battle_log_overlap_gap"] : [],
  };
}

export const WORST_SEASON_SUMMARY: HistoricalSeasonSummary = {
  kind: "player-season-summary",
  tag: WORST_TAG,
  seasonId: WORST_SEASONS[0].seasonId,
  seasonStart: "2026-08-02T05:00:00+00:00",
  seasonEnd: "2026-08-30T05:00:00+00:00",
  startTrophies: null,
  endTrophies: 6498,
  finalRank: 13204,
  attackCount: 1,
  attackGain: 11040,
  defenseCount: 0,
  defenseLoss: null,
  netTrophyChange: -12880,
  attackStars: { "0": 1, "1": null, "2": 0, "3": 1284 },
  defenseStars: { "0": 0, "1": 1, "2": null, "3": 1284 },
  attackStarsUnknown: 1,
  defenseStarsUnknown: null,
  daysObserved: 27,
  daysMissing: [14],
  coverageState: "partial",
  unresolvedFlags: ["trophy_equation_mismatch"],
  dailyEntries: Array.from({ length: 28 }, (_, index) => seasonDay(index + 1)),
  publishedAt: null,
  source: "tracked_summary",
  officialHistory: {
    observedAt: "2026-08-30T05:00:00Z",
    eodTrophies: 6498,
    finalPlacement: 13204,
  },
};

export const WORST_PAST_SEASONS: PastSeasonFinish[] = Array.from(
  { length: 20 },
  (_, index) => ({
    seasonId: `20${String(24 - Math.floor(index / 12)).padStart(2, "0")}-${String(12 - (index % 12)).padStart(2, "0")}`,
    seasonStart: null,
    seasonEnd:
      index < 3 ? new Date(Date.UTC(2026, 8 - index, 29, 5)).toISOString() : null,
    trophies: 4800 + index * 85,
    globalRank: index % 4 === 3 ? null : [1, 13204, 248913][index % 3],
  }),
);
