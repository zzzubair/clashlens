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
  createPythonClient: vi.fn(),
  getGroupComparison: vi.fn(),
}));

vi.mock("../../app/server/config.server", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/server/config.server")>();
  return { ...actual, getWebsiteConfig: mocks.getWebsiteConfig };
});

vi.mock("../../app/server/actions.server", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/server/actions.server")>();
  return { ...actual, readLoginIdentity: mocks.readLoginIdentity };
});

vi.mock("../../app/services/python.server", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../app/services/python.server")>();
  return { ...actual, createPythonClient: mocks.createPythonClient };
});

vi.mock("../../app/services/group-comparison.server", () => ({
  getGroupComparison: mocks.getGroupComparison,
}));

import { ClockCard } from "../../app/components/BoardCards";
import type { LinkedPlayerCard, ListedGroup } from "../../app/lib/account-contracts";
import type {
  PlayerPage,
  RankedBattleEvent,
  RankedDaySummary,
  TrackedLeaderboard,
  TrackedPlayerEntry,
} from "../../app/lib/contracts";
import { CARDS, serializeSettings, type CardId } from "../../app/lib/dashboard";
import {
  around,
  goalPlan,
  heatCells,
  offenseConsistency,
  recentForm,
  type GoalData,
  type SeasonData,
} from "../../app/lib/dashboard-cards";
import type { ComparedPlayer } from "../../app/lib/group-comparison";
import DashboardRoute, {
  loader,
  type DashboardLoaderData,
} from "../../app/routes/dashboard";
import { loadWebsiteConfig } from "../../app/server/config.server";
import { PythonApiError } from "../../app/services/python.server";

const ORIGIN = "https://clashlens.example";
const IDENTITY = { provider: "google", providerSubject: "11223344556677889900" } as const;
const HOUR = 3_600_000;
const DAY = 24 * HOUR;
/** The Season started at this Reset; the page is read on Day 9 at 09:30 UTC. */
const ANCHOR = Date.UTC(2026, 8, 29, 5);
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

const iso = (ms: number) => new Date(ms).toISOString();
const dayStart = (day: number) => ANCHOR + (day - 1) * DAY;

let battleIds = 0;
function event(at: number, stars: number, trophyChange: number): RankedBattleEvent {
  battleIds += 1;
  return {
    battleId: `battle-${battleIds}`,
    battleTimestamp: iso(at),
    opponent: { tag: "#2PY", name: "Opponent" },
    destructionPercentage: stars === 3 ? 100 : 80,
    stars,
    trophyChange,
    perspectiveDisagreement: false,
    armyShareCode: null,
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
}

/** One finished Legend day as the player page sends it, every battle recorded. */
function savedDay(plan: DayPlan): RankedDaySummary {
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
    dayNumber: plan.day,
    label: "Ranked day",
    period: `${iso(start)} – ${iso(start + DAY)}`,
    state: "Complete",
    confidence: "exact",
    startTrophies: plan.start,
    offense: { attacks, threeStars: Math.min(plan.triples, attacks), trophyGain: gain },
    defense: { defenses, threeStarsAgainst: defenses - plan.held, trophyLoss: loss },
    trophyChange: gain - loss,
    resetRank: plan.rank,
    battlesComplete: true,
    offenseEvents: hits.map((value, index) =>
      event(start + (index + 1) * HOUR, value === 40 ? 3 : 2, value),
    ),
    defenseEvents: losses.map((value, index) =>
      event(start + (index + 1.5) * HOUR, value === 40 ? 3 : 2, -value),
    ),
    completeness: { state: "complete", reason: "Complete evidence." },
    uncertainty: [],
  };
}

/** Day 9 so far. */
function today(
  offense: RankedDaySummary["offense"],
  defense: RankedDaySummary["defense"],
  events: { offense: RankedBattleEvent[]; defense: RankedBattleEvent[] },
): RankedDaySummary {
  const start = dayStart(9);
  return {
    dayNumber: 9,
    label: "Ranked day",
    period: `${iso(start)} – ${iso(start + DAY)}`,
    state: "Live",
    confidence: null,
    offense,
    defense,
    trophyChange: null,
    battlesComplete: false,
    offenseEvents: events.offense,
    defenseEvents: events.defense,
    completeness: { state: "partial", reason: "The day is not over." },
    uncertainty: [],
  };
}

