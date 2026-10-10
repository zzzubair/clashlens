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
  requireLogin: vi.fn(),
  readLoginIdentity: vi.fn(),
  createPythonClient: vi.fn(),
  requestJson: vi.fn(),
}));

vi.mock("../../app/server/config.server", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/server/config.server")>();
  return { ...actual, getWebsiteConfig: mocks.getWebsiteConfig };
});

vi.mock("../../app/server/auth-guard.server", () => ({
  requireLogin: mocks.requireLogin,
}));

vi.mock("../../app/server/actions.server", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/server/actions.server")>();
  return { ...actual, readLoginIdentity: mocks.readLoginIdentity };
});

vi.mock("../../app/services/python.server", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../app/services/python.server")>();
  return {
    ...actual,
    createPythonClient: mocks.createPythonClient,
    requestJson: mocks.requestJson,
  };
});

import { LegendClock, clockMarks } from "../../app/components/LegendClock";
import { LegendDayCard } from "../../app/components/LegendDayCard";
import { OpponentsCard } from "../../app/components/OpponentsCard";
import App, { loader as rootLoader } from "../../app/root";
import type { LinkedPlayerCard } from "../../app/lib/account-contracts";
import type { ClockBattle, OpponentRow, PlayerDay } from "../../app/lib/dashboard";
import {
  MAX_CARDS_PER_TAB,
  baseStrength,
  defaultLayout,
  nextResetMs,
  parsePostedLayout,
  readSavedLayout,
  serializeLayout,
} from "../../app/lib/dashboard";
import DashboardRoute, {
  action,
  loader,
  type DashboardActionData,
  type DashboardLoaderData,
} from "../../app/routes/dashboard";
import { loadWebsiteConfig } from "../../app/server/config.server";
import { PythonApiError } from "../../app/services/python.server";

const ORIGIN = "https://clashlens.example";
const IDENTITY = { provider: "google", providerSubject: "11223344556677889900" } as const;
const IDEMPOTENCY_KEY = "3be934b5-68fa-4741-8c7b-e03592e4ad70";
const MAIN = "#LQ2V8PJ0";
const ALT = "#2PP8QLY0";

function linked(
  tag: string,
  overrides: Partial<LinkedPlayerCard> = {},
): LinkedPlayerCard {
  return {
    tag,
    name: "Lens Main",
    clan: null,
    state: "tracking",
    reason: null,
    trophies: 5696,
    seasonResetPending: false,
    rank: 318,
    league: null,
    today: { net: 46, attacks: 5, defenses: 6 },
    ...overrides,
  };
}

function unwrap<T>(result: unknown): { data: T; status: number } {
  const wrapped = result as { data: T; init: { status?: number } | null };
  return { data: wrapped.data, status: wrapped.init?.status ?? 200 };
}

const loaderArgs = (url: string) =>
  ({ request: new Request(url), params: {}, context: {} }) as unknown as Parameters<
    typeof loader
  >[0];

function postRequest(
  fields: Record<string, string>,
  origin = ORIGIN,
): Parameters<typeof action>[0] {
  return {
    request: new Request(`${ORIGIN}/dashboard`, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded", Origin: origin },
      body: new URLSearchParams({
        idempotencyKey: IDEMPOTENCY_KEY,
        ...fields,
      }).toString(),
    }),
    params: {},
    context: {},
  } as unknown as Parameters<typeof action>[0];
}

const saveRequest = (layout: string, origin = ORIGIN) => postRequest({ layout }, origin);

function emptyDay(overrides: Partial<PlayerDay> = {}): PlayerDay {
  return {
    dayNumber: 16,
    dayCount: 28,
    battles: [],
    complete: false,
    net: null,
    attacks: null,
    defenses: null,
    lastResetRank: null,
    trophies: 5696,
    observedAtMs: null,
    battlesObservedAtMs: null,
    openDefenses: null,
    autoDefenseEach: null,
    ...overrides,
  };
}

