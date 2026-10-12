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
import type {
  ClockBattle,
  DashboardSettings,
  OpponentRow,
  PlayerDay,
} from "../../app/lib/dashboard";
import {
  BASE_TROPHY_BAND,
  DEFAULT_LAYOUT,
  LEGENDS_DEFENSE_LOSS,
  addableCards,
  baseStrength,
  defaultSettings,
  nextResetMs,
  parsePostedSettings,
  readSettings,
  serializeSettings,
  shownCards,
} from "../../app/lib/dashboard";
import DashboardRoute, {
  action,
  loader,
  shouldRevalidate,
  type DashboardActionData,
  type DashboardLoaderData,
} from "../../app/routes/dashboard";
import { loadWebsiteConfig } from "../../app/server/config.server";
import { getPlayerToday } from "../../app/services/dashboard.server";
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

const saveRequest = (settings: string, origin = ORIGIN) =>
  postRequest({ settings }, origin);
const GROUP = "0f6c1d8e-7b2a-4c3d-9e5f-1a2b3c4d5e6f";
const DEFAULT_SAVE = JSON.stringify({ v: 3, layout: "default" });

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

describe("dashboard settings", () => {
  it("starts on the first-visit choice with the owner's defaults", () => {
    for (const saved of [undefined, null, "x", { v: 2, today: [["shield"]] }, { v: 3 }]) {
      expect(readSettings(saved)).toEqual(defaultSettings());
    }
    expect(defaultSettings()).toEqual({
      layout: null,
      goal: 200,
      cutoffs: [10, 50, 100, 200, 500, 1000, 5000],
      group: null,
      groupWindow: "7",
      basesMode: "held",
    });
    expect(DEFAULT_LAYOUT).toEqual([
      "legend-day",
      "legend-clock",
      "bases",
      "goal",
      "armies-defense",
      "rivals-watch",
      "heat-map",
    ]);
    expect(shownCards(null)).toEqual(DEFAULT_LAYOUT);
    expect(shownCards("default")).toEqual(DEFAULT_LAYOUT);
    expect(addableCards(shownCards("default"))).toEqual([
      "cutoffs",
      "season-graph",
      "recent-form",
      "offense-consistency",
      "group-watch",
      "armies-offense",
    ]);
  });

  it("stores only what differs from the defaults and reads it back", () => {
    const settings: DashboardSettings = {
      layout: ["cutoffs", "goal"],
      goal: 10_000,
      cutoffs: [10, 20, 50, 100, 200, 300, 400],
      group: GROUP,
      groupWindow: "season",
      basesMode: "trophies",
    };
    const stored = serializeSettings(settings);
    expect(stored).toEqual({
      v: 3,
      layout: ["cutoffs", "goal"],
      goal: 10_000,
      cutoffs: [10, 20, 50, 100, 200, 300, 400],
      group: GROUP,
      window: "season",
      bases: "trophies",
    });
    expect(serializeSettings({ ...defaultSettings(), layout: "default" })).toEqual({
      v: 3,
      layout: "default",
    });
    expect(readSettings(stored)).toEqual(settings);
    expect(parsePostedSettings(JSON.parse(JSON.stringify(stored)))).toEqual(settings);
    // Every card and setting together stays far under the 4,096-byte box.
    const everything = JSON.stringify(
      serializeSettings({ ...settings, layout: addableCards([]) }),
    );
    expect(new TextEncoder().encode(everything).byteLength).toBeLessThan(400);
  });

  it("drops saved cards that are unknown or repeated", () => {
    expect(
      readSettings({
        v: 3,
        layout: ["goal", "retired", "goal", "bases", "cutoffs"],
        goal: 0,
        cutoffs: [10],
        group: "nope",
        window: "x",
        bases: "x",
      }),
    ).toEqual({ ...defaultSettings(), layout: ["goal", "bases", "cutoffs"] });
    // A saved Goal past 10,000 falls back to the default.
    expect(readSettings({ v: 3, layout: "default", goal: 10_001 }).goal).toBe(
      defaultSettings().goal,
    );
    // An empty own layout stays empty; it is not a first visit.
    expect(readSettings({ v: 3, layout: [] }).layout).toEqual([]);
  });

  it("refuses posted settings the page would never send", () => {
    const posted = (changes: Record<string, unknown>) =>
      parsePostedSettings({ v: 3, layout: "default", ...changes });
    expect(posted({})).not.toBeNull();
    expect(posted({ goal: 1 })?.goal).toBe(1);
    expect(posted({ goal: 10_000 })?.goal).toBe(10_000);
    expect(posted({ layout: [] })?.layout).toEqual([]);
    for (const bad of [
      { v: 2 },
      { layout: null },
      { layout: ["goal", "goal"] },
      { layout: ["nope"] },
      { goal: 0 },
      { goal: 10_001 },
      { goal: 2.5 },
      { goal: "200" },
      { cutoffs: [10, 50, 100, 200, 500, 1000] },
      { cutoffs: [50, 10, 100, 200, 500, 1000, 5000] },
      { cutoffs: [10, 50, 100, 200, 500, 1000, 7000] },
      { group: "not-a-group" },
      { window: "7" },
      { bases: "held" },
      { bases: "rank" },
      { extra: true },
    ]) {
      expect(posted(bad)).toBeNull();
    }
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
        trophyChange: -30,
        yours: false,
      }));
    // 50% Legends average: 13 of 20 held is +15, 14 of 20 is +20.
    expect(baseStrength(defenses(13, 20), legends)).toBe("average");
    expect(baseStrength(defenses(14, 20), legends)).toBe("hard");
    expect(baseStrength(defenses(7, 20), legends)).toBe("average");
    expect(baseStrength(defenses(6, 20), legends)).toBe("easy");
    // A 30% base is Easy against 52% but Average against 30% at its trophies.
    expect(baseStrength(defenses(6, 20), { held: 52, defenses: 100 })).toBe("easy");
    expect(baseStrength(defenses(6, 20), { held: 30, defenses: 100 })).toBe("average");
    expect(baseStrength(defenses(2, 2), legends)).toBe("early");
    expect(baseStrength(defenses(3, 3), null)).toBe("early");
  });

  it("rates a base by trophies given up a defense against the fixed Legends 32", () => {
    const gaveUp = (each: number) =>
      [0, 1, 2].map(() => ({ stars: 2, trophyChange: -each, yours: false }));
    const band = BASE_TROPHY_BAND;
    expect(LEGENDS_DEFENSE_LOSS).toBe(32);
    // Today's Legends held share plays no part, read or not.
    for (const legends of [{ held: 50, defenses: 100 }, null]) {
      expect(baseStrength(gaveUp(32 - band), legends, "trophies")).toBe("average");
      expect(baseStrength(gaveUp(32 - band - 1), legends, "trophies")).toBe("hard");
      expect(baseStrength(gaveUp(32 + band), legends, "trophies")).toBe("average");
      expect(baseStrength(gaveUp(32 + band + 1), legends, "trophies")).toBe("easy");
      expect(baseStrength(gaveUp(40).slice(0, 2), legends, "trophies")).toBe("early");
    }
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

  it("shows – for attack and defense counts it does not know, not 0", () => {
    // Today's battles not saved yet, or the player page not read at all.
    for (const day of [emptyDay({ lastResetRank: 331 }), null]) {
      const html = render(day);
      expect(html).toContain("– – attacks");
      expect(html).toContain("– – defenses");
      expect(html).not.toContain("0 attacks");
      expect(html).toContain("Trophies 5,696");
      expect(html).toContain("Now #318");
      expect(html).toContain("Next Reset #95 – #760");
    }
  });
});