function playerPage(
  tag: string,
  name: string,
  trophies: number,
  days: RankedDaySummary[],
  current: RankedDaySummary | null,
): PlayerPage {
  return {
    kind: "player-page",
    tag,
    trackingState: "tracking",
    profile: {
      tag,
      name,
      clan: "",
      trophies,
      seasonResetPending: false,
      currentLeagueSeasonId: SEASON_ID,
      freshness: { state: "fresh", observedAt: iso(NOW - 10 * 60_000), ageSeconds: 600 },
      confidence: "high",
      coverage: "complete",
      eligibility: "legend-i",
    },
    season: {
      id: SEASON_ID,
      anchor: iso(ANCHOR),
      currentDayNumber: 9,
      dayCount: 28,
      anchorSource: "official_league_history",
      anchorObservedAt: iso(ANCHOR),
    },
    currentDay: current,
    currentDayPublishedAt: iso(NOW - 5 * 60_000),
    // The last two days also arrive as recent days; each counts once.
    recentDays: days.slice(-2),
    seasonDays: days,
    dataQuality: [],
    provenance: {} as PlayerPage["provenance"],
  };
}

/**
 * The selected account's Season: Day 3 a shield day, Day 7 never saved.
 * Net by day: +44, +70, 0, +8, +64, +2, (missing), +86.
 */
const MAIN_DAYS = [
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
  savedDay({ day: 6, triples: 5, held: 2, start: 5186, rank: 520 }),
  savedDay({ day: 8, triples: 7, held: 6, start: 5238, rank: 318 }),
];

function mainPage(): PlayerPage {
  const start = dayStart(9);
  return playerPage(
    MAIN,
    "Lens Main",
    5370,
    MAIN_DAYS,
    today(
      { attacks: 2, threeStars: 1, trophyGain: 71 },
      { defenses: 1, threeStarsAgainst: 0, trophyLoss: 16 },
      {
        // 07:10 and 07:25 share one mark on the clock.
        offense: [
          event(start + 2 * HOUR + 10 * 60_000, 3, 40),
          event(start + 2 * HOUR + 25 * 60_000, 2, 31),
        ],
        defense: [event(start + 4 * HOUR, 1, -16)],
      },
    ),
  );
}