function fight(
  kind: ClockBattle["kind"],
  hour: number,
  minute: number,
  stars: number,
  trophyChange: number,
): ClockBattle {
  return {
    at: Date.UTC(2026, 9, 7, hour, minute),
    kind,
    stars,
    destruction: stars === 3 ? 100 : 80,
    trophyChange,
    opponent: "Opponent",
  };
}

/** Render inside a data router, as the page does, for links and saves. */
async function renderInRouter(element: ReturnType<typeof createElement>) {
  const handler = createStaticHandler([{ path: "/", Component: () => element }]);
  const context = await handler.query(new Request(`${ORIGIN}/`));
  if (context instanceof Response) throw new Error("unexpected response");
  return renderToString(
    createElement(StaticRouterProvider, {
      router: createStaticRouter(handler.dataRoutes, context),
      context,
      hydrate: false,
    }),
  );
}

const text = (html: string) => html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");

describe("dashboard layout", () => {
  it("falls back to the default layout when nothing is saved", () => {
    expect(readSavedLayout(undefined)).toEqual(defaultLayout());
    expect(readSavedLayout({ v: 3, today: [] })).toEqual(defaultLayout());
    expect(defaultLayout().tabs.today.map((card) => card.card)).toEqual([
      "legendday",
      "clock",
      "opponents",
      "cutoffs",
      "shield",
      "around",
      "ghost",
    ]);
  });

  it("round-trips a saved layout with pins and a time zone", () => {
    const layout = defaultLayout();
    layout.timeZone = "Asia/Tokyo";
    layout.tabs.today = [
      { card: "clock", player: null },
      { card: "clock", player: MAIN },
    ];
    layout.tabs.crew = [];
    const stored = serializeLayout(layout);
    expect(stored.today).toEqual([["clock"], ["clock", MAIN]]);
    expect(readSavedLayout(stored)).toEqual(layout);
    expect(parsePostedLayout(JSON.parse(JSON.stringify(stored)))).toEqual(layout);
  });

  it("reads a version 1 layout without its sizes and keeps cards on their own tab", () => {
    const layout = readSavedLayout({
      v: 1,
      tz: "Not/AZone",
      today: [["retired-card", "l"], ["shield", "xl"], ["clock", "s", MAIN], "junk"],
      season: [["invite", "s"]],
    });
    expect(layout.timeZone).toBe("auto");
    expect(layout.tabs.today).toEqual([
      { card: "shield", player: null },
      { card: "clock", player: MAIN },
    ]);
    expect(layout.tabs.season).toEqual([]);
  });

  it("rejects a posted layout the page would never send", () => {
    const valid = serializeLayout(defaultLayout());
    const posted = (changes: Record<string, unknown>) =>
      parsePostedLayout({ ...valid, ...changes });
    expect(posted({})).not.toBeNull();
    expect(posted({ v: 1 })).toBeNull();
    expect(posted({ today: [["nope"]] })).toBeNull();
    expect(posted({ today: [["shield", "s"]] })).toBeNull();
    expect(posted({ crew: [["invite", MAIN]] })).toBeNull();
    expect(posted({ season: [["clock"]] })).toBeNull();
    expect(posted({ today: [["clock", "#lq2v8pj0"]] })).toBeNull();
    expect(posted({ tz: "Not/AZone" })).toBeNull();
    expect(posted({ season: undefined })).toBeNull();
    expect(
      posted({ today: Array.from({ length: MAX_CARDS_PER_TAB + 1 }, () => ["clock"]) }),
    ).toBeNull();
  });

  it("finds the next 05:00 UTC Reset", () => {
    expect(nextResetMs(Date.UTC(2026, 9, 7, 1, 48))).toBe(Date.UTC(2026, 9, 7, 5));
    expect(nextResetMs(Date.UTC(2026, 9, 7, 5))).toBe(Date.UTC(2026, 9, 8, 5));
    expect(nextResetMs(Date.UTC(2026, 9, 7, 23, 59))).toBe(Date.UTC(2026, 9, 8, 5));
  });

  it("rates a base within 15 points of today's Legends held share as Average", () => {
    const legends = { held: 50, defenses: 100 };
    const defenses = (held: number, total: number) =>
      Array.from({ length: total }, (_, index) => ({
        stars: index < held ? 1 : 3,
        yours: false,
      }));
    // 50% Legends average: 13 of 20 held is +15, 14 of 20 is +20.
    expect(baseStrength(defenses(13, 20), legends)).toBe("average");
    expect(baseStrength(defenses(14, 20), legends)).toBe("hard");
    expect(baseStrength(defenses(7, 20), legends)).toBe("average");
    expect(baseStrength(defenses(6, 20), legends)).toBe("easy");
    // Exactly 15 points either side of 49 of 60 held.
    expect(baseStrength(defenses(2, 3), { held: 49, defenses: 60 })).toBe("average");
    expect(baseStrength(defenses(29, 30), { held: 49, defenses: 60 })).toBe("average");
    expect(baseStrength(defenses(2, 2), legends)).toBe("early");
    expect(baseStrength(defenses(3, 3), null)).toBe("early");
  });
});