describe("Bases you attacked", () => {
  const row = (name: string, defenses: OpponentRow["defenses"]): OpponentRow => ({
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
    // Legends at similar trophies today: 41% held.
    legends: { held: 41, defenses: 100 },
  });
  const held = (count: number, total: number) =>
    Array.from({ length: total }, (_, index) => ({
      stars: index < count ? 2 : 3,
      trophyChange: index < count ? -25 : -40,
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
        timeZone: "UTC",
      }),
    );
    const plain = text(html);
    expect(plain).toContain("Base strength · vs Legends at similar trophies today");
    expect(plain.indexOf("Hard one")).toBeLessThan(plain.indexOf("Easy one"));
    expect(plain.indexOf("Easy one")).toBeLessThan(plain.indexOf("Early"));
    expect(plain).toContain("Hard held 5 of 6 · 83% vs 41%");
    expect(plain).toContain("Easy held 1 of 4 · 25% vs 41%");
    expect(plain).toContain("Too early held 0 of 2 · needs 3+ defenses");
    expect(plain).not.toContain("not yet");
    // Each opponent can be saved into one of the account's groups.
    expect(html).toContain('href="/account/groups/add/8PP"');
    expect(html.match(/is-yours/g)).toHaveLength(3);
  });

  it("says today's battles aren't saved yet rather than no attacks", async () => {
    const render = async (rows: OpponentRow[] | null) =>
      text(
        await renderInRouter(createElement(OpponentsCard, { rows, timeZone: "UTC" })),
      ).replace(/&#x27;/g, "'");
    const unsaved = await render(null);
    expect(unsaved).toContain(
      "Bases you attacked · today Today's battles aren't saved yet.",
    );
    expect(unsaved).not.toContain("of 8");
    expect(unsaved).not.toContain("No attacks yet today");
    expect(await render([])).toContain("· today · 0 of 8 No attacks yet today.");
  });

  it("rates bases by trophies given up a defense in Trophies mode", async () => {
    const plain = text(
      await renderInRouter(
        createElement(OpponentsCard, {
          rows: [row("Easy one", held(1, 4)), row("Hard one", held(5, 6))],
          timeZone: "UTC",
          mode: "trophies",
        }),
      ),
    );
    expect(plain).toContain("Base strength · Legends −32 a defense");
    // 5 held at −25 and one tripled at −40: 165 over 6 is −28 a defense.
    expect(plain).toContain("Hard gave up −28 a defense");
    // One held and three tripled: 145 over 4 is −36 a defense.
    expect(plain).toContain("Easy gave up −36 a defense");
    expect(plain.indexOf("Hard one")).toBeLessThan(plain.indexOf("Easy one"));
  });
});

