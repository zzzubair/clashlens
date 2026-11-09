import cases from "../../../testdata/w6-presentation-parity.json";
import { CALENDAR, RAW_DAY_FACTS, RAW_PLAYER_FACTS } from "./player-route-data";
export const HOUR = 3_600_000;
export const DAY = 24 * HOUR;
export const ANCHOR = Date.UTC(2026, 8, 7, 5);
export const SEASON_ID = String(ANCHOR / 1000);
export const NOW = ANCHOR + 8 * DAY + 4.5 * HOUR;
export const MAIN = "#LQ2V8PJ0";
export const TUNDRA = "#8PYQ2LJ0";
export const iso = (ms: number) => new Date(ms).toISOString().replace(".000Z", "+00:00");
export const dayStart = (day: number) => ANCHOR + (day - 1) * DAY;
const WINDOWS = cases.filter((c) => c.rule === "dashboard_windows");
export const suppliedWindows = (index = 11) =>
  structuredClone(
    WINDOWS[
      ({ 4: 2, 6: 4, 9: 3, 10: 0, 11: 1, 12: 5 } as Record<number, number>)[index] ?? 1
    ].expected,
  );
const SEASONS = cases.filter((c) => c.rule === "dashboard_season");
export const suppliedSeason = (index: number) =>
  structuredClone(SEASONS[index].expected) as {
    today: number;
    start: string;
    end: string;
    day_count: number;
    days_left: number;
    days: { day: number; hits: [number, number][] | null; end_trophies: number | null }[];
  };

let battleIds = 0;
function event(at: number, stars: number, trophyChange: number) {
  battleIds += 1;
  return {
    battle_id: `battle-${battleIds}`,
    battle_timestamp: iso(at),
    opponent: { tag: "#2PY", name: "Opponent" },
    destruction_percentage: stars === 3 ? 100 : 80,
    stars,
    trophy_change: trophyChange,
    perspective_disagreement: false,
    army_share_code: null,
  };
}

interface DayPlan {
  day: number;
  /** Attacks tripled (40 each); the rest are 2-star attacks worth 30. */
  triples: number;
  /** Defenses held at 2 stars (−24 each); the rest are tripled (−40 each). */
  held: number;
  start: number;
  rank: number | null;
  attacks?: number;
  defenses?: number;
  /** The battle log has a gap, so Python leaves the day's trophy change unknown. */
  gap?: boolean;
}

/** One finished Legend day as Python's player page sends it. */
function savedDay(plan: DayPlan) {
  const start = dayStart(plan.day);
  const attacks = plan.attacks ?? 8;
  const defenses = plan.defenses ?? 8;
  const hits = Array.from({ length: attacks }, (_, index) =>
    index < plan.triples ? 40 : 30,
  );
  const losses = Array.from({ length: defenses }, (_, index) =>
    index < plan.held ? 24 : 40,
  );
  const gain = hits.reduce((total, value) => total + value, 0);
  const loss = losses.reduce((total, value) => total + value, 0);
  return {
    ...RAW_DAY_FACTS,
    ranked_day_start: iso(start),
    ranked_day_end: iso(start + DAY),
    season_day_number: plan.day,
    state: plan.gap ? "Partial" : "Complete",
    confidence: plan.gap ? "partial" : "exact",
    coverage: plan.gap ? "partial" : "complete",
    completeness: plan.gap
      ? { state: "partial", reason: "battle_log_gap" }
      : { state: "complete", reason: "Complete evidence." },
    public_confidence: plan.gap ? "partial" : "high",
    uncertainty_reasons: plan.gap ? ["battle_log_gap"] : [],
    start_trophies: plan.start,
    display_start: { trophies: plan.start, source: "saved", calculation: null },
    attack_count: attacks,
    attack_three_star_count: Math.min(plan.triples, attacks),
    attack_gain: gain,
    defense_count: defenses,
    defense_three_star_count: defenses - plan.held,
    defense_loss: loss,
    net_trophy_change: plan.gap ? null : gain - loss,
    reset_rank: plan.rank,
    battles_complete: !plan.gap && attacks === 8 && defenses === 8,
    offense_events: hits.map((value, index) =>
      event(start + (index + 1) * HOUR, value === 40 ? 3 : 2, value),
    ),
    defense_events: losses.map((value, index) =>
      event(start + (index + 1.5) * HOUR, value === 40 ? 3 : 2, -value),
    ),
  };
}

/** Day 9 so far. */
function today(
  totals: [
    attacks: number,
    triples: number,
    gain: number,
    defenses: number,
    loss: number,
  ],
  events: { offense: ReturnType<typeof event>[]; defense: ReturnType<typeof event>[] },
) {
  const start = dayStart(9);
  const [attacks, triples, gain, defenses, loss] = totals;
  return {
    ...RAW_DAY_FACTS,
    presentation: {
      ...RAW_DAY_FACTS.presentation,
      status: "in_progress",
      is_current: true,
      live_routine: true,
      battle_net: gain - loss,
      battle_net_complete: null,
    },
    ranked_day_start: iso(start),
    ranked_day_end: iso(start + DAY),
    season_day_number: 9,
    state: "Live",
    confidence: null,
    coverage: "partial",
    completeness: { state: "partial", reason: "The day is not over." },
    public_confidence: "partial",
    uncertainty_reasons: [],
    start_trophies: null,
    attack_count: attacks,
    attack_three_star_count: triples,
    attack_gain: gain,
    defense_count: defenses,
    defense_three_star_count: 0,
    defense_loss: loss,
    net_trophy_change: null,
    battles_complete: false,
    offense_events: events.offense,
    defense_events: events.defense,
  };
}