describe("Legend clock", () => {
  const dayStart = Date.UTC(2026, 9, 7, 5);

  it("merges battles within 20 minutes of each other into one mark", () => {
    const marks = clockMarks(
      [
        fight("attack", 7, 10, 3, 40),
        fight("attack", 7, 25, 3, 40),
        fight("attack", 7, 44, 2, 31),
        fight("attack", 12, 6, 2, 29),
        fight("defense", 7, 30, 3, -40),
        fight("defense", 4, 0, 1, -16),
      ],
      dayStart,
    );
    expect(marks.map((mark) => [mark.kind, mark.battles.length])).toEqual([
      ["attack", 3],
      ["attack", 1],
      ["defense", 1],
    ]);
  });

  it("names the upcoming Reset in local time across a clock change", () => {
    const html = renderToStaticMarkup(
      createElement(LegendClock, {
        battles: [],
        nowMs: Date.UTC(2026, 9, 25, 2),
        timeZone: "Europe/London",
      }),
    );
    expect(html).toContain("to Reset at 05:00");
    expect(text(html)).toContain("RESET 05:00");
    expect(text(html)).toContain("3h 00m");
    expect(text(html)).not.toContain("now ");
  });
});

describe("Legend day card", () => {
  const render = (day: PlayerDay | null, range = { best: 95, worst: 760 }) =>
    text(
      renderToStaticMarkup(
        createElement(LegendDayCard, { player: linked(MAIN), day, range }),
      ),
    );

  it("shows trophies, net, the ranks and the next Reset range as an estimate", () => {
    const html = render(
      emptyDay({
        trophies: 5702,
        complete: true,
        net: 66,
        attacks: 2,
        defenses: 1,
        lastResetRank: 331,
        openDefenses: 7,
        autoDefenseEach: 21,
        battles: [
          fight("attack", 7, 0, 3, 40),
          fight("attack", 8, 0, 2, 31),
          fight("defense", 9, 0, 1, -5),
        ],
      }),
    );
    expect(html).toContain("Trophies 5,702");
    expect(html).toContain("Net today +66");
    expect(html).toContain("Last Reset #331");
    expect(html).toContain("Now #318");
    expect(html).toContain("Next Reset #95 – #760 estimate · narrows as the day goes on");
    expect(html).toContain("+71 2 attacks");
    expect(html).toContain("−5 1 defense");
    expect(html).toContain("7 defenses remain · auto defense −21 each");
  });

  it("leaves totals and the automatic loss out until they are known", () => {
    const html = render(
      emptyDay({ attacks: 3, defenses: 2, openDefenses: 6, battles: [] }),
    );
    expect(html).toContain("– 3 attacks");
    expect(html).toContain("6 defenses remain");
    expect(html).not.toContain("auto defense");
    expect(render(null)).toContain("Trophies 5,696 Net today –");
  });
});

