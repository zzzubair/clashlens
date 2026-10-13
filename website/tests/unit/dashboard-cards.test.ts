import { createElement } from "react";
import { renderToStaticMarkup, renderToString } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  StaticRouterProvider,
} from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getWebsiteConfig: vi.fn(),
  readLoginIdentity: vi.fn(),
}));

vi.mock("../../app/server/config.server", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/server/config.server")>();
  return { ...actual, getWebsiteConfig: mocks.getWebsiteConfig };
});

vi.mock("../../app/server/actions.server", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/server/actions.server")>();
  return { ...actual, readLoginIdentity: mocks.readLoginIdentity };
});

import { ClockCard } from "../../app/components/BoardCards";
import { CARDS, serializeSettings, type CardId } from "../../app/lib/dashboard";
import {
  around,
  goalPlan,
  heatCells,
  isShieldDay,
  offenseConsistency,
  recentForm,
  type CardDataSet,
  type CardReads,
  type GoalData,
  type SeasonData,
} from "../../app/lib/dashboard-cards";
import DashboardRoute, {
  loader,
  type DashboardLoaderData,
} from "../../app/routes/dashboard";
import { loadWebsiteConfig } from "../../app/server/config.server";

const ORIGIN = "https://clashlens.example";
const IDENTITY = { provider: "google", providerSubject: "11223344556677889900" } as const;
const TEST_SECRET = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8";
const HOUR = 3_600_000;
const DAY = 24 * HOUR;
/** The Season started at this Reset; the page is read on Day 9 at 09:30 UTC. */
const ANCHOR = Date.UTC(2026, 8, 7, 5);
const SEASON_ID = String(ANCHOR / 1000);
const NOW = ANCHOR + 8 * DAY + 4.5 * HOUR;
const MAIN = "#LQ2V8PJ0";
const ALT = "#2PP8QLY0";
const TUNDRA = "#8PYQ2LJ0";
const AVA = "#9YJ2C0QL";
const BO = "#YQ2LPJ80";
const CY = "#QL8YPJ20";
const CLAN_MATES = "0f6c1d8e-7b2a-4c3d-9e5f-1a2b3c4d5e6f";
const ALPHA = "6a7b8c9d-0e1f-4a2b-8c3d-4e5f6a7b8c9d";
const BOARD_SIZE = 9000;