/** GET /v1/players/{tag}, as Python sends it. */
function playerPage(
  tag: string,
  name: string,
  trophies: number,
  days: ReturnType<typeof savedDay>[],
  current: ReturnType<typeof today>,
  publishedAt = NOW - 5 * 60_000,
) {
  const observedAt = iso(NOW - 10 * 60_000);
  return {
    tag,
    name,
    trophies,
    active: true,
    eligibility: "eligible",
    clan: null,
    season_reset_pending: false,
    current_league_season_id: SEASON_ID,
    freshness: "fresh",
    observed_at: observedAt,
    age_seconds: 600,
    public_confidence: "high",
    screen_ready: {
      ...RAW_PLAYER_FACTS,
      dashboard_season: suppliedSeason(11),
      dashboard_windows: suppliedWindows(),
      rival_season: {
        reset_rank: 318,
        hit_rate: 37 / 48,
        offense: 1810 / 6,
        defense: -256,
        net_per_day: 274 / 6,
        finished_days: 8,
      },
      calendar: {
        ...CALENDAR,
        as_of: iso(NOW),
        day_start: iso(dayStart(9)),
        day_end: iso(dayStart(10)),
        season_id: SEASON_ID,
        season_start: iso(ANCHOR),
        season_end: iso(ANCHOR + 28 * DAY),
        day_number: 9,
      },
      season: {
        id: SEASON_ID,
        start: iso(ANCHOR),
        end: iso(ANCHOR + 28 * DAY),
        current_day_number: 9,
        day_count: 28,
        initial_trophies: 5000,
        anchor_source: "official_league_history",
        anchor_observed_at: iso(ANCHOR),
      },
      days: [...days, current],
      current_day_start: current.ranked_day_start,
      current_day_published_at: iso(publishedAt),
      // The last two days also arrive as recent days; each counts once.
      recent_day_starts: days.slice(-2).map((day) => day.ranked_day_start),
      season_day_starts: days.map((day) => day.ranked_day_start),
      data_quality: [],
      provenance: {
        source: "api_player_daily_logs",
        observed_at: observedAt,
        freshness: "fresh",
        confidence: "high",
        coverage: "partial",
        version: "test-v1",
      },
    },
  };
}

/**
 * The selected account's Season: Day 3 a shield day, Day 7 never saved.
 * Net by day: +44, +70, 0, +8, +64, +2, (missing), +86.
 */
function mainDays(day6: Partial<DayPlan> = {}) {
  return [
    savedDay({ day: 1, triples: 6, held: 4, start: 5000, rank: 900 }),
    savedDay({ day: 2, triples: 7, held: 5, start: 5044, rank: 700 }),
    savedDay({
      day: 3,
      triples: 0,
      held: 0,
      start: 5114,
      rank: 760,
      attacks: 0,
      defenses: 0,
    }),
    savedDay({ day: 4, triples: 4, held: 3, start: 5114, rank: 800 }),
    savedDay({ day: 5, triples: 8, held: 4, start: 5122, rank: 500 }),
    savedDay({ day: 6, triples: 5, held: 2, start: 5186, rank: 520, ...day6 }),
    savedDay({ day: 8, triples: 7, held: 6, start: 5238, rank: 318 }),
  ];
}

export function mainPage(day6: Partial<DayPlan> = {}, seasonCase = 11) {
  const start = dayStart(9);
  const page = playerPage(
    MAIN,
    "Lens Main",
    5370,
    mainDays(day6),
    today([2, 1, 71, 1, 16], {
      // 07:10 and 07:25 share one mark on the clock.
      offense: [
        event(start + 2 * HOUR + 10 * 60_000, 3, 40),
        event(start + 2 * HOUR + 25 * 60_000, 2, 31),
      ],
      defense: [event(start + 4 * HOUR, 1, -16)],
    }),
  );
  page.screen_ready.dashboard_season = suppliedSeason(seasonCase);
  page.screen_ready.dashboard_windows = suppliedWindows(seasonCase);
  return page;
}

/** A rival 1 place above, who was #310 at the last Reset; today's battles saved 20 min ago. */
export function tundraPage() {
  return playerPage(
    TUNDRA,
    "Tundra",
    5375,
    [savedDay({ day: 8, triples: 8, held: 8, start: 5180, rank: 310 })],
    today([4, 2, 150, 2, 60], { offense: [], defense: [] }),
    NOW - 20 * 60_000,
  );
}