describe("Bases you attacked", () => {
  const row = (
    name: string,
    defenses: { stars: number; yours: boolean }[],
  ): OpponentRow => ({
    tag: `#${name.length}PP`,
    name,
    resetTrophies: 5712,
    hit: {
      stars: 3,
      destruction: 100,
      trophyChange: 40,
      at: Date.UTC(2026, 9, 7, 7, 20),
    },
    defenses,
    observedAtMs: null,
  });
  const held = (count: number, total: number) =>
    Array.from({ length: total }, (_, index) => ({
      stars: index < count ? 2 : 3,
      yours: index === total - 1,
    }));

  it("lists hard bases first and colours them for the attacker", async () => {
    const html = await renderInRouter(
      createElement(OpponentsCard, {
        rows: [
          row("Easy one", held(1, 4)),
          row("Early", held(0, 2)),
          row("Hard one", held(5, 6)),
        ],
        legends: { held: 52, defenses: 100 },
        timeZone: "UTC",
      }),
    );
    const plain = text(html);
    expect(plain.indexOf("Hard one")).toBeLessThan(plain.indexOf("Easy one"));
    expect(plain.indexOf("Easy one")).toBeLessThan(plain.indexOf("Early"));
    expect(plain).toContain("Hard held 5 of 6 · 83%");
    expect(plain).toContain("Easy held 1 of 4 · 25%");
    expect(plain).toContain("Too early held 0 of 2 · needs 3+ defenses");
    expect(plain).not.toContain("not yet");
    // Each opponent can be saved into one of the account's groups.
    expect(html).toContain('href="/account/groups/add/8PP"');
    expect(html.match(/is-yours/g)).toHaveLength(3);
  });
});