/** A timestamp as Python writes it. */
const iso = (ms: number) => new Date(ms).toISOString().replace(".000Z", "+00:00");
const dayStart = (day: number) => ANCHOR + (day - 1) * DAY;
/** A valid player tag for a board place nobody else holds. */
const rankTag = (rank: number) =>
  `#JC${[...String(rank)].map((digit) => "0289PYLQGR"[Number(digit)]).join("")}`;

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
      season: {
        id: SEASON_ID,
        start: iso(ANCHOR),
        end: iso(ANCHOR + 28 * DAY),
        current_day_number: 9,
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

function mainPage(day6: Partial<DayPlan> = {}) {
  const start = dayStart(9);
  return playerPage(
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
}

/** A rival 1 place above, who was #310 at the last Reset; today's battles saved 20 min ago. */
function tundraPage() {
  return playerPage(
    TUNDRA,
    "Tundra",
    5375,
    [savedDay({ day: 8, triples: 8, held: 8, start: 5180, rank: 310 })],
    today([4, 2, 150, 2, 60], { offense: [], defense: [] }),
    NOW - 20 * 60_000,
  );
}

const LIVE_LINES: Record<number, number> = {
  10: 6100,
  50: 5800,
  100: 5650,
  200: 5420,
  500: 5250,
  1000: 5100,
  5000: 5010,
};
const RESET_LINES: Record<number, Record<number, number>> = {
  8: { 10: 6050, 50: 5780, 100: 5630, 200: 5400, 500: 5230, 1000: 5090, 5000: 5005 },
  1: { 200: 5120 },
};

/** The selected account's rank and trophies on each saved Reset board, as its days say. */
const MAIN_BOARD: Record<number, [rank: number, trophies: number]> = {
  1: [900, 5044],
  2: [700, 5114],
  3: [760, 5114],
  4: [800, 5122],
  5: [500, 5186],
  6: [520, 5188],
  8: [318, 5324],
};

function boardEntry(rank: number, day: number | null) {
  const mine = day === null ? undefined : python.mainBoard[day];
  const tag =
    (day === null ? python.liveMoves[0]?.[rank] : undefined) ??
    ((day === null && rank === python.liveRank) || mine?.[0] === rank
      ? MAIN
      : rank === 315
        ? TUNDRA
        : rankTag(rank));
  const lines = day === null ? LIVE_LINES : (RESET_LINES[day] ?? {});
  return {
    position: rank,
    tag,
    name: tag === MAIN ? "Lens Main" : tag === TUNDRA ? "Tundra" : `Player ${rank}`,
    clan: null,
    trophies:
      (mine?.[0] === rank ? mine[1] : undefined) ??
      lines[rank] ??
      (day === null ? 5450 : 5414) - 5 * (rank - 300),
    observed_at: iso(NOW - 3 * 60_000),
    age_seconds: 180,
    freshness: "fresh",
    confidence: day === null ? "eligible" : "confirmed",
    public_confidence: "high",
    official_rank: null,
    tied: false,
  };
}

/** Where `tag` stands on the live board. */
function livePlace(tag: string) {
  const moved = Object.entries(python.liveMoves[0] ?? {}).find(([, at]) => at === tag);
  if (moved) return Number(moved[0]);
  if (tag === MAIN) return python.liveRank;
  return Number([...tag.slice(3)].map((letter) => "0289PYLQGR".indexOf(letter)).join(""));
}

/** GET /v1/leaderboards/{live,frozen}, as Python pages it. */
function leaderboard(query: URLSearchParams) {
  const limit = Number(query.get("limit"));
  const dayNumber = query.get("season_day_number");
  const day = dayNumber === null ? null : Number(dayNumber);
  // A focused read is the page holding the player, with 5 places either side.
  const focusTag = query.get("focus_tag");
  const focus = focusTag === null ? null : livePlace(focusTag);
  const offset =
    focus === null
      ? Number(query.get("offset"))
      : Math.floor((focus - 1) / limit) * limit;
  const first = Math.max(
    1,
    focus === null ? offset + 1 : Math.min(offset + 1, focus - 5),
  );
  const last = Math.min(
    BOARD_SIZE,
    focus === null ? offset + limit : Math.max(offset + limit, focus + 5),
  );
  const observed = iso(NOW - 3 * 60_000);
  const pages = Math.ceil(BOARD_SIZE / limit);
  return {
    kind: day === null ? "live" : "frozen",
    generated_at: iso(NOW),
    ...(day === null
      ? {
          source_observations: {
            oldest_observed_at: observed,
            newest_observed_at: observed,
            stale_count: 0,
          },
        }
      : {
          official_season_id: SEASON_ID,
          season_day_number: day,
          reset_at: iso(dayStart(day) + DAY),
          season_start_at: iso(ANCHOR),
          season_end_at: iso(ANCHOR + 28 * DAY),
          previous_snapshot: null,
          next_snapshot: null,
        }),
    tracked_population: BOARD_SIZE,
    total_entries: BOARD_SIZE,
    page: offset / limit + 1,
    page_size: limit,
    page_count: pages,
    has_previous: offset > 0,
    has_next: offset / limit + 1 < pages,
    coverage: {
      state: "partial",
      tracked_players: BOARD_SIZE,
      measured_percent: 50,
      note: "Tracked players.",
    },
    provenance: {
      source: "test",
      observed_at: observed,
      freshness: "fresh",
      confidence: "partial",
      coverage: "partial",
      version: "test-v1",
    },
    quality_states: [],
    entries: Array.from({ length: last - first + 1 }, (_, index) =>
      boardEntry(first + index, day),
    ),
  };
}

/** The 7 finished days the group comparison covers. */
const COMPARED_DAYS = Array.from({ length: 7 }, (_, index) => iso(dayStart(2 + index)));

/** One player in GET /v1/account/groups/{id}/comparison, as Python sends it. */
function compared(
  tag: string,
  name: string,
  netPerDay: number,
  changes: Record<string, unknown> = {},
) {
  return {
    tag,
    name,
    you: false,
    in_group: true,
    status: "tracking",
    trophies: 5300,
    season_reset_pending: false,
    observed_at: null,
    age_seconds: null,
    freshness: null,
    today: null,
    day_results: COMPARED_DAYS.map((start) => ({
      start,
      state: "complete",
      net: netPerDay,
    })),
    counted_days: 7,
    counted_attacks: 56,
    net: netPerDay * 7,
    net_per_day: netPerDay,
    vs_group: null,
    attack: { count: 56, stars: 150, destruction: 5000, three_stars: 28, trophies: 1960 },
    defense: {
      count: 56,
      stars: 140,
      destruction: 5000,
      trophies: 1680,
      star_counts: { "0": 0, "1": 0, "2": 0, "3": 0 },
    },
    ...changes,
  };
}

function comparison() {
  return {
    kind: "group-comparison",
    group_id: CLAN_MATES,
    name: "Clan mates",
    days: 7,
    day_starts: COMPARED_DAYS,
    today_start: iso(dayStart(9)),
    generated_at: iso(NOW),
    season: SEASON_ID,
    players: [
      compared(MAIN, "Lens Main", 38, { you: true, in_group: false }),
      compared(AVA, "Ava", 52, { trophies: 5510 }),
      // Battles of a partial day count in the totals but not in its counted days.
      compared(BO, "Bo", 12, {
        day_results: COMPARED_DAYS.map((start, index) =>
          index === 6
            ? { start, state: "partial", net: null }
            : { start, state: "complete", net: 12 },
        ),
        counted_days: 6,
        net: 72,
      }),
      // Another linked account outside the group, and a member who left Legend I.
      compared(ALT, "Lens Alt", 90, { you: true, in_group: false }),
      compared(CY, "Cy", 99, { status: "not_in_legend" }),
    ],
  };
}

function linked(tag: string, changes: Record<string, unknown> = {}) {
  return {
    tag,
    name: "Lens Main",
    clan: null,
    state: "tracking",
    reason: null,
    trophies: 5370,
    season_reset_pending: false,
    rank: 316,
    league: null,
    today: { net: 55, attacks: 2, defenses: 1 },
    ...changes,
  };
}

const GROUPS = [
  {
    group_id: ALPHA,
    name: "Alpha",
    tags: [TUNDRA],
    players: [],
    created_at: "2026-10-01T10:00:00+00:00",
  },
  {
    group_id: CLAN_MATES,
    name: "Clan mates",
    tags: [AVA, BO, CY],
    players: [],
    created_at: "2026-09-01T10:00:00+00:00",
  },
];

const defense = (stars: number, trophyChange: number, yours = false) => ({
  stars,
  trophy_change: trophyChange,
  yours,
});

const hit = (stars: number, destruction: number, trophies: number, minutes: number) => ({
  stars,
  destruction_percentage: destruction,
  trophy_change: trophies,
  battle_timestamp: iso(dayStart(9) + 2 * HOUR + minutes * 60_000),
});

/** What GET /v1/players/{tag}/today returns on Day 9 for today's two attacks. */
const TODAY = {
  tag: MAIN,
  rank_range: { best: 95, worst: 760 },
  open_defenses: 7,
  automatic_defense_each: 21,
  opponents: [
    {
      tag: BO,
      name: "Bo",
      reset_trophies: 5688,
      hit: hit(2, 88, 31, 25),
      // Held 1 of 4, 151 trophies given up; 41% at similar trophies.
      legends_held: { held: 41, defenses: 100 },
      defenses: [
        defense(3, -40),
        defense(2, -31, true),
        defense(3, -40),
        defense(3, -40),
      ],
      observed_at: iso(NOW - 12 * 60_000),
    },
    {
      tag: AVA,
      name: "Ava",
      reset_trophies: 5712,
      hit: hit(3, 100, 40, 10),
      // Held 5 of 6, 90 trophies given up; 41% at similar trophies.
      legends_held: { held: 41, defenses: 100 },
      defenses: [
        defense(1, -10),
        defense(1, -10),
        defense(3, -40, true),
        defense(1, -10),
        defense(2, -20),
        defense(0, 0),
      ],
      observed_at: iso(NOW - 4 * 60_000),
    },
  ],
};

const outcome = (battles: number, three_stars: number, trophies: number) => ({
  battles,
  three_stars,
  trophies,
});

/** What GET /v1/players/{tag}/armies returns for Days 2–8. */
function armies(lens: string, days: number) {
  const covered_days = Array.from({ length: days }, (_, index) =>
    iso(dayStart(9 - days + index)),
  );
  return lens === "defense"
    ? {
        tag: MAIN,
        lens,
        days,
        covered_days,
        rows: [
          {
            type_id: 3,
            name: "Thrower",
            ...outcome(5, 2, 170),
            legends: outcome(1000, 710, 37000),
          },
          {
            type_id: 4,
            name: "Hydra",
            ...outcome(3, 3, 120),
            legends: outcome(800, 400, 26400),
          },
        ],
        total: outcome(8, 5, 290),
        legends_total: outcome(20000, 9600, 640000),
      }
    : {
        tag: MAIN,
        lens,
        days,
        covered_days,
        rows: [
          {
            type_id: 3,
            name: "Thrower",
            ...outcome(16, 12, 600),
            legends: { battles: 1000, three_stars: 500 },
          },
        ],
        total: outcome(16, 12, 600),
      };
}

/** The fake Python service: what it holds, and every request it was sent. */
const python = {
  preferences: {} as Record<string, unknown>,
  pages: {} as Record<string, unknown>,
  groups: GROUPS as unknown[],
  liveRank: 316,
  mainBoard: MAIN_BOARD,
  /** Live places held by someone other than usual; each read focused on you moves on to the next. */
  liveMoves: [] as Record<number, string>[],
  /** Paths ending in one of these answer 503. */
  down: [] as string[],
  requests: [] as { path: string; query: Record<string, string> }[],
};

function answer(url: URL): [status: number, body: unknown] {
  const path = decodeURIComponent(url.pathname);
  python.requests.push({ path, query: Object.fromEntries(url.searchParams) });
  if (python.down.some((end) => path.endsWith(end)))
    return [503, { error: "unavailable" }];
  if (path === "/v1/account")
    return [
      200,
      {
        username: "nova88",
        display_name: "Nova",
        preferences: python.preferences,
        providers: ["google"],
      },
    ];
  if (path === "/v1/users/nova88")
    return [
      200,
      {
        username: "nova88",
        display_name: "Nova",
        // The linked list holds an older reading than the player page's 5,370.
        verified_players: [
          linked(MAIN, { trophies: 5360 }),
          linked(ALT, { name: "Lens Alt", rank: 2000 }),
        ],
      },
    ];
  if (path === "/v1/account/groups")
    return [200, { season: SEASON_ID, groups: python.groups }];
  if (path === `/v1/account/groups/${CLAN_MATES}/comparison`) return [200, comparison()];
  if (path === `/v1/players/${MAIN}/seasons`)
    return [
      200,
      {
        seasons: [
          {
            official_season_id: String(ANCHOR / 1000 - 28 * 86_400),
            coverage_state: "partial",
            days_observed: 0,
            days_missing: 28,
            source: "official_league_history",
            official_history: {
              source: "official_league_history",
              observed_at: iso(ANCHOR),
              eod_trophies: 5288,
              final_placement: 412,
            },
          },
        ],
      },
    ];
  if (path === `/v1/players/${MAIN}/today`) return [200, TODAY];
  if (path === `/v1/players/${MAIN}/armies`)
    return [
      200,
      armies(url.searchParams.get("lens")!, Number(url.searchParams.get("days"))),
    ];
  const page = python.pages[path.replace("/v1/players/", "")];
  if (path.startsWith("/v1/players/") && page) return [200, page];
  if (path === "/v1/leaderboards/live" || path === "/v1/leaderboards/frozen") {
    const board = leaderboard(url.searchParams);
    if (url.searchParams.get("focus_tag") === MAIN && python.liveMoves.length > 1)
      python.liveMoves.shift();
    return [200, board];
  }
  return [404, { error: "not_found" }];
}

const ALL_CARDS = Object.keys(CARDS).filter(
  (card) => CARDS[card as CardId].built,
) as CardId[];

const loaderArgs = (url: string) =>
  ({ request: new Request(url), params: {}, context: {} }) as unknown as Parameters<
    typeof loader
  >[0];

/** Each card's data once its read finishes: the page answers before its cards. */
async function settled(reads: CardReads): Promise<CardDataSet> {
  return Object.fromEntries(
    await Promise.all(
      Object.entries(reads).map(async ([card, read]) => [card, await read]),
    ),
  ) as CardDataSet;
}

async function load() {
  const result = (await loader(loaderArgs(`${ORIGIN}/dashboard`))) as unknown as {
    data: DashboardLoaderData;
  };
  if (result.data.kind !== "signed-in") throw new Error("expected signed in");
  return { ...result.data, cards: await settled(result.data.cards) };
}

async function render(loaderData: DashboardLoaderData): Promise<string> {
  const handler = createStaticHandler([
    { path: "/dashboard", Component: DashboardRoute, loader: () => loaderData },
  ]);
  const context = await handler.query(new Request(`${ORIGIN}/dashboard`));
  if (context instanceof Response) throw new Error("unexpected response");
  return renderToString(
    createElement(StaticRouterProvider, {
      router: createStaticRouter(handler.dataRoutes, context),
      context,
      hydrate: false,
    }),
  ).replaceAll("<!-- -->", "");
}

const text = (html: string) =>
  html
    .replace(/<[^>]+>/g, " ")
    .replaceAll("&#x27;", "'")
    .replaceAll("&amp;", "&")
    .replace(/\s+/g, " ");

/** Each card's own markup, by card id. */
function cardsIn(html: string): Record<string, string> {
  const found: Record<string, string> = {};
  for (const match of html.matchAll(
    /<section[^>]*data-card="([^"]+)"[^>]*>([\s\S]*?)<\/section>/g,
  ))
    found[match[1]!] = match[2]!;
  return found;
}