/** A rival 1 place above, who was #310 at the last Reset. */
function tundraPage(): PlayerPage {
  return playerPage(
    TUNDRA,
    "Tundra",
    5375,
    [savedDay({ day: 8, triples: 8, held: 8, start: 5180, rank: 310 })],
    today(
      { attacks: 4, threeStars: 2, trophyGain: 150 },
      { defenses: 2, threeStarsAgainst: 1, trophyLoss: 60 },
      { offense: [], defense: [] },
    ),
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

/** The selected account is #316 live and was #318 on the Day 8 Reset board. */
function boardEntry(rank: number, day: number | null): TrackedPlayerEntry {
  const tag =
    (day === null && rank === 316) || (day === 8 && rank === 318)
      ? MAIN
      : rank === 315
        ? TUNDRA
        : `#P${rank}`;
  const lines = day === null ? LIVE_LINES : (RESET_LINES[day] ?? {});
  return {
    rank,
    tag,
    name: tag === MAIN ? "Lens Main" : tag === TUNDRA ? "Tundra" : `Player ${rank}`,
    clan: "",
    trophies: lines[rank] ?? (day === null ? 5450 : 5414) - 5 * (rank - 300),
    freshness: { state: "fresh", observedAt: iso(NOW - 3 * 60_000), ageSeconds: 180 },
    state: "available",
    confidence: "high",
  } as TrackedPlayerEntry;
}

function board(entries: TrackedPlayerEntry[]): TrackedLeaderboard {
  return {
    kind: "tracked-leaderboard",
    entries,
    totalEntries: 9000,
  } as TrackedLeaderboard;
}

function compared(
  tag: string,
  name: string,
  changes: Partial<ComparedPlayer> & { netPerDay: number },
): ComparedPlayer {
  return {
    tag,
    name,
    you: false,
    inGroup: true,
    status: "tracking",
    trophies: 5300,
    seasonResetPending: false,
    observedAt: null,
    ageSeconds: null,
    freshness: null,
    today: null,
    days: [],
    countedDays: 7,
    countedAttacks: 56,
    net: changes.netPerDay * 7,
    vsGroup: null,
    attack: { count: 56, stars: 150, destruction: 5000, threeStars: 28, trophies: 1960 },
    defense: {
      count: 56,
      stars: 140,
      destruction: 5000,
      trophies: 1680,
      starCounts: [0, 0, 0, 0],
    },
    ...changes,
  };
}

function linked(tag: string, changes: Partial<LinkedPlayerCard> = {}): LinkedPlayerCard {
  return {
    tag,
    name: "Lens Main",
    clan: null,
    state: "tracking",
    reason: null,
    trophies: 5370,
    seasonResetPending: false,
    rank: 316,
    league: null,
    today: { net: 55, attacks: 2, defenses: 1 },
    ...changes,
  };
}

const ALL_CARDS = Object.keys(CARDS).filter(
  (card) => CARDS[card as CardId].built,
) as CardId[];

function fakeClient(preferences: Record<string, unknown>) {
  const groups: ListedGroup[] = [
    {
      groupId: ALPHA,
      name: "Alpha",
      tags: [TUNDRA],
      players: [],
      createdAt: "2026-10-01T10:00:00+00:00",
    },
    {
      groupId: CLAN_MATES,
      name: "Clan mates",
      tags: [AVA, BO, CY],
      players: [],
      createdAt: "2026-09-01T10:00:00+00:00",
    },
  ];
  return {
    getAccount: vi.fn(async () => ({
      username: "nova88",
      displayName: "Nova",
      preferences,
      providers: ["google"],
    })),
    getPublicUser: vi.fn(async () => ({
      username: "nova88",
      displayName: "Nova",
      verifiedPlayers: [linked(MAIN), linked(ALT, { name: "Lens Alt", rank: 2000 })],
    })),
    getPlayer: vi.fn(async (tag: string) => {
      if (tag === MAIN) return mainPage();
      if (tag === TUNDRA) return tundraPage();
      throw new PythonApiError(404, { error: "not_found" });
    }),
    getPlayerSeasons: vi.fn(async () => [
      {
        seasonId: String(ANCHOR / 1000 - 28 * 86_400),
        coverageState: "complete",
        daysObserved: 28,
        daysMissing: 0,
        source: "official_league_history",
        officialHistory: {
          observedAt: iso(ANCHOR),
          eodTrophies: 5288,
          finalPlacement: 412,
        },
      },
    ]),
    getTrackedLeaderboard: vi.fn(
      async (
        limit = 25,
        view: "live" | "daily" = "live",
        offset = 0,
        selector?: { officialSeasonId: string; dayNumber: number },
        focus?: string,
      ) => {
        if (focus) {
          return board(
            Array.from({ length: 11 }, (_, index) => boardEntry(311 + index, null)),
          );
        }
        const day = view === "daily" ? (selector?.dayNumber ?? null) : null;
        return board(
          Array.from({ length: limit }, (_, index) =>
            boardEntry(offset + index + 1, day),
          ),
        );
      },
    ),
    listGroups: vi.fn(async () => ({ season: SEASON_ID, groups })),
  };
}

const loaderArgs = (url: string) =>
  ({ request: new Request(url), params: {}, context: {} }) as unknown as Parameters<
    typeof loader
  >[0];

async function load() {
  const result = (await loader(loaderArgs(`${ORIGIN}/dashboard`))) as unknown as {
    data: DashboardLoaderData;
  };
  if (result.data.kind !== "signed-in") throw new Error("expected signed in");
  return result.data;
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

describe("dashboard cards, from saved days through the reads to the page", () => {
  let client: ReturnType<typeof fakeClient>;

  function useClient(preferences: Record<string, unknown>) {
    client = fakeClient(preferences);
    mocks.createPythonClient.mockReturnValue(client);
  }

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"], now: NOW });
    vi.stubEnv("CLASHLENS_DASHBOARD_ENABLED", "true");
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
    mocks.getGroupComparison.mockReset();
    mocks.getGroupComparison.mockResolvedValue({
      groupId: CLAN_MATES,
      name: "Clan mates",
      days: 7,
      players: [
        compared(MAIN, "Lens Main", { you: true, inGroup: false, netPerDay: 38 }),
        compared(AVA, "Ava", { netPerDay: 52, trophies: 5510 }),
        compared(BO, "Bo", { netPerDay: 12 }),
        // Another linked account outside the group, and a member who left Legend I.
        compared(ALT, "Lens Alt", { you: true, inGroup: false, netPerDay: 90 }),
        compared(CY, "Cy", { status: "not_in_legend", netPerDay: 99 }),
      ],
    });
    useClient({ dashboard: serializeSettings({ ...defaultsWith(ALL_CARDS) }) });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllEnvs();
  });

  function defaultsWith(layout: CardId[]) {
    return {
      layout,
      goal: 200,
      cutoffs: [10, 50, 100, 200, 500, 1000, 5000],
      group: null,
      groupWindow: "7" as const,
    };
  }

  it("draws all 9 cards with the numbers worked out from the saved days", async () => {
    const data = await load();
    const html = await render(data);
    const cards = cardsIn(html);
    expect(Object.keys(cards)).toEqual(ALL_CARDS);
    expect(ALL_CARDS).toHaveLength(9);
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
    expect(graph).toContain("Best rank this Season #318 · 5,324 · Day 8");
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

    // Group watch: the first group made, with you; never another linked account.
    const group = text(cards["group-watch"]!);
    expect(cards["group-watch"]).toMatch(
      /<option value="0f6c1d8e[^"]*" selected="">Clan mates \(3\)<\/option><option value="6a7b8c9d[^"]*">Alpha \(1\)/,
    );
    expect(group).toContain("Last 7 days");
    expect(group).toContain("1. Ava 5,510 +52 50% +280 −240");
    expect(group).toContain("2. Lens Main 5,300 +38");
    expect(group).toContain("3. Bo 5,300 +12");
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
    const selector = (dayNumber: number) => ({ officialSeasonId: SEASON_ID, dayNumber });
    expect(client.getPlayer.mock.calls.filter(([tag]) => tag === MAIN)).toHaveLength(1);
    expect(client.getTrackedLeaderboard).toHaveBeenCalledWith(
      1,
      "live",
      0,
      undefined,
      MAIN,
    );
    expect(client.getTrackedLeaderboard).toHaveBeenCalledWith(
      1,
      "daily",
      199,
      selector(8),
    );
    expect(client.getTrackedLeaderboard).toHaveBeenCalledWith(
      1,
      "daily",
      199,
      selector(1),
    );
    expect(client.getTrackedLeaderboard).toHaveBeenCalledWith(1, "live", 4999, undefined);
    // Rivals at the last Reset: the two pages of 10 holding #315–#321.
    expect(client.getTrackedLeaderboard).toHaveBeenCalledWith(
      10,
      "daily",
      310,
      selector(8),
    );
    expect(client.getTrackedLeaderboard).toHaveBeenCalledWith(
      10,
      "daily",
      320,
      selector(8),
    );
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
    expect(mocks.getGroupComparison).toHaveBeenCalledWith(IDENTITY, CLAN_MATES, 7);
  });

  it("follows the saved Goal, Cutoffs lines and group for the whole Season", async () => {
    useClient({
      dashboard: serializeSettings({
        layout: ["goal", "cutoffs", "group-watch"],
        goal: 50,
        cutoffs: [10, 20, 50, 100, 200, 300, 400],
        group: ALPHA,
        groupWindow: "season",
      }),
    });
    const data = await load();
    const cards = cardsIn(await render(data));
    expect(Object.keys(cards)).toEqual(["goal", "cutoffs", "group-watch"]);
    expect(text(cards.goal!)).toContain("Top 50");
    expect(client.getTrackedLeaderboard).toHaveBeenCalledWith(1, "daily", 49, {
      officialSeasonId: SEASON_ID,
      dayNumber: 8,
    });
    expect(data.cards.cutoffs?.live.lines.map((line) => line.top)).toEqual([
      10, 20, 50, 100, 200, 300, 400,
    ]);
    // Whole Season reads each member's page, plus yours; no 7-day comparison.
    expect(mocks.getGroupComparison).not.toHaveBeenCalled();
    const group = text(cards["group-watch"]!);
    expect(group).toContain("Whole Season · 8 days");
    expect(group).toContain("1. Tundra 5,375 +128 100% +320 −192");
    expect(group).toContain("2. Lens Main 5,370 +39 77% +259 −219");
  });

  it("shows one card as unavailable when only its reads fail", async () => {
    client.listGroups.mockRejectedValue(
      new PythonApiError(503, { error: "unavailable" }),
    );
    client.getPlayerSeasons.mockRejectedValue(
      new PythonApiError(503, { error: "unavailable" }),
    );
    const data = await load();
    const cards = cardsIn(await render(data));
    expect(text(cards["group-watch"]!)).toContain(
      "This card could not be loaded right now. Reload the page to try again.",
    );
    // A missing last-Season finish leaves the graph standing.
    expect(text(cards["season-graph"]!)).toContain("Last Season finish –");
    expect(text(cards.goal!)).toContain("+44 a day needed");
  });

  it("asks to make a group when there is none", async () => {
    client.listGroups.mockResolvedValue({ season: SEASON_ID, groups: [] });
    const cards = cardsIn(await render(await load()));
    expect(text(cards["group-watch"]!)).toContain(
      "Make a group first, then this card compares you with its players.",
    );
    expect(cards["group-watch"]).toContain('href="/account/groups"');
  });

  it("waits for the first Reset on Day 1", async () => {
    const page = mainPage();
    client.getPlayer.mockResolvedValue({
      ...page,
      season: { ...page.season!, currentDayNumber: 1 },
      seasonDays: [],
      recentDays: [],
    });
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
      history: [],
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
  });

  it("keeps 3 above and 3 below, filling from the other side at the edges", () => {
    const rows = Array.from({ length: 11 }, (_, index) => index + 1);
    expect(around(rows, 5, 3, 3)).toEqual([3, 4, 5, 6, 7, 8, 9]);
    expect(around(rows, 0, 3, 3)).toEqual([1, 2, 3, 4, 5, 6, 7]);
    expect(around(rows, 10, 3, 3)).toEqual([5, 6, 7, 8, 9, 10, 11]);
    expect(around([1, 2, 3], 1, 3, 3)).toEqual([1, 2, 3]);
  });
});