describe("dashboard route", () => {
  let account: {
    username: string;
    displayName: string;
    preferences: Record<string, unknown>;
  };
  let client: {
    getAccount: ReturnType<typeof vi.fn>;
    updateAccount: ReturnType<typeof vi.fn>;
    getPublicUser: ReturnType<typeof vi.fn>;
    getPlayer: ReturnType<typeof vi.fn>;
  };

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllEnvs();
  });

  beforeEach(() => {
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
    mocks.requireLogin.mockResolvedValue(IDENTITY);
    mocks.readLoginIdentity.mockResolvedValue(IDENTITY);
    mocks.requestJson.mockReset();
    mocks.requestJson.mockRejectedValue(
      new PythonApiError(503, { error: "unavailable" }),
    );
    account = { username: "nova88", displayName: "Nova", preferences: { other: 1 } };
    client = {
      getAccount: vi.fn(async () => ({ ...account, providers: ["google"] })),
      updateAccount: vi.fn(async () => ({ ...account, providers: ["google"] })),
      getPublicUser: vi.fn(async () => ({
        username: "nova88",
        displayName: "Nova",
        verifiedPlayers: [
          linked(ALT, { name: "Lens Alt", state: "not_in_legend", trophies: null }),
          linked(MAIN),
        ],
      })),
      getPlayer: vi.fn(async () => {
        throw new PythonApiError(503, { error: "unavailable" });
      }),
    };
    mocks.createPythonClient.mockReturnValue(client);
  });

  const page = (
    currentDay: Record<string, unknown>,
    extra: Record<string, unknown> = {},
  ) => ({
    season: { currentDayNumber: 3, dayCount: 28 },
    profile: {
      trophies: 5702,
      freshness: { observedAt: "2026-10-07T09:20:00Z" },
      // A later correction to an earlier day.
      battleHistoryUpdatedAt: "2026-10-07T09:28:00Z",
    },
    currentDayPublishedAt: "2026-10-07T09:25:00Z",
    recentDays: [],
    seasonDays: [],
    currentDay: {
      period: "2026-10-07T05:00:00+00:00 – 2026-10-08T05:00:00+00:00",
      battlesComplete: false,
      trophyChange: null,
      offense: { attacks: 0 },
      defense: { defenses: 0 },
      offenseEvents: [],
      defenseEvents: [],
      ...currentDay,
    },
    ...extra,
  });

  it("shows the signed-out preview without a login", async () => {
    mocks.readLoginIdentity.mockResolvedValue(null);
    const { data } = unwrap<DashboardLoaderData>(
      await loader(loaderArgs(`${ORIGIN}/dashboard`)),
    );
    expect(data).toEqual({ kind: "signed-out", loginAvailable: true });
    expect(client.getAccount).not.toHaveBeenCalled();
  });

  it("selects the first Legend player and survives missing player reads", async () => {
    const { data } = unwrap<DashboardLoaderData>(
      await loader(loaderArgs(`${ORIGIN}/dashboard`)),
    );
    if (data.kind !== "signed-in") throw new Error("expected signed in");
    expect(data.selectedTag).toBe(MAIN);
    expect(data.layout).toEqual(defaultLayout());
    expect(data.days).toEqual({});
    expect(data.ranges).toEqual({});
    expect(data.opponents).toEqual({});
    expect(data.legendsHeld).toBeNull();
    expect(client.getPlayer).toHaveBeenCalledWith(MAIN);
  });

  it("reads the day, the last Reset rank, the range and the bases attacked", async () => {
    vi.useFakeTimers({ toFake: ["Date"], now: Date.UTC(2026, 9, 7, 9, 30) });
    client.getPlayer.mockResolvedValue(
      page(
        {
          offense: { attacks: 2 },
          defense: { defenses: 1 },
          offenseEvents: [
            {
              battleTimestamp: "2026-10-07T09:00:00Z",
              stars: 3,
              destructionPercentage: 100,
              trophyChange: 40,
              opponent: { tag: "#8PY", name: "Tundra" },
            },
          ],
        },
        {
          recentDays: [
            {
              period: "2026-10-06T05:00:00+00:00 – 2026-10-07T05:00:00+00:00",
              resetRank: 331,
            },
          ],
        },
      ),
    );
    mocks.requestJson.mockResolvedValue({
      tag: MAIN,
      rank_range: { best: 95, worst: 760 },
      legends_held: { held: 52, defenses: 100 },
      open_defenses: 7,
      automatic_defense_each: 21,
      opponents: [
        {
          tag: "#8PY",
          name: "Tundra",
          reset_trophies: 5688,
          hit: {
            stars: 3,
            destruction_percentage: 100,
            trophy_change: 40,
            battle_timestamp: "2026-10-07T09:00:00Z",
          },
          defenses: [{ stars: 3, yours: true }],
          observed_at: "2026-10-07T09:10:00+00:00",
        },
      ],
    });
    const { data } = unwrap<DashboardLoaderData>(
      await loader(loaderArgs(`${ORIGIN}/dashboard`)),
    );
    if (data.kind !== "signed-in") throw new Error("expected signed in");
    expect(mocks.requestJson).toHaveBeenCalledWith(
      `/v1/players/${encodeURIComponent(MAIN)}/today`,
      "GET",
      undefined,
      undefined,
    );
    expect(data.dayEndsMs).toBe(Date.UTC(2026, 9, 8, 5));
    expect(data.days[MAIN]).toEqual(
      emptyDay({
        dayNumber: 3,
        battles: [
          {
            at: Date.UTC(2026, 9, 7, 9),
            kind: "attack",
            stars: 3,
            destruction: 100,
            trophyChange: 40,
            opponent: "Tundra",
          },
        ],
        attacks: 2,
        defenses: 1,
        lastResetRank: 331,
        trophies: 5702,
        observedAtMs: Date.UTC(2026, 9, 7, 9, 20),
        battlesObservedAtMs: Date.UTC(2026, 9, 7, 9, 25),
        openDefenses: 7,
        autoDefenseEach: 21,
      }),
    );
    expect(data.ranges[MAIN]).toEqual({ best: 95, worst: 760 });
    expect(data.legendsHeld).toEqual({ held: 52, defenses: 100 });
    expect(data.opponents[MAIN]).toEqual([
      {
        tag: "#8PY",
        name: "Tundra",
        resetTrophies: 5688,
        hit: {
          stars: 3,
          destruction: 100,
          trophyChange: 40,
          at: Date.UTC(2026, 9, 7, 9),
        },
        defenses: [{ stars: 3, yours: true }],
        observedAtMs: Date.UTC(2026, 9, 7, 9, 10),
      },
    ]);
  });

  it("keeps the last Reset rank before today's log is published", async () => {
    vi.useFakeTimers({ toFake: ["Date"], now: Date.UTC(2026, 9, 7, 5, 10) });
    client.getPlayer.mockResolvedValue(
      page(
        {},
        {
          currentDay: null,
          recentDays: [
            {
              period: "2026-10-06T05:00:00+00:00 – 2026-10-07T05:00:00+00:00",
              resetRank: 331,
            },
          ],
        },
      ),
    );
    const { data } = unwrap<DashboardLoaderData>(
      await loader(loaderArgs(`${ORIGIN}/dashboard`)),
    );
    if (data.kind !== "signed-in") throw new Error("expected signed in");
    expect(data.days[MAIN]).toMatchObject({ lastResetRank: 331, battles: [] });
  });

  it("drops a today read whose range is impossible", async () => {
    client.getPlayer.mockResolvedValue(page({}));
    mocks.requestJson.mockResolvedValue({
      tag: MAIN,
      rank_range: { best: 9, worst: 3 },
      legends_held: null,
      open_defenses: null,
      automatic_defense_each: null,
      opponents: [],
    });
    const { data } = unwrap<DashboardLoaderData>(
      await loader(loaderArgs(`${ORIGIN}/dashboard`)),
    );
    if (data.kind !== "signed-in") throw new Error("expected signed in");
    expect(data.ranges).toEqual({});
    expect(data.days[MAIN]).toMatchObject({ openDefenses: null, autoDefenseEach: null });
  });

  it("never shows last Season's trophies as live", async () => {
    const pending = page({});
    client.getPlayer.mockResolvedValue({
      ...pending,
      profile: { ...pending.profile, trophies: 6000, seasonResetPending: true },
    });
    const { data } = unwrap<DashboardLoaderData>(
      await loader(loaderArgs(`${ORIGIN}/dashboard`)),
    );
    if (data.kind !== "signed-in") throw new Error("expected signed in");
    expect(data.days[MAIN]).toMatchObject({ trophies: null });
  });

  it("works out a complete live day's gain from its attacks and defenses", async () => {
    const day = {
      battlesComplete: true,
      offense: { attacks: 2, trophyGain: 80 },
      defense: { defenses: 1, trophyLoss: 16 },
    };
    client.getPlayer.mockResolvedValue(page(day));
    const complete = unwrap<DashboardLoaderData>(
      await loader(loaderArgs(`${ORIGIN}/dashboard`)),
    ).data;
    if (complete.kind !== "signed-in") throw new Error("expected signed in");
    expect(complete.days[MAIN]).toMatchObject({ complete: true, net: 64 });
    client.getPlayer.mockResolvedValue(page({ ...day, battlesComplete: false }));
    const partial = unwrap<DashboardLoaderData>(
      await loader(loaderArgs(`${ORIGIN}/dashboard`)),
    ).data;
    if (partial.kind !== "signed-in") throw new Error("expected signed in");
    expect(partial.days[MAIN]).toMatchObject({ complete: false, net: null });
  });

  it("ends the day at the Reset after the first read starts, even if it lands after", async () => {
    vi.useFakeTimers({ toFake: ["Date"], now: Date.UTC(2026, 9, 7, 4, 59, 59) });
    client.getPublicUser.mockImplementation(async () => {
      vi.setSystemTime(Date.UTC(2026, 9, 7, 5, 0, 1));
      return { username: "nova88", displayName: "Nova", verifiedPlayers: [linked(MAIN)] };
    });
    const { data } = unwrap<DashboardLoaderData>(
      await loader(loaderArgs(`${ORIGIN}/dashboard`)),
    );
    if (data.kind !== "signed-in") throw new Error("expected signed in");
    expect(data.dayEndsMs).toBe(Date.UTC(2026, 9, 7, 5));
  });

  it("follows the switcher's player and reads the saved layout", async () => {
    account.preferences = { dashboard: { v: 2, tz: "auto", today: [["shield"]] } };
    const { data } = unwrap<DashboardLoaderData>(
      await loader(loaderArgs(`${ORIGIN}/dashboard?player=%232pp8qly0`)),
    );
    if (data.kind !== "signed-in") throw new Error("expected signed in");
    expect(data.selectedTag).toBe(ALT);
    expect(data.layout.tabs.today).toEqual([{ card: "shield", player: null }]);
    expect(client.getPlayer).not.toHaveBeenCalled();
  });

  it("sends an account without a profile to setup", async () => {
    client.getAccount.mockRejectedValue(
      new PythonApiError(404, { error: "account_not_found" }),
    );
    await expect(loader(loaderArgs(`${ORIGIN}/dashboard`))).rejects.toSatisfy(
      (thrown: unknown) =>
        thrown instanceof Response &&
        thrown.headers.get("Location") === "/account/setup?returnPath=/dashboard",
    );
  });

  it("saves the layout and keeps every other preference", async () => {
    const layout = defaultLayout();
    layout.tabs.today = [{ card: "clock", player: MAIN }];
    const { data, status } = unwrap<DashboardActionData>(
      await action(saveRequest(JSON.stringify(serializeLayout(layout)))),
    );
    expect(status).toBe(200);
    expect(data.saved).toBe(true);
    expect(client.updateAccount).toHaveBeenCalledWith(
      {
        username: "nova88",
        displayName: "Nova",
        preferences: { other: 1, dashboard: serializeLayout(layout) },
      },
      IDEMPOTENCY_KEY,
    );
  });

  it("refuses a bad layout, another site, or a layout too big to store", async () => {
    const bad = unwrap<DashboardActionData>(await action(saveRequest('{"v":2}')));
    expect(bad.status).toBe(400);
    const crossSite = unwrap<DashboardActionData>(
      await action(
        saveRequest(
          JSON.stringify(serializeLayout(defaultLayout())),
          "https://evil.example",
        ),
      ),
    );
    expect(crossSite.status).toBe(403);
    account.preferences = { notes: "x".repeat(3900) };
    const tooBig = unwrap<DashboardActionData>(
      await action(saveRequest(JSON.stringify(serializeLayout(defaultLayout())))),
    );
    expect(tooBig.status).toBe(413);
    expect(client.updateAccount).not.toHaveBeenCalled();
  });

  it("reports a failed save without losing the page", async () => {
    client.updateAccount.mockRejectedValue(
      new PythonApiError(503, { error: "unavailable" }),
    );
    const { data, status } = unwrap<DashboardActionData>(
      await action(saveRequest(JSON.stringify(serializeLayout(defaultLayout())))),
    );
    expect(status).toBe(503);
    expect(data).toMatchObject({ saved: false, error: expect.any(String) });
  });
});