describe("today read for the Legend day and Bases cards", () => {
  beforeEach(() => {
    mocks.requestJson.mockReset();
  });

  it("reads the Next Reset range and the bases attacked with their Legends held", async () => {
    mocks.requestJson.mockResolvedValue({
      tag: MAIN,
      rank_range: { best: 95, worst: 760 },
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
          defenses: [{ stars: 3, trophy_change: -40, yours: true }],
          observed_at: "2026-10-07T09:10:00+00:00",
          legends_held: { held: 41, defenses: 100 },
        },
      ],
    });
    expect(await getPlayerToday(MAIN)).toEqual({
      rankRange: { best: 95, worst: 760 },
      openDefenses: 7,
      automaticDefenseEach: 21,
      opponents: [
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
          defenses: [{ stars: 3, trophyChange: -40, yours: true }],
          observedAtMs: Date.UTC(2026, 9, 7, 9, 10),
          legends: { held: 41, defenses: 100 },
        },
      ],
    });
    expect(mocks.requestJson).toHaveBeenCalledWith(
      `/v1/players/${encodeURIComponent(MAIN)}/today`,
      "GET",
      undefined,
      undefined,
    );
  });

  it("keeps a today read without saved battles apart from one with no attacks", async () => {
    mocks.requestJson.mockResolvedValue({
      tag: MAIN,
      rank_range: { best: 95, worst: 760 },
      open_defenses: null,
      automatic_defense_each: null,
      opponents: null,
    });
    expect(await getPlayerToday(MAIN)).toMatchObject({
      rankRange: { best: 95, worst: 760 },
      opponents: null,
    });
  });

  it("refuses a today read whose range is impossible", async () => {
    mocks.requestJson.mockResolvedValue({
      tag: MAIN,
      rank_range: { best: 9, worst: 3 },
      open_defenses: null,
      automatic_defense_each: null,
      opponents: [],
    });
    await expect(getPlayerToday(MAIN)).rejects.toMatchObject({ status: 502 });
  });
});