describe("dashboard cards, from Python's replies through the reads to the page", () => {
  function defaultsWith(layout: CardId[]) {
    return {
      layout,
      goal: 200,
      cutoffs: [10, 50, 100, 200, 500, 1000, 5000],
      group: null,
      groupWindow: "7" as const,
      basesMode: "held" as const,
    };
  }

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"], now: NOW });
    vi.stubEnv("CLASHLENS_DASHBOARD_ENABLED", "true");
    vi.stubEnv("CLASHLENS_PYTHON_API_URL", "http://python-fixture.test/");
    vi.stubEnv("CLASHLENS_PYTHON_HMAC_SECRET_B64", TEST_SECRET);
    vi.stubEnv("CLASHLENS_PYTHON_HMAC_SECRET_FILE", "");
    mocks.getWebsiteConfig.mockReturnValue(
      loadWebsiteConfig({
        NODE_ENV: "test",
        CLASHLENS_LOGIN_ENABLED: "true",
        CLASHLENS_PUBLIC_ORIGIN: ORIGIN,
        CLASHLENS_GOOGLE_CLIENT_ID: "test-client.apps.googleusercontent.com",
        CLASHLENS_GOOGLE_CLIENT_SECRET: "test-client-secret",
        CLASHLENS_DISCORD_CLIENT_ID: "1234567890123456789",
        CLASHLENS_DISCORD_CLIENT_SECRET: "discord-test-secret",
        CLASHLENS_LOGIN_SECRET_B64: "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8",
      }),
    );
    mocks.readLoginIdentity.mockResolvedValue(IDENTITY);
    Object.assign(python, {
      preferences: { dashboard: serializeSettings(defaultsWith(ALL_CARDS)) },
      pages: { [MAIN]: mainPage(), [TUNDRA]: tundraPage() },
      groups: GROUPS,
      liveRank: 316,
      mainBoard: MAIN_BOARD,
      liveMoves: [],
      down: [],
      requests: [],
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: URL | string) => {
        const [status, body] = answer(new URL(String(input)));
        return new Response(JSON.stringify(body), {
          status,
          headers: { "content-type": "application/json" },
        });
      }),
    );
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("draws all 13 cards with the numbers worked out from the saved days", async () => {
    const data = await load();
    const html = await render(data);
    const cards = cardsIn(html);
    expect(Object.keys(cards)).toEqual(ALL_CARDS);
    expect(ALL_CARDS).toHaveLength(13);
    for (const card of ALL_CARDS)
      expect(cards[card]).not.toContain("could not be loaded");

    // Season heat map: ranked against your own days; a shield day is grey.
    const heat = text(cards["heat-map"]!);
    expect(heat).toContain(
      "Up days 6 Best +86 · D8 Down days 0 Worst +2 · D6 No change 0",
    );
    expect(cards["heat-map"]).toContain('aria-label="Day 3: no battles"');
    expect(cards["heat-map"]).toContain('aria-label="Day 7: no data"');
    expect(cards["heat-map"]).toContain('aria-label="Day 9: today, not finished yet"');
    expect(cards["heat-map"]).toContain('aria-label="Day 10: not played yet"');
    expect(cards["heat-map"]).toMatch(
      /background:rgb\(47 179 68\)[^>]*><button[^>]*aria-label="Day 8: \+86"/,
    );
    expect(cards["heat-map"]).toMatch(
      /background:rgb\(224 49 49\)[^>]*><button[^>]*aria-label="Day 6: \+2"/,
    );

    // Recent form: Days 2–8, six of them saved, the shield day counting as 0.
    const form = text(cards["recent-form"]!);
    expect(form).toContain("Last 7 days · Days 2–8 · 6 of 7 recorded");
    expect(form).toContain("Trophies a day +38.3 +230 total");
    expect(form).toContain("Attack +37.8 per attack · 78% tripled");
    expect(form).toContain("Defense −32.0 per defense · 50% held");
    expect(form).toContain("Compares with the 7 days before from Day 15.");

    // Offense consistency: 8 slots a saved day, so the shield day is 8 misses.
    const offense = text(cards["offense-consistency"]!);
    expect(offense).toContain("Last 7 days · 40 attacks");
    expect(offense).toContain("65% hit rate · 31 of 48 tripled");
    expect(offense).toContain("Average per attack +37.8");
    expect(offense).toContain("Average when not tripled +30.0");
    expect(offense).toContain("Trophies missed vs all triples −410");
    expect(offense).toMatch(/7 D2 0 D3 4 D4 8 D5 5 D6 7 D8/);

    // Season graph: a gap where Day 7 is missing; best rank and last Season's finish.
    const graph = text(cards["season-graph"]!);
    expect(graph).toContain("Best rank this Season #318 · Day 8");
    expect(graph).toContain("Last Season finish #412 · 5,288");
    expect(cards["season-graph"]).toContain('aria-label="Day 6: 5,188"');
    expect(cards["season-graph"]).toContain('aria-label="Day 8: 5,324"');
    expect(cards["season-graph"]).toMatch(/class="graph-gap" d="M\S+ \S+ L\S+ \S+ "/);

    // Goal: 76 behind the Top 200 line with 20 days left, the line moving 40 a day.
    const goal = text(cards.goal!);
    expect(goal).toContain("Top 200");
    expect(goal).toContain("+44 a day needed Behind pace by 4 a day");
    expect(goal).toContain("Gap at last Reset −76");
    expect(goal).toContain("Days left 20");
    expect(goal).toContain("Top 200 line moving (7 days) +40 a day");
    expect(goal).toContain("Your pace (7 days) +40 a day");
    expect(goal).toContain("Shield days 1 2 3 +46 · behind by 6");
    expect(cards.goal).toContain('aria-label="Lens Main against the Top 200 line"');

    // Cutoffs: the account slotted between Top 200 and Top 500.
    const cutoffs = text(cards.cutoffs!);
    expect(cutoffs).toContain(
      "Top 10 6,100 −730 Top 50 5,800 −430 Top 100 5,650 −280 Top 200 5,420 −50 Lens Main 5,370 Top 500 5,250 +120 Top 1,000 5,100 +270 Top 5,000 5,010 +360",
    );
    // Your trophies and their time come from the player page; that reading is
    // older than the board's, so the label goes by it.
    expect(data.cards.cutoffs?.live.observedAtMs).toBe(NOW - 10 * 60_000);

    // Rivals watch: 3 above and 3 below on the live board, with moves since the Reset.
    const rivals = text(cards["rivals-watch"]!);
    expect(rivals).toContain("Today so far");
    expect(rivals.match(/#3\d\d/g)).toEqual([
      "#313",
      "#314",
      "#315",
      "#316",
      "#317",
      "#318",
      "#319",
    ]);
    expect(rivals).toContain("#315 ▼5 Tundra 5,375 50% +150 −60");
    expect(rivals).toContain("#316 ▲2 Lens Main 5,370 50% +71 −16");
    expect(rivals).toContain("#317 Player 317 5,365 – – –");
    // Tundra's battles were saved 20 minutes ago, before the board's 3.
    expect(data.cards["rivals-watch"]?.live?.observedAtMs).toBe(NOW - 20 * 60_000);

    // Group watch: the first group made, with you; never another linked account.
    const group = text(cards["group-watch"]!);
    expect(cards["group-watch"]).toMatch(
      /<option value="0f6c1d8e[^"]*" selected="">Clan mates \(3\)<\/option><option value="6a7b8c9d[^"]*">Alpha \(1\)/,
    );
    expect(group).toContain("Last 7 days");
    expect(group).toContain("1. Ava 5,510 +52 50% +280 −240");
    expect(group).toContain("2. Lens Main 5,300 +38");
    // Bo's battle totals include a partial day, so they divide by all 7 saved days.
    expect(group).toContain("3. Bo 5,300 +12 50% +280 −240");
    expect(group).not.toContain("Lens Alt");
    expect(group).not.toContain("Cy");
    expect(cards["group-watch"]).toContain(`href="/account/groups/${CLAN_MATES}"`);

    // Legend clock: today's battles; the dial draws once the browser knows the time.
    expect(text(cards["legend-clock"]!)).toContain("Times in your zone: UTC");
    expect(data.cards["legend-clock"]).toEqual({
      battles: [
        expect.objectContaining({ kind: "attack", stars: 3, trophyChange: 40 }),
        expect.objectContaining({ kind: "attack", stars: 2, trophyChange: 31 }),
        expect.objectContaining({ kind: "defense", stars: 1, trophyChange: -16 }),
      ],
      observedAtMs: NOW - 5 * 60_000,
    });
    const clock = renderToStaticMarkup(
      createElement(ClockCard, {
        data: data.cards["legend-clock"]!,
        nowMs: NOW,
        timeZone: "UTC",
      }),
    );
    expect(clock).toContain('aria-label="Legend clock: 19h 30m to Reset at 05:00"');
    expect(clock).toContain('aria-label="2 attacks"');
    expect(clock).toContain('aria-label="1 defense"');
    expect(clock).toContain("updated 5 min ago");
  });

  it("reads each player page once and asks the boards for the right days", async () => {
    const data = await load();
    const frozen = (offset: number, day: number, limit = 1) => ({
      path: "/v1/leaderboards/frozen",
      query: {
        limit: String(limit),
        offset: String(offset),
        official_season_id: SEASON_ID,
        season_day_number: String(day),
      },
    });
    expect(
      python.requests.filter(({ path }) => path === `/v1/players/${MAIN}`),
    ).toHaveLength(1);
    expect(python.requests).toContainEqual({
      path: "/v1/leaderboards/live",
      query: { limit: "7", offset: "0", focus_tag: MAIN },
    });
    expect(python.requests).toContainEqual(frozen(199, 8));
    expect(python.requests).toContainEqual(frozen(199, 1));
    expect(python.requests).toContainEqual({
      path: "/v1/leaderboards/live",
      query: { limit: "1", offset: "4999" },
    });
    // Rivals at the last Reset: the two pages of 10 holding #315–#321.
    expect(python.requests).toContainEqual(frozen(310, 8, 10));
    expect(python.requests).toContainEqual(frozen(320, 8, 10));
    expect(data.cards["rivals-watch"]?.season?.rows.map((row) => row.rank)).toEqual([
      315, 316, 317, 318, 319, 320, 321,
    ]);
    // Season averages a day for you: 37 of 48 attacks tripled over 7 saved days.
    expect(data.cards["rivals-watch"]?.season?.rows[3]).toMatchObject({
      tag: MAIN,
      you: true,
      hitRate: 37 / 48,
      offense: 1810 / 7,
      defense: -1536 / 7,
    });
    expect(data.cards.cutoffs?.reset).toEqual({
      lines: [
        { top: 10, trophies: 6050 },
        { top: 50, trophies: 5780 },
        { top: 100, trophies: 5630 },
        { top: 200, trophies: 5400 },
        { top: 500, trophies: 5230 },
        { top: 1000, trophies: 5090 },
        { top: 5000, trophies: 5005 },
      ],
      you: 5324,
    });
    expect(python.requests).toContainEqual({
      path: `/v1/account/groups/${CLAN_MATES}/comparison`,
      query: { days: "7" },
    });
  });

  it("keeps 7 live rivals for the first and last places on the board", async () => {
    python.liveRank = 1;
    const top = await load();
    expect(top.cards["rivals-watch"]?.live?.rows.map((row) => row.rank)).toEqual([
      1, 2, 3, 4, 5, 6, 7,
    ]);
    python.liveRank = BOARD_SIZE;
    const bottom = await load();
    expect(bottom.cards["rivals-watch"]?.live?.rows.map((row) => row.rank)).toEqual([
      8994, 8995, 8996, 8997, 8998, 8999, 9000,
    ]);
    expect(bottom.cards["rivals-watch"]?.live?.rows[6]?.you).toBe(true);
  });

  it("takes all 7 live rivals at the top from your own read", async () => {
    python.liveRank = 1;
    // After your read you drop to 6th and the 4th player goes top.
    python.liveMoves = [{}, { 1: rankTag(4), 4: rankTag(5), 5: rankTag(6), 6: MAIN }];
    const data = await load();
    expect(
      data.cards["rivals-watch"]?.live?.rows.map((row) => [row.rank, row.tag]),
    ).toEqual([[1, MAIN], ...[2, 3, 4, 5, 6, 7].map((rank) => [rank, rankTag(rank)])]);
  });

  it("uses a second live read at the bottom only when it alone holds all 7", async () => {
    python.liveRank = BOARD_SIZE;
    const rows = async () =>
      (await load()).cards["rivals-watch"]?.live?.rows.map((row) => [row.rank, row.tag]);
    // Two players swap after your read; the second read still holds all 7.
    python.liveMoves = [{}, { 8996: rankTag(8997), 8997: rankTag(8996) }];
    expect(await rows()).toEqual([
      [8994, rankTag(8994)],
      [8995, rankTag(8995)],
      [8996, rankTag(8997)],
      [8997, rankTag(8996)],
      [8998, rankTag(8998)],
      [8999, rankTag(8999)],
      [9000, MAIN],
    ]);
    // You swap with the player it centres on; it holds 6 of your rows, so
    // your own read's 6 stay.
    python.liveMoves = [{}, { 8998: MAIN, 9000: rankTag(8998) }];
    expect(await rows()).toEqual([
      ...[8995, 8996, 8997, 8998, 8999].map((rank) => [rank, rankTag(rank)]),
      [9000, MAIN],
    ]);
  });

  it("leaves a day not available when Python leaves its result unknown", async () => {
    // Day 6: six triples recorded, eight defenses for −320, and a gap in the battle log.
    python.pages[MAIN] = mainPage({ triples: 6, held: 0, attacks: 6, gap: true });
    const data = await load();
    expect(data.cards["heat-map"]?.days.find((day) => day.day === 6)).toMatchObject({
      net: null,
      endTrophies: null,
      hits: null,
    });
    const cards = cardsIn(await render(data));
    expect(cards["heat-map"]).toContain('aria-label="Day 6: no data"');
    expect(cards["season-graph"]).not.toContain('aria-label="Day 6:');
    const form = text(cards["recent-form"]!);
    expect(form).toContain("Last 7 days · Days 2–8 · 5 of 7 recorded");
    expect(form).toContain("Trophies a day +45.6 +228 total");
    // Day 6's attacks may be missing, so its slots are not counted as misses.
    const offense = text(cards["offense-consistency"]!);
    expect(offense).toContain("Last 7 days · 32 attacks");
    expect(offense).toContain("65% hit rate · 26 of 40 tripled");
    expect(offense).toMatch(/7 D2 0 D3 4 D4 8 D5 – D6 7 D8/);
  });

  it("keeps a day's attacks when only its defenses may be missing", async () => {
    // Day 6: all 8 attacks recorded, 6 defenses, and a gap in the battle log.
    python.pages[MAIN] = mainPage({ defenses: 6, gap: true });
    const data = await load();
    const day6 = data.cards["heat-map"]?.days.find((day) => day.day === 6);
    expect(day6).toMatchObject({ net: null, triples: 5, offense: 290 });
    expect(day6?.hits).toHaveLength(8);
    const offense = text(cardsIn(await render(data))["offense-consistency"]!);
    expect(offense).toContain("Last 7 days · 40 attacks");
    expect(offense).toContain("65% hit rate · 31 of 48 tripled");
    expect(offense).toMatch(/7 D2 0 D3 4 D4 8 D5 5 D6 7 D8/);
  });

  it("takes your Cutoffs trophies and Season reset from the player page", async () => {
    python.pages[MAIN] = { ...mainPage(), season_reset_pending: true };
    const data = await load();
    expect(data.cards.cutoffs?.live).toMatchObject({
      you: null,
      observedAtMs: NOW - 3 * 60_000,
    });
  });

  it("says when your own Group watch row could not be loaded", async () => {
    python.preferences = {
      dashboard: serializeSettings({
        ...defaultsWith(["group-watch"]),
        group: ALPHA,
        groupWindow: "season",
      }),
    };
    python.pages = { [TUNDRA]: tundraPage() };
    const group = text(cardsIn(await render(await load()))["group-watch"]!);
    expect(group).toContain("Your numbers could not be loaded.");
    expect(group).not.toContain("Tundra");
    // The same note when nobody else in the group has numbers either.
    python.groups = [{ ...GROUPS[0], tags: [] }];
    const empty = text(cardsIn(await render(await load()))["group-watch"]!);
    expect(empty).toContain("Your numbers could not be loaded.");
  });

  it("adds a reset at the closing Reset to a day's end when the next day has no start", async () => {
    const page = mainPage();
    Object.assign(
      page.screen_ready.days.find((day) => day.season_day_number === 8)!,
      {
        adjustments: [{ type: "weekly_reset", amount: 80 }],
      },
    );
    python.pages[MAIN] = page;
    const data = await load();
    // 5,238 at the start, +86 from battles, +80 from the reset.
    expect(
      data.cards["season-graph"]?.days.find((day) => day.day === 8)?.endTrophies,
    ).toBe(5404);
    // Cutoffs' Last Reset keeps your trophies on that day's board.
    expect(data.cards.cutoffs?.reset?.you).toBe(5324);
  });

  it("leaves a day's attacks unknown when more than 8 are recorded", async () => {
    // Day 6: nine recorded triples in an unbroken battle log.
    python.pages[MAIN] = mainPage({ attacks: 9, triples: 9 });
    const data = await load();
    expect(data.cards["heat-map"]?.days.find((day) => day.day === 6)?.hits).toBeNull();
    const offense = text(cardsIn(await render(data))["offense-consistency"]!);
    expect(offense).toContain("65% hit rate · 26 of 40 tripled");
    expect(offense).toMatch(/7 D2 0 D3 4 D4 8 D5 – D6 7 D8/);
  });

  it("shows today's read on Legend day and Bases you attacked, and the army reads", async () => {
    const cards = cardsIn(await render(await load()));

    // Legend day: the page's trophies and battles, the read's range and open defenses.
    const day = text(cards["legend-day"]!);
    expect(day).toContain("Legend day · Lens Main Day 9 of 28");
    expect(day).toContain("Trophies 5,370 Net today –");
    expect(day).toContain("Last Reset #318 → Now #316 → Next Reset #95 – #760");
    expect(day).toContain("2 attacks");
    expect(day).toContain("1 defense");
    expect(day).toContain("7 defenses remain · auto defense −21 each");

    // Bases you attacked: hardest first against 41% held at similar trophies.
    const bases = text(cards.bases!);
    expect(bases).toContain("Bases you attacked · today · 2 of 8");
    expect(bases).toContain("Base strength · vs Legends at similar trophies today");
    expect(bases).toContain("Ava 5,712 ★★★ 100% +40 · 07:10");
    expect(bases).toContain("Hard held 5 of 6 · 83% vs 41%");
    expect(bases).toContain("Bo 5,688 ★★☆ 88% +31 · 07:25");
    expect(bases).toContain("Easy held 1 of 4 · 25% vs 41%");
    expect(bases.indexOf("Ava")).toBeLessThan(bases.indexOf("Bo"));

    // Armies on defense: the costliest armies first, each beside the Legends average.
    const armiesDefense = text(cards["armies-defense"]!);
    expect(armiesDefense).toContain("Armies on defense 3 days 7 days Custom");
    expect(armiesDefense).toContain("Last 7 days · 8 defenses");
    expect(armiesDefense).toContain(
      "Army type Share Held / Legends avg Avg lost / Legends avg",
    );
    expect(armiesDefense).toContain("Thrower 63% 60% / 29% −34 / −37");
    expect(armiesDefense).toContain("Hydra 38% 0% / 50% −40 / −33");
    expect(armiesDefense).toContain("All defenses 100% 38% / 52% −36 / −32");

    // Armies on offense: one army, so the two hit rates big.
    const armiesOffense = text(cards["armies-offense"]!);
    expect(armiesOffense).toContain("Armies on offense 3 days 7 days");
    expect(armiesOffense).not.toContain("Custom");
    expect(armiesOffense).toContain("Thrower You 75% Legends 50%");
    expect(armiesOffense).not.toContain("Tripled");

    // One read of today serves both cards; each army card starts at 7 days.
    const reads = python.requests.filter(({ path }) =>
      path.startsWith(`/v1/players/${MAIN}/`),
    );
    expect(reads.filter(({ path }) => path.endsWith("/today"))).toHaveLength(1);
    expect(
      reads.filter(({ path }) => path.endsWith("/armies")).map(({ query }) => query),
    ).toEqual(
      expect.arrayContaining([
        { lens: "defense", days: "7" },
        { lens: "offense", days: "7" },
      ]),
    );
  });

  it("rates the bases by trophies given up when that is the saved setting", async () => {
    python.preferences = {
      dashboard: serializeSettings({ ...defaultsWith(["bases"]), basesMode: "trophies" }),
    };
    const bases = text(cardsIn(await render(await load())).bases!);
    expect(bases).toContain("Base strength · vs all Legends −32 a defense");
    expect(bases).toContain("Hard gave up −15 a defense");
    expect(bases).toContain("Easy gave up −38 a defense");
  });

  it("keeps Legend day from the player page when today's read fails", async () => {
    python.down = ["/today", "/armies"];
    const cards = cardsIn(await render(await load()));
    const day = text(cards["legend-day"]!);
    expect(day).toContain("Trophies 5,370");
    expect(day).toContain("Next Reset –");
    expect(day).not.toContain("defenses remain");
    for (const card of ["bases", "armies-defense", "armies-offense"])
      expect(text(cards[card]!)).toContain("This card could not be loaded right now.");
  });

  it("follows the saved Goal, Cutoffs lines and group for the whole Season", async () => {
    python.preferences = {
      dashboard: serializeSettings({
        layout: ["goal", "cutoffs", "group-watch"],
        goal: 50,
        cutoffs: [10, 20, 50, 100, 200, 300, 400],
        group: ALPHA,
        groupWindow: "season",
        basesMode: "held",
      }),
    };
    const data = await load();
    const cards = cardsIn(await render(data));
    expect(Object.keys(cards)).toEqual(["goal", "cutoffs", "group-watch"]);
    expect(text(cards.goal!)).toContain("Top 50");
    expect(python.requests).toContainEqual({
      path: "/v1/leaderboards/frozen",
      query: {
        limit: "1",
        offset: "49",
        official_season_id: SEASON_ID,
        season_day_number: "8",
      },
    });
    expect(data.cards.cutoffs?.live.lines.map((line) => line.top)).toEqual([
      10, 20, 50, 100, 200, 300, 400,
    ]);
    // Whole Season reads each member's page, plus yours; no 7-day comparison.
    expect(python.requests.some(({ path }) => path.endsWith("/comparison"))).toBe(false);
    const group = text(cards["group-watch"]!);
    expect(group).toContain("Whole Season · 8 days");
    expect(group).toContain("1. Tundra 5,375 +128 100% +320 −192");
    expect(group).toContain("2. Lens Main 5,370 +39 77% +259 −219");
  });

  it("shows one card as unavailable when only its reads fail", async () => {
    python.down = ["/v1/account/groups", "/seasons"];
    const data = await load();
    const cards = cardsIn(await render(data));
    expect(text(cards["group-watch"]!)).toContain(
      "This card could not be loaded right now. Reload the page to try again.",
    );
    // A missing last-Season finish leaves the graph standing.
    expect(text(cards["season-graph"]!)).toContain("Last Season finish –");
    expect(text(cards.goal!)).toContain("+44 a day needed");
  });

  it("shows a failed read as unavailable, not as nothing saved", async () => {
    const unavailable =
      "This card could not be loaded right now. Reload the page to try again.";
    python.down = ["/v1/leaderboards/frozen"];
    const data = await load();
    expect(data.cards.goal).toBeNull();
    expect(data.cards["rivals-watch"]).toBeNull();
    const cards = cardsIn(await render(data));
    expect(text(cards.goal!)).toContain(unavailable);
    expect(text(cards.goal!)).not.toContain("not on a saved Reset board");
    expect(text(cards["heat-map"]!)).not.toContain("could not be loaded");
    python.down = [`/v1/players/${MAIN}`];
    const cutoffs = text(cardsIn(await render(await load())).cutoffs!);
    expect(cutoffs).toContain(unavailable);
  });

  it("places you against saved boards with your trophies on them", async () => {
    // Day 8's board holds you at 5,444, before the automatic defense loss;
    // the day ends 120 lower, at 5,324, after it.
    python.mainBoard = { ...MAIN_BOARD, 8: [318, 5444] };
    const data = await load();
    expect(data.cards.cutoffs?.reset?.you).toBe(5444);
    expect(data.cards.goal).toMatchObject({
      you: 5324,
      youThen: 5044,
      history: [5044, 5114, 5114, 5122, 5186, 5188, null, 5444],
    });
    const goal = text(cardsIn(await render(data)).goal!);
    expect(goal).toContain("Gap at last Reset +44");
    expect(goal).toContain("Your pace (7 days) +40 a day");
  });

  it("hides the day's numbers once the Legend day they were read for ends", async () => {
    const html = await render({ ...(await load()), dayEndsMs: NOW });
    expect(text(html)).toContain(
      "A new Legend day has started. Reload for today's numbers",
    );
    const tabs = text(html.match(/<nav class="dash-accounts"[\s\S]*?<\/nav>/)![0]);
    expect(tabs).toContain("Lens Main 5,360 · #316");
    expect(tabs).not.toContain("+55");
    const cards = cardsIn(html);
    expect(Object.keys(cards)).toEqual(ALL_CARDS);
    for (const card of ALL_CARDS)
      expect(text(cards[card]!).trim()).toBe(CARDS[card as CardId].title);
  });

  it("asks to make a group when there is none", async () => {
    python.groups = [];
    const cards = cardsIn(await render(await load()));
    expect(text(cards["group-watch"]!)).toContain(
      "Make a group first, then this card compares you with its players.",
    );
    expect(cards["group-watch"]).toContain('href="/account/groups"');
  });

  it("waits for the first Reset on Day 1", async () => {
    const page = mainPage();
    page.screen_ready.season.current_day_number = 1;
    page.screen_ready.season_day_starts = [];
    page.screen_ready.recent_day_starts = [];
    python.pages[MAIN] = page;
    const cards = cardsIn(await render(await load()));
    expect(text(cards.goal!)).toContain(
      "Needs one full day. The plan starts after the first Reset.",
    );
    for (const card of ["heat-map", "season-graph", "recent-form", "offense-consistency"])
      expect(text(cards[card]!)).toContain(
        "Results start after the Season's first Reset.",
      );
    expect(text(cards.cutoffs!)).toContain("Top 200 5,420");
  });
});