describe("dashboard switch", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  async function renderNavigation() {
    const handler = createStaticHandler([
      {
        id: "root",
        path: "/",
        loader: rootLoader,
        Component: App,
        children: [{ index: true, Component: () => null }],
      },
    ]);
    const context = await handler.query(new Request(`${ORIGIN}/`));
    if (context instanceof Response) throw new Error("unexpected response");
    return renderToString(
      createElement(StaticRouterProvider, {
        router: createStaticRouter(handler.dataRoutes, context),
        context,
        hydrate: false,
      }),
    );
  }

  it("hides the page, its saves and the nav link while it is off", async () => {
    vi.stubEnv("CLASHLENS_DASHBOARD_ENABLED", "");
    for (const run of [
      () => loader(loaderArgs(`${ORIGIN}/dashboard`)),
      () => action(saveRequest(JSON.stringify(serializeLayout(defaultLayout())))),
    ]) {
      const thrown = await run().then(
        () => null,
        (error: unknown) => error,
      );
      expect(unwrap(thrown).status).toBe(404);
    }
    expect(await renderNavigation()).not.toContain('href="/dashboard"');
  });

  it("shows the nav link while it is on", async () => {
    vi.stubEnv("CLASHLENS_DASHBOARD_ENABLED", "true");
    expect(await renderNavigation()).toContain('href="/dashboard"');
  });
});