describe("dashboard route", () => {
  let account: {
    username: string;
    displayName: string;
    preferences: Record<string, unknown>;
  };
  let client: Record<
    | "getAccount"
    | "updateAccount"
    | "getPublicUser"
    | "getPlayer"
    | "getPlayerSeasons"
    | "getTrackedLeaderboard"
    | "listGroups",
    ReturnType<typeof vi.fn>
  >;

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
    account = { username: "nova88", displayName: "Nova", preferences: { other: 1 } };
    const unavailable = async () => {
      throw new PythonApiError(503, { error: "unavailable" });
    };
    mocks.requestJson.mockReset();
    mocks.requestJson.mockImplementation(unavailable);
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
      getPlayer: vi.fn(unavailable),
      getPlayerSeasons: vi.fn(unavailable),
      getTrackedLeaderboard: vi.fn(unavailable),
      listGroups: vi.fn(unavailable),
    };
    mocks.createPythonClient.mockReturnValue(client);
  });

  const load = async (url = `${ORIGIN}/dashboard`) => {
    const { data } = unwrap<DashboardLoaderData>(await loader(loaderArgs(url)));
    if (data.kind !== "signed-in") throw new Error("expected signed in");
    return data;
  };

  it("shows the signed-out preview without a login", async () => {
    mocks.readLoginIdentity.mockResolvedValue(null);
    const { data } = unwrap<DashboardLoaderData>(
      await loader(loaderArgs(`${ORIGIN}/dashboard`)),
    );
    expect(data).toEqual({ kind: "signed-out", loginAvailable: true });
    expect(client.getAccount).not.toHaveBeenCalled();
  });

  it("selects the first Legend player and still answers when every card read fails", async () => {
    const data = await load();
    expect(data.selectedTag).toBe(MAIN);
    expect(data.settings).toEqual(defaultSettings());
    // A first visit reads the default cards that sit behind the pop-up.
    // Legend day still has the account's live trophies and rank.
    expect(data.cards).toEqual({
      "legend-day": { player: linked(MAIN), day: null, range: null },
      "legend-clock": null,
      bases: null,
      goal: null,
      "armies-defense": null,
      "rivals-watch": null,
      "heat-map": null,
    });
    expect(client.getPlayer).toHaveBeenCalledWith(MAIN);
  });

  it("follows the switcher's player and reads no cards outside Legend I", async () => {
    account.preferences = { dashboard: { v: 3, layout: ["goal"], goal: 50 } };
    const data = await load(`${ORIGIN}/dashboard?player=%232pp8qly0`);
    expect(data.selectedTag).toBe(ALT);
    expect(data.settings).toMatchObject({ layout: ["goal"], goal: 50 });
    expect(data.cards).toEqual({});
    expect(client.getPlayer).not.toHaveBeenCalled();
    expect(client.getTrackedLeaderboard).not.toHaveBeenCalled();
  });

  it("reads only the cards on the dashboard", async () => {
    account.preferences = { dashboard: { v: 3, layout: ["heat-map"] } };
    const data = await load();
    expect(Object.keys(data.cards)).toEqual(["heat-map"]);
    expect(client.getPlayer).toHaveBeenCalledTimes(1);
    expect(client.getTrackedLeaderboard).not.toHaveBeenCalled();
    expect(client.listGroups).not.toHaveBeenCalled();
  });

  it("ends the day at the Reset after the first read starts, even if it lands after", async () => {
    vi.useFakeTimers({ toFake: ["Date"], now: Date.UTC(2026, 9, 7, 4, 59, 59) });
    client.getPublicUser.mockImplementation(async () => {
      vi.setSystemTime(Date.UTC(2026, 9, 7, 5, 0, 1));
      return { username: "nova88", displayName: "Nova", verifiedPlayers: [linked(MAIN)] };
    });
    expect((await load()).dayEndsMs).toBe(Date.UTC(2026, 9, 7, 5));
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

  it("saves the settings and keeps every other preference", async () => {
    const settings = { v: 3, layout: ["goal", "cutoffs"], goal: 75 };
    const { data, status } = unwrap<DashboardActionData>(
      await action(saveRequest(JSON.stringify(settings))),
    );
    expect(status).toBe(200);
    expect(data.saved).toBe(true);
    expect(client.updateAccount).toHaveBeenCalledWith(
      {
        username: "nova88",
        displayName: "Nova",
        preferences: { other: 1, dashboard: settings },
      },
      IDEMPOTENCY_KEY,
    );
  });

  it("saves the default choice so later default changes reach the user", async () => {
    account.preferences = { dashboard: { v: 3, layout: ["goal"], goal: 75 } };
    await action(saveRequest(JSON.stringify({ v: 3, layout: "default", goal: 75 })));
    expect(client.updateAccount.mock.calls[0]?.[0].preferences.dashboard).toEqual({
      v: 3,
      layout: "default",
      goal: 75,
    });
  });

  it("saves a card setting made before any layout was picked with the default layout", async () => {
    const posted = serializeSettings({ ...defaultSettings(), goal: 50 });
    const { data, status } = unwrap<DashboardActionData>(
      await action(saveRequest(JSON.stringify(posted))),
    );
    expect(status).toBe(200);
    expect(data.saved).toBe(true);
    expect(client.updateAccount.mock.calls[0]?.[0].preferences.dashboard).toEqual({
      v: 3,
      layout: "default",
      goal: 50,
    });
  });

  it("refuses bad settings, another site, or settings too big to store", async () => {
    for (const bad of [
      '{"v":2}',
      "not json",
      '{"v":3,"layout":"default","goal":0}',
      '{"v":3,"layout":"default","goal":10001}',
    ]) {
      expect(unwrap<DashboardActionData>(await action(saveRequest(bad))).status).toBe(
        400,
      );
    }
    const crossSite = unwrap<DashboardActionData>(
      await action(saveRequest(DEFAULT_SAVE, "https://evil.example")),
    );
    expect(crossSite.status).toBe(403);
    account.preferences = { notes: "x".repeat(4050) };
    const tooBig = unwrap<DashboardActionData>(await action(saveRequest(DEFAULT_SAVE)));
    expect(tooBig.status).toBe(413);
    expect(client.updateAccount).not.toHaveBeenCalled();
  });

  it("reports a failed save without losing the page", async () => {
    client.updateAccount.mockRejectedValue(
      new PythonApiError(503, { error: "unavailable" }),
    );
    const { data, status } = unwrap<DashboardActionData>(
      await action(saveRequest(DEFAULT_SAVE)),
    );
    expect(status).toBe(503);
    expect(data).toMatchObject({ saved: false, error: expect.any(String) });
  });

  it("reloads the cards after a save or for another player, never otherwise", () => {
    const check = (from: string, to: string, formMethod?: string) =>
      shouldRevalidate({
        currentUrl: new URL(`${ORIGIN}${from}`),
        nextUrl: new URL(`${ORIGIN}${to}`),
        formMethod,
        defaultShouldRevalidate: true,
      } as Parameters<typeof shouldRevalidate>[0]);
    expect(check("/dashboard", "/dashboard", "POST")).toBe(true);
    expect(check("/dashboard", "/dashboard?player=%232PP8QLY0")).toBe(true);
    expect(check("/dashboard?player=%232PP8QLY0", "/dashboard?player=%232PP8QLY0")).toBe(
      false,
    );
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
      () => action(saveRequest(DEFAULT_SAVE)),
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

  async function renderPage(loaderData: DashboardLoaderData) {
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

  const renderDashboard = (
    loaderData: Partial<Extract<DashboardLoaderData, { kind: "signed-in" }>>,
  ) =>
    renderPage({
      kind: "signed-in",
      players: [linked(MAIN)],
      playersUnavailable: false,
      selectedTag: MAIN,
      settings: { ...defaultSettings(), layout: "default" },
      cards: {},
      dayEndsMs: Date.UTC(2099, 0, 1, 5),
      idempotencyKey: IDEMPOTENCY_KEY,
      ...loaderData,
    });

  const cardOrder = (html: string) =>
    [...html.matchAll(/class="dash-card dash-card-(\w)"[^>]*data-card="([^"]+)"/g)].map(
      (match) => `${match[2]}:${match[1]}`,
    );

  it("shows each linked account as a tab with its trophies, rank and net today", async () => {
    const html = await renderDashboard({
      players: [
        linked(MAIN),
        linked(ALT, { name: "Lens Alt", state: "not_in_legend", trophies: null }),
        linked(CHECKING, { name: "Lens New", state: "checking", rank: null }),
      ],
    });
    const plain = text(html);
    expect(plain).toContain("Lens Main 5,696 · #318 · +46");
    expect(plain).toContain("Lens Alt Not in Legend I");
    expect(plain).toContain("Lens New Not tracked yet");
    expect(html).toContain(
      `aria-current="page" href="/dashboard?player=${encodeURIComponent(MAIN)}"`,
    );
    expect(html).toContain('href="/account/verify-player"');
    // The player's name shows once, in its tab, cut with "…" and in full on hover.
    expect(html.match(/title="Lens Main"/g)).toHaveLength(1);
  });

  it("puts Customise on the divider above the cards, in the saved order and sizes", async () => {
    const html = await renderDashboard({
      settings: {
        ...defaultSettings(),
        layout: ["heat-map", "rivals-watch", "goal", "group-watch"],
      },
      cards: { goal: null },
    });
    expect(html.indexOf("Customise dashboard")).toBeLessThan(html.indexOf("dash-grid"));
    expect(cardOrder(html)).toEqual([
      "heat-map:s",
      "rivals-watch:m",
      "goal:s",
      "group-watch:m",
    ]);
    expect(text(html)).toContain(
      "Goal This card could not be loaded right now. Reload the page to try again.",
    );
    expect(html).not.toContain("Use our layout");
  });

  it("asks a first-time user to pick a start over the default cards", async () => {
    const html = await renderDashboard({ settings: defaultSettings() });
    const plain = text(html);
    expect(plain).toContain("Your dashboard");
    expect(plain).toContain("Recommended Use our layout");
    expect(plain).toContain("Build my own");
    expect(plain).toContain("Change it any time with");
    expect(cardOrder(html)).toEqual([
      "legend-day:m",
      "legend-clock:s",
      "bases:l",
      "goal:s",
      "armies-defense:m",
      "rivals-watch:m",
      "heat-map:s",
    ]);
  });

  it("offers Add a card on an empty dashboard", async () => {
    const html = await renderDashboard({
      settings: { ...defaultSettings(), layout: [] },
    });
    expect(text(html)).toContain("Your dashboard is empty Add a card");
    expect(html).not.toContain("data-card=");
  });

  it("says the dashboard is Legend I only for an account outside it", async () => {
    const html = await renderDashboard({
      players: [
        linked(ALT, { name: "Lens Alt", state: "not_in_legend", trophies: null }),
      ],
      selectedTag: ALT,
    });
    expect(text(html)).toContain(
      "Unfortunately, the dashboard is Legend I only. Support for lower leagues is coming in the future!",
    );
    expect(html).not.toContain("Customise dashboard");
    expect(html).not.toContain("data-card=");
  });

  it("explains an account still being checked", async () => {
    const html = await renderDashboard({
      players: [linked(CHECKING, { name: "Lens New", state: "checking", rank: null })],
      selectedTag: CHECKING,
    });
    expect(text(html)).toContain("Checking this tag with Clash of Clans.");
    expect(html).not.toContain("data-card=");
  });

  it("shows only the linking prompt when no player is linked", async () => {
    const html = await renderDashboard({ players: [], selectedTag: null });
    expect(html).toContain("Link your Clash player");
    expect(html).not.toContain("Customise dashboard");
    expect(html).not.toContain("data-card=");
  });

  it("previews the default cards behind sign-in", async () => {
    const html = await renderPage({ kind: "signed-out", loginAvailable: true });
    const plain = text(html);
    expect(plain).toContain("Your Legend day, on one page");
    expect(plain).toContain("Continue with Google");
    expect(plain).toContain(
      "Legend day Legend clock Bases you attacked Goal Armies on defense Rivals watch Season heat map",
    );
  });
});