describe("dashboard card numbers", () => {
  const season = (days: SeasonData["days"], today = 9): SeasonData => ({ today, days });
  const day = (number: number, net: number, changes = {}) => ({
    day: number,
    attacks: 8,
    defenses: 8,
    offense: 300,
    defense: net - 300,
    net,
    triples: 6,
    held: 4,
    endTrophies: 5000 + net,
    rank: null,
    hits: [],
    ...changes,
  });

  it("compares the last 3 days with the 3 before once the Season has them", () => {
    const form = recentForm(
      season([day(3, 0), day(4, 8), day(5, 64), day(6, 2), day(8, 86)]),
      3,
    );
    expect(form?.window).toMatchObject({ first: 6, last: 8, span: 3 });
    expect(form?.now.net).toBe(44);
    expect(form?.before?.net).toBe(24);
    // Day 3 of the Season: only 2 finished days and nothing before them.
    expect(recentForm(season([day(1, 10), day(2, 20)], 3), 3)?.before).toBeNull();
    expect(recentForm(season([], 1), 7)).toBeNull();
  });

  it("gives equal days one colour and leaves shield days out", () => {
    const cells = heatCells(
      season([
        day(1, 10),
        day(2, 10),
        day(3, 30),
        day(4, 0, { attacks: 0, defenses: 0 }),
        day(5, -20),
      ]),
      "net",
    );
    expect(cells.slice(0, 5).map((cell) => [cell.kind, cell.shade])).toEqual([
      ["played", 0.5],
      ["played", 0.5],
      ["played", 1],
      ["shield", null],
      ["played", 0],
    ]);
  });

  it("ranks a day with no battles but an automatic loss, not as a shield day", () => {
    const quiet = { attacks: 0, defenses: 0, offense: 0, defense: 0 };
    const lost = day(3, -304, quiet);
    expect(isShieldDay(lost)).toBe(false);
    expect(isShieldDay(day(2, 0, quiet))).toBe(true);
    expect(isShieldDay({ ...day(4, 0, quiet), net: null })).toBe(false);
    const cells = heatCells(season([day(1, 40), day(2, 0, quiet), lost]), "net");
    expect(cells.slice(0, 3).map((cell) => [cell.kind, cell.shade])).toEqual([
      ["played", 1],
      ["shield", null],
      ["played", 0],
    ]);
  });

  it("counts every slot of a short day as a miss", () => {
    const stats = offenseConsistency(
      season([
        day(8, 0, {
          hits: [
            [3, 40],
            [3, 40],
            [1, 12],
          ],
        }),
      ]),
      3,
    );
    expect(stats).toMatchObject({
      attacks: 3,
      slots: 8,
      triples: 2,
      stars: [2, 0, 1, 0],
    });
    expect(stats?.missed).toBe(92 - 320);
    expect(stats?.perMiss).toBe(12);
  });

  it("says ahead, on pace or behind, with what each shield costs", () => {
    const goal = (changes: Partial<GoalData>): GoalData => ({
      rank: 200,
      name: "Lens Main",
      finished: 8,
      window: 7,
      you: 5324,
      youThen: 5044,
      line: 5400,
      lineThen: 5120,
      history: [5044, changes.you ?? 5324],
      ...changes,
    });
    expect(goalPlan(goal({}))?.status).toBe("behind");
    expect(goalPlan(goal({ youThen: 5000 }))?.status).toBe("on");
    expect(goalPlan(goal({ you: 5600, youThen: 5300, lineThen: 5400 }))).toMatchObject({
      status: "ahead",
      gap: 200,
    });
    // On Day 27 a shield of 1 still leaves a day; 2 and 3 do not.
    expect(goalPlan(goal({ finished: 26 }))?.shields.map((item) => item.days)).toEqual([
      1,
    ]);
    expect(goalPlan(goal({ line: null }))).toBeNull();
    // The gap goes by your trophies on the board; the pace by your day ends.
    expect(goalPlan(goal({ history: [5044, 5444] }))).toMatchObject({
      gap: 44,
      pace: 40,
    });
    expect(goalPlan(goal({ history: [5044, null] }))).toBeNull();
  });

  it("keeps 3 above and 3 below, filling from the other side at the edges", () => {
    const rows = Array.from({ length: 11 }, (_, index) => index + 1);
    expect(around(rows, 5, 3, 3)).toEqual([3, 4, 5, 6, 7, 8, 9]);
    expect(around(rows, 0, 3, 3)).toEqual([1, 2, 3, 4, 5, 6, 7]);
    expect(around(rows, 10, 3, 3)).toEqual([5, 6, 7, 8, 9, 10, 11]);
    expect(around([1, 2, 3], 1, 3, 3)).toEqual([1, 2, 3]);
  });
});