describe("dashboard page", () => {
  const CHECKING = "#9YJ2C0QL";

  async function renderDashboard(
    loaderData: Partial<Extract<DashboardLoaderData, { kind: "signed-in" }>>,
    search = "",
  ) {
    const full: DashboardLoaderData = {
      kind: "signed-in",
      players: [],
      playersUnavailable: false,
      selectedTag: null,
      layout: defaultLayout(),
      days: {},
      ranges: {},
      opponents: {},
      legendsHeld: null,
      dayEndsMs: Date.UTC(2099, 0, 1, 5),
      idempotencyKey: IDEMPOTENCY_KEY,
      ...loaderData,
    };
    const handler = createStaticHandler([
      { path: "/dashboard", Component: DashboardRoute, loader: () => full },
    ]);
    const context = await handler.query(new Request(`${ORIGIN}/dashboard${search}`));
    if (context instanceof Response) throw new Error("unexpected response");
    return renderToString(
      createElement(StaticRouterProvider, {
        router: createStaticRouter(handler.dataRoutes, context),
        context,
        hydrate: false,
      }),
    ).replaceAll("<!-- -->", "");
  }

  it("says each player's status once and shows live numbers once", async () => {
    const layout = defaultLayout();
    layout.tabs.today = [
      { card: "legendday", player: null },
      { card: "shield", player: ALT },
      { card: "around", player: ALT },
      { card: "ghost", player: CHECKING },
    ];
    const html = await renderDashboard({
      players: [
        linked(ALT, { name: "Lens Alt", state: "not_in_legend", trophies: null }),
        linked(MAIN),
        linked(CHECKING, { name: "Lens New", state: "checking", rank: null }),
      ],
      selectedTag: MAIN,
      layout,
      days: { [MAIN]: emptyDay() },
    });
    expect(html.match(/not in Legends/gi)).toHaveLength(1);
    expect(html).toContain("Lens Alt is not in Legends");
    expect(html).toContain("Lens New</b> Checking this tag with Clash of Clans.");
    expect(html.match(/5,696/g)).toHaveLength(1);
    expect(html.match(/#318/g)).toHaveLength(1);
    expect(html).not.toContain('data-card="shield"');
    expect(html).not.toContain('data-card="ghost"');
    expect(html).not.toMatch(/follows switcher/i);
    expect(html).not.toMatch(/\bLIVE\b/);
  });

  it("gives every card its one size on the 3-column grid", async () => {
    const html = await renderDashboard({
      players: [linked(MAIN)],
      selectedTag: MAIN,
      days: { [MAIN]: emptyDay() },
    });
    expect(html).toMatch(/dash-card dash-card-m" aria-label="Legend day"/);
    expect(html).toMatch(/dash-card dash-card-s" aria-label="Legend clock"/);
    expect(html).toMatch(/dash-card dash-card-l" aria-label="Bases you attacked"/);
    // The Legend day heading and the switcher name the player in full on hover.
    expect(html.match(/title="Lens Main"/g)).toHaveLength(2);
  });

  it("shows only the linking prompt on every tab when no player is linked", async () => {
    for (const tab of ["today", "season", "crew"]) {
      const html = await renderDashboard({}, `?tab=${tab}`);
      expect(html).toContain("Link your Clash player");
      expect(html).not.toContain("data-card=");
    }
  });
});
