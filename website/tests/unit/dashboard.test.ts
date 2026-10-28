import { createElement } from "react";
import { renderToStaticMarkup, renderToString } from "react-dom/server";
import {
  createMemoryRouter,
  createStaticHandler,
  createStaticRouter,
  data as dataWithStatus,
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
  addableCards,
  baseStrength,
  defaultSettings,
  nextResetMs,
  parsePostedSettings,
  readSettings,
  serializeSettings,
  shownCards,
} from "../../app/lib/dashboard";
import type { CardDataSet, CardReads } from "../../app/lib/dashboard-cards";
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

/** Each card's data once its read finishes: the page answers before its cards. */
async function settled(reads: CardReads): Promise<CardDataSet> {
  return Object.fromEntries(
    await Promise.all(
      Object.entries(reads).map(async ([card, read]) => [card, await read]),
    ),
  ) as CardDataSet;
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
    request: new Request(`${ORIGIN}/`, {
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
      basesMode: "trophies",
    });
    expect(DEFAULT_LAYOUT).toEqual([
      "legend-day",
      "legend-clock",
      "bases",
      "rivals-watch",
      "goal",
      "armies-defense",
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
      basesMode: "held",
    };
    const stored = serializeSettings(settings);
    expect(stored).toEqual({
      v: 3,
      layout: ["cutoffs", "goal"],
      goal: 10_000,
      cutoffs: [10, 20, 50, 100, 200, 300, 400],
      group: GROUP,
      window: "season",
      bases: "held",
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
    expect(posted({ bases: "held" })?.basesMode).toBe("held");
    // A page left open from before Trophies was the default still saves.
    expect(posted({ bases: "trophies" })?.basesMode).toBe("trophies");
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
    const legends = { held: 50, defenses: 100, lost: 3200 };
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
    expect(baseStrength(defenses(6, 20), { ...legends, held: 52 })).toBe("easy");
    expect(baseStrength(defenses(6, 20), { ...legends, held: 30 })).toBe("average");
    expect(baseStrength(defenses(2, 2), legends)).toBe("early");
    // 3+ defenses without enough Legends at similar trophies: no comparison.
    expect(baseStrength(defenses(3, 3), null)).toBe("unavailable");
  });

  it("rates a base by trophies given up a defense against Legends at similar trophies", () => {
    const gaveUp = (each: number) =>
      [0, 1, 2].map(() => ({ stars: 2, trophyChange: -each, yours: false }));
    const band = BASE_TROPHY_BAND;
    // Legends near the base lost 36 a defense; their held share plays no part.
    for (const held of [0, 100]) {
      const legends = { held, defenses: 100, lost: 3600 };
      expect(baseStrength(gaveUp(36 - band), legends, "trophies")).toBe("average");
      expect(baseStrength(gaveUp(36 - band - 1), legends, "trophies")).toBe("hard");
      expect(baseStrength(gaveUp(36 + band), legends, "trophies")).toBe("average");
      expect(baseStrength(gaveUp(36 + band + 1), legends, "trophies")).toBe("easy");
      expect(baseStrength(gaveUp(40).slice(0, 2), legends, "trophies")).toBe("early");
    }
    // 34 a defense is Hard near players losing 36 and Easy near players losing 32.
    expect(
      baseStrength(gaveUp(34), { held: 0, defenses: 100, lost: 3600 }, "trophies"),
    ).toBe("hard");
    expect(
      baseStrength(gaveUp(34), { held: 0, defenses: 100, lost: 3200 }, "trophies"),
    ).toBe("easy");
    // Too few Legends near it, or a data service that does not send trophies lost.
    expect(baseStrength(gaveUp(34), null, "trophies")).toBe("unavailable");
    expect(
      baseStrength(gaveUp(34), { held: 0, defenses: 100, lost: null }, "trophies"),
    ).toBe("unavailable");
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
    expect(text(html)).toContain("Reset 05:00");
    expect(text(html)).toContain("3h 00m");
    expect(text(html)).not.toContain("now ");
  });
});

describe("Legend day card", () => {
  it("says how old its range is, even without the player page", () => {
    const at = (minutes: number) => Date.UTC(2026, 9, 7, 9, 0) - minutes * 60_000;
    const updated = (day: PlayerDay | null) =>
      text(
        renderToStaticMarkup(
          createElement(LegendDayCard, {
            player: linked(MAIN),
            day,
            range: { best: 95, worst: 760 },
            rangeAtMs: at(9),
            nowMs: at(0),
          }),
        ),
      );
    expect(updated(null)).toContain("updated 9 min ago");
    // The older of the day's reads and the range's shows.
    expect(updated(emptyDay({ observedAtMs: at(2) }))).toContain("updated 9 min ago");
    expect(updated(emptyDay({ observedAtMs: at(12) }))).toContain("updated 12 min ago");
    // 09:00 UTC is 20 hours before the 05:00 Reset; the title's name is for screen readers.
    expect(updated(emptyDay({ dayNumber: 9 }))).toContain(
      "Legend day 9 , Lens Main updated",
    );
    expect(updated(emptyDay({}))).toContain("20h 00m to Reset");
  });

  const render = (
    day: PlayerDay | null,
    range = { best: 95, worst: 760 },
    projected: number | null = null,
  ) =>
    text(
      renderToStaticMarkup(
        createElement(LegendDayCard, { player: linked(MAIN), day, range, projected }),
      ),
    );

  it("shows the projected place beside the outer range, labelled an estimate", () => {
    const html = render(emptyDay({}), { best: 1, worst: 12_270 }, 3000);
    expect(html).toContain("Next Reset #1–#12,270 estimate Projected #3,000 at Reset");
    expect(html).toContain(
      "An estimate from how players in your spot finished on recent days. It can be hundreds of places out while battles are left, and gets closer through the day.",
    );
    // Before any day's examples are saved, only the outer range shows.
    expect(render(emptyDay({}))).not.toContain("Projected");
  });

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
    // The change is marked "today" on screen, not left as a bare number.
    expect(html).toContain("Net today +66 today");
    expect(html).toContain("Last Reset #331");
    expect(html).toContain("Now #318");
    expect(html).toContain("Next Reset #95–#760 estimate");
    expect(html).toContain("Attacks 2 of 8 +71");
    expect(html).toContain("Defenses 1 of 8 −5");
    expect(html).toContain("7 defenses remain · auto defense −21 each");
  });

  it("leaves totals and the automatic loss out until they are known", () => {
    const html = render(
      emptyDay({ attacks: 3, defenses: 2, openDefenses: 6, battles: [] }),
    );
    expect(html).toContain("Attacks 3 of 8 ");
    expect(html).not.toContain("Attacks 3 of 8 –");
    expect(html).toContain("6 defenses remain");
    expect(html).not.toContain("auto defense");
    expect(render(null)).toContain("Trophies 5,696 Net today –");
  });

  it("shows – for attack and defense counts it does not know, not 0", () => {
    // Today's battles not saved yet, or the player page not read at all.
    for (const day of [emptyDay({ lastResetRank: 331 }), null]) {
      const html = render(day);
      expect(html).toContain("Attacks – of 8");
      expect(html).toContain("Defenses – of 8");
      expect(html).not.toContain("0 of 8");
      expect(html).toContain("Trophies 5,696");
      expect(html).toContain("Now #318");
      expect(html).toContain("Next Reset #95–#760");
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
    // Legends at similar trophies today: 41% held, 32 lost a defense.
    legends: { held: 41, defenses: 100, lost: 3200 },
  });
  const held = (count: number, total: number) =>
    Array.from({ length: total }, (_, index) => ({
      stars: index < count ? 2 : 3,
      trophyChange: index < count ? -25 : -40,
      yours: index === total - 1,
    }));
  // No earlier Legend day chosen.
  const TODAY = { chosen: null, read: new Map(), choose: () => {} };

  it("lists hard bases first and colours them for the attacker", async () => {
    const html = await renderInRouter(
      createElement(OpponentsCard, {
        days: TODAY,
        dayNumber: null,
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
    expect(html).toContain('href="/account/watchlists/add/8PP"');
    expect(html.match(/is-yours/g)).toHaveLength(3);
  });

  it("shows a base with 3+ defenses but no comparison as unavailable, not too early", async () => {
    const plain = text(
      await renderInRouter(
        createElement(OpponentsCard, {
          days: TODAY,
          dayNumber: null,
          rows: [
            { ...row("Early", held(0, 2)), legends: null },
            { ...row("No comparison", held(3, 4)), legends: null },
            row("Easy one", held(1, 4)),
          ],
          timeZone: "UTC",
        }),
      ),
    );
    expect(plain).toContain("– held 3 of 4 · no comparison yet");
    expect(plain).not.toContain("Too early held 3 of 4");
    expect(plain).toContain("Too early held 0 of 2 · needs 3+ defenses");
    expect(plain.indexOf("Easy one")).toBeLessThan(plain.indexOf("No comparison"));
    expect(plain.indexOf("No comparison")).toBeLessThan(plain.indexOf("Early"));
  });

  it("says today's battles aren't saved yet rather than no attacks", async () => {
    const render = async (rows: OpponentRow[] | null) =>
      text(
        await renderInRouter(
          createElement(OpponentsCard, {
            days: TODAY,
            dayNumber: null,
            rows,
            timeZone: "UTC",
          }),
        ),
      ).replace(/&#x27;/g, "'");
    const unsaved = await render(null);
    expect(unsaved).toContain(
      "Bases you attacked · today Today's battles aren't saved yet.",
    );
    expect(unsaved).not.toContain("of 8");
    expect(unsaved).not.toContain("No attacks yet today");
    expect(await render([])).toContain("· today · 0 of 8 No attacks yet today.");
  });

  it("shows the older of the opponents' reads and Held % mode's comparison", async () => {
    const at = (minutes: number) => Date.UTC(2026, 9, 7, 9, 0) - minutes * 60_000;
    const updated = async (mode: "held" | "trophies") =>
      text(
        await renderInRouter(
          createElement(OpponentsCard, {
            days: TODAY,
            dayNumber: null,
            rows: [{ ...row("Easy one", held(1, 4)), observedAtMs: at(2) }],
            timeZone: "UTC",
            mode,
            nowMs: at(0),
            comparedAtMs: at(9),
          }),
        ),
      );
    // Both modes compare with the same read of Legends at similar trophies.
    expect(await updated("held")).toContain("updated 9 min ago");
    expect(await updated("trophies")).toContain("updated 9 min ago");
  });

  it("rates bases by trophies given up a defense and shows the day's total in Trophies mode", async () => {
    const plain = text(
      await renderInRouter(
        createElement(OpponentsCard, {
          days: TODAY,
          dayNumber: null,
          rows: [
            row("Easy one", held(1, 4)),
            row("Hard one", held(5, 6)),
            row("Early", held(1, 2)),
            row("First", held(0, 1)),
            {
              ...row("Near 36", held(1, 4)),
              legends: { held: 30, defenses: 100, lost: 3600 },
            },
            {
              ...row("Old service", held(1, 4)),
              legends: { held: 41, defenses: 100, lost: null },
            },
          ],
          timeZone: "UTC",
          mode: "trophies",
        }),
      ),
    );
    expect(plain).toContain("Base strength · vs Legends at similar trophies today");
    // 5 held at −25 and one tripled at −40: 165 over 6 is −28 a defense,
    // against 32 a defense, 192 over 6, at similar trophies.
    expect(plain).toContain("Hard lost −165 · avg −192 · in 6 defenses");
    // One held and three tripled: 145 over 4 is −36 a defense.
    expect(plain).toContain("Easy lost −145 · avg −128 · in 4 defenses");
    // The same base near players losing 36 a defense is Average.
    expect(plain).toContain(
      "Near 36 5,712 ★★★ 100 % +40 · 07:20 Average lost −145 · avg −144 · in 4 defenses",
    );
    expect(plain).toContain("– lost −145 · in 4 defenses · no comparison yet");
    // Under 3 defenses the rating waits, but the day's total so far shows
    // beside the average.
    expect(plain).toContain(
      "Too early lost −65 · avg −64 · in 2 defenses · needs 3+ defenses",
    );
    expect(plain).toContain(
      "Too early lost −40 · avg −32 · in 1 defense · needs 3+ defenses",
    );
    expect(plain).not.toContain("held 1 of 2");
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
      season_day_number: 9,
      rank_range: { best: 95, worst: 760 },
      open_defenses: 7,
      automatic_defense_each: 21,
      board_computed_at: "2026-10-07T09:05:00+00:00",
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
          legends_held: { held: 41, defenses: 100, lost: 3300 },
        },
      ],
    });
    expect(await getPlayerToday(MAIN)).toEqual({
      dayNumber: 9,
      rankRange: { best: 95, worst: 760 },
      projectedRank: null,
      projectedRankPending: false,
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
          legends: { held: 41, defenses: 100, lost: 3300 },
        },
      ],
      boardComputedAtMs: Date.UTC(2026, 9, 7, 9, 5),
    });
    expect(mocks.requestJson).toHaveBeenCalledWith(
      `/v1/players/${encodeURIComponent(MAIN)}/today`,
      "GET",
      undefined,
      undefined,
    );
  });

  it("reads the projected place inside the Next Reset range, and whether it is still worked out", async () => {
    const today = {
      tag: MAIN,
      rank_range: { best: 95, worst: 760 },
      projected_rank: 240,
      projected_rank_pending: false,
      legends_held: null,
      open_defenses: null,
      automatic_defense_each: null,
      opponents: null,
      board_computed_at: null,
    };
    mocks.requestJson.mockResolvedValue(today);
    expect(await getPlayerToday(MAIN)).toMatchObject({
      rankRange: { best: 95, worst: 760 },
      projectedRank: 240,
      projectedRankPending: false,
    });
    mocks.requestJson.mockResolvedValue({
      ...today,
      projected_rank: null,
      projected_rank_pending: true,
    });
    expect(await getPlayerToday(MAIN)).toMatchObject({
      projectedRank: null,
      projectedRankPending: true,
    });
    for (const bad of [{ projected_rank: 0 }, { projected_rank_pending: "yes" }]) {
      mocks.requestJson.mockResolvedValue({ ...today, ...bad });
      await expect(getPlayerToday(MAIN)).rejects.toMatchObject({ status: 502 });
    }
  });

  it("keeps a today read without saved battles apart from one with no attacks", async () => {
    mocks.requestJson.mockResolvedValue({
      tag: MAIN,
      rank_range: { best: 95, worst: 760 },
      open_defenses: null,
      automatic_defense_each: null,
      opponents: null,
      board_computed_at: null,
    });
    expect(await getPlayerToday(MAIN)).toMatchObject({
      rankRange: { best: 95, worst: 760 },
      opponents: null,
      boardComputedAtMs: null,
    });
  });

  it("refuses a today read whose range is impossible", async () => {
    mocks.requestJson.mockResolvedValue({
      tag: MAIN,
      rank_range: { best: 9, worst: 3 },
      open_defenses: null,
      automatic_defense_each: null,
      opponents: [],
      board_computed_at: null,
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

  const load = async (url = `${ORIGIN}/`) => {
    const { data } = unwrap<DashboardLoaderData>(await loader(loaderArgs(url)));
    if (data.kind !== "signed-in") throw new Error("expected signed in");
    return { ...data, cards: await settled(data.cards) };
  };

  it("shows the signed-out preview without a login", async () => {
    mocks.readLoginIdentity.mockResolvedValue(null);
    const { data } = unwrap<DashboardLoaderData>(await loader(loaderArgs(`${ORIGIN}/`)));
    expect(data).toMatchObject({ kind: "signed-out", loginAvailable: true });
    expect(client.getAccount).not.toHaveBeenCalled();
  });

  it("selects the first Legend player and still answers when every card read fails", async () => {
    const data = await load();
    expect(data.selectedTag).toBe(MAIN);
    expect(data.settings).toEqual(defaultSettings());
    // A first visit reads the default cards that sit behind the pop-up.
    // Legend day still has the account's live trophies and rank.
    expect(data.cards).toEqual({
      "legend-day": {
        player: linked(MAIN),
        day: null,
        range: null,
        rangeAtMs: null,
        projected: null,
      },
      "legend-clock": null,
      bases: null,
      goal: null,
      "armies-defense": null,
      "rivals-watch": null,
      "heat-map": null,
    });
    expect(client.getPlayer).toHaveBeenCalledWith(MAIN);
  });

  it("marks Legend day and Bases read before the shared board, and waits for it when asked again", async () => {
    const boardAt = "2026-10-07T09:05:00+00:00";
    // Today's read answers before the board is read; asked again, it waits
    // for it, and the time after for the projected place worked out after
    // it. Once no day's examples are saved, there is none to wait for.
    let waits = 0;
    mocks.requestJson.mockImplementation(async (path: string) => {
      const waited = path.endsWith("?wait_for_board=true");
      if (waited) waits += 1;
      return {
        tag: MAIN,
        rank_range: waited ? { best: 95, worst: 760 } : null,
        projected_rank: waits === 2 ? 240 : null,
        projected_rank_pending: waits === 1,
        open_defenses: 7,
        automatic_defense_each: 21,
        opponents: [],
        board_computed_at: waited ? boardAt : null,
      };
    });
    account.preferences = { dashboard: { v: 3, layout: ["legend-day", "bases"] } };
    const first = await load();
    expect(first.cards["legend-day"]).toMatchObject({
      range: null,
      rangeAtMs: null,
      boardPending: true,
    });
    expect(first.cards.bases).toMatchObject({
      rows: [],
      comparedAtMs: null,
      boardPending: true,
    });
    const again = await load(
      `${ORIGIN}/?${new URLSearchParams({ player: MAIN, cards: "legend-day,bases", board: "wait" })}`,
    );
    // Legend day has its range but not yet its projected place, so asks again.
    expect(again.cards).toEqual({
      "legend-day": {
        player: linked(MAIN),
        day: null,
        range: { best: 95, worst: 760 },
        rangeAtMs: Date.parse(boardAt),
        projected: null,
        boardPending: true,
      },
      bases: { dayNumber: null, rows: [], comparedAtMs: Date.parse(boardAt) },
    });
    const last = await load(
      `${ORIGIN}/?${new URLSearchParams({ player: MAIN, cards: "legend-day", board: "wait" })}`,
    );
    expect(last.cards).toEqual({
      "legend-day": {
        player: linked(MAIN),
        day: null,
        range: { best: 95, worst: 760 },
        rangeAtMs: Date.parse(boardAt),
        projected: 240,
      },
    });
    const none = await load(
      `${ORIGIN}/?${new URLSearchParams({ player: MAIN, cards: "legend-day", board: "wait" })}`,
    );
    expect(none.cards["legend-day"]).toMatchObject({ projected: null });
    expect(none.cards["legend-day"]).not.toHaveProperty("boardPending");
    // One read of today's numbers serves both cards each time.
    expect(mocks.requestJson.mock.calls.map(([path]) => path)).toEqual([
      `/v1/players/${encodeURIComponent(MAIN)}/today`,
      ...Array(3).fill(
        `/v1/players/${encodeURIComponent(MAIN)}/today?wait_for_board=true`,
      ),
    ]);
  });

  it("follows the switcher's player and reads no cards outside Legend I", async () => {
    account.preferences = { dashboard: { v: 3, layout: ["goal"], goal: 50 } };
    const data = await load(`${ORIGIN}/?player=%232pp8qly0`);
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

  it("shows the front page to a sign-in with no account yet", async () => {
    client.getAccount.mockRejectedValue(
      new PythonApiError(404, { error: "account_not_found" }),
    );
    const { data } = unwrap<DashboardLoaderData>(await loader(loaderArgs(`${ORIGIN}/`)));
    expect(data).toMatchObject({ kind: "signed-out", loginAvailable: true });
  });

  it("saves the settings and keeps every other preference", async () => {
    const settings = { v: 3, layout: ["goal", "cutoffs"], goal: 75 };
    const { data, status } = unwrap<DashboardActionData>(
      await action(saveRequest(JSON.stringify(settings))),
    );
    expect(status).toBe(200);
    expect(data.saved).toBe(true);
    // The page shows the answer's settings without reading itself again.
    expect(data.settings).toEqual(readSettings(settings));
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

  it("reloads the cards for another player, never after its own save", () => {
    const check = (from: string, to: string, formMethod?: string, formAction?: string) =>
      shouldRevalidate({
        currentUrl: new URL(`${ORIGIN}${from}`),
        nextUrl: new URL(`${ORIGIN}${to}`),
        formMethod,
        formAction,
        defaultShouldRevalidate: true,
      } as Parameters<typeof shouldRevalidate>[0]);
    // A save answers with the settings as saved; the page reads nothing else.
    expect(check("/", "/", "POST", "/?player=%232PP")).toBe(false);
    expect(check("/", "/", "POST", "/logout")).toBe(true);
    expect(check("/", "/?player=%232PP8QLY0")).toBe(true);
    expect(check("/?player=%232PP8QLY0", "/?player=%232PP8QLY0&cards=goal")).toBe(false);
    // The same address again, such as a reread, reads again.
    expect(check("/?player=%232PP8QLY0", "/?player=%232PP8QLY0")).toBe(true);
  });

  it.each([
    ["saved", 50],
    ["failed", 100],
  ])(
    "reads the new player when a save that %s lands during a switch",
    async (how, kept) => {
      // The account's Goal, which a save changes once the test lets it land.
      let goal = 100;
      let land: (() => void) | null = null;
      let reads = 0;
      let release = () => {};
      const router = createMemoryRouter(
        [
          {
            id: "dashboard",
            path: "/dashboard",
            shouldRevalidate,
            loader: async ({ request }) => {
              const read = {
                player: new URL(request.url).searchParams.get("player"),
                goal,
              };
              // The switch's own read starts before the save lands, and is slow.
              if (read.player === "B" && ++reads === 1)
                await new Promise<void>((resolve) => (release = resolve));
              return read;
            },
            action: () =>
              new Promise((resolve) => {
                land = () => {
                  if (how === "failed")
                    return resolve(dataWithStatus({ saved: false }, { status: 503 }));
                  goal = 50;
                  resolve({ saved: true });
                };
              }),
          },
        ],
        { initialEntries: ["/dashboard?player=A"] },
      );
      await vi.waitFor(() => expect(router.state.initialized).toBe(true));
      void router.fetch("save", "dashboard", "/dashboard?player=A", {
        formMethod: "post",
        formData: new FormData(),
      });
      const switched = router.navigate("/dashboard?player=B");
      await vi.waitFor(() => expect(reads > 0 && land !== null).toBe(true));
      land!();
      await vi.waitFor(() => expect(router.state.navigation.state).toBe("idle"));
      release();
      await switched;
      expect(router.state.location.search).toBe("?player=B");
      expect(router.state.loaderData.dashboard).toEqual({ player: "B", goal: kept });
    },
  );
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

  it("shows the front page, refuses saves and hides the tab while it is off", async () => {
    vi.stubEnv("CLASHLENS_DASHBOARD_ENABLED", "");
    const { data } = unwrap<DashboardLoaderData>(await loader(loaderArgs(`${ORIGIN}/`)));
    expect(data).toMatchObject({ kind: "signed-out", dashboardEnabled: false });
    const thrown = await action(saveRequest(DEFAULT_SAVE)).then(
      () => null,
      (error: unknown) => error,
    );
    expect(unwrap(thrown).status).toBe(404);
    expect(await renderNavigation()).not.toContain(">Dashboard</a>");
  });

  it("shows the tab while it is on", async () => {
    vi.stubEnv("CLASHLENS_DASHBOARD_ENABLED", "true");
    expect(await renderNavigation()).toContain(">Dashboard</a>");
  });
});

describe("dashboard page", () => {
  const CHECKING = "#9YJ2C0QL";

  async function renderPage(loaderData: DashboardLoaderData) {
    const handler = createStaticHandler([
      { path: "/", Component: DashboardRoute, loader: () => loaderData },
    ]);
    const context = await handler.query(new Request(`${ORIGIN}/`));
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

  it("shows Legend day and Bases read before the shared board at once, without waiting for it", async () => {
    const html = text(
      await renderDashboard({
        settings: { ...defaultSettings(), layout: ["legend-day", "bases"] },
        cards: {
          "legend-day": {
            player: linked(MAIN),
            day: null,
            range: null,
            rangeAtMs: null,
            projected: null,
            boardPending: true,
          },
          bases: {
            dayNumber: null,
            rows: [],
            comparedAtMs: null,
            boardPending: true,
          },
        },
      }),
    );
    expect(html).toContain("Trophies 5,696");
    expect(html).toContain("Next Reset –");
    expect(html).not.toContain("Loading");
    expect(html).not.toContain("could not be loaded");
  });

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
      `aria-current="page" href="/?player=${encodeURIComponent(MAIN)}"`,
    );
    expect(html).toContain('href="/account/verify-player"');
    // The player's name shows once, in its tab, cut with "…" and in full on hover.
    expect(html.match(/title="Lens Main"/g)).toHaveLength(1);
  });

  it("puts a Page button on each tab that opens that player's own page", async () => {
    const html = await renderDashboard({
      players: [
        linked(MAIN),
        linked(ALT, { name: "Lens Alt", state: "not_in_legend", trophies: null }),
        linked(CHECKING, { name: "Lens New", state: "checking", rank: null }),
      ],
    });
    const pages = [...html.matchAll(/<a [^>]*class="dash-account-page"[^>]*>/g)].map(
      ([tag]) =>
        `${/aria-label="([^"]+)"/.exec(tag)![1]} → ${/href="([^"]+)"/.exec(tag)![1]}`,
    );
    expect(pages).toEqual([
      "Lens Main player page → /players/%23LQ2V8PJ0",
      "Lens Alt player page → /players/%232PP8QLY0",
      "Lens New player page → /players/%239YJ2C0QL",
    ]);
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
      "rivals-watch:m",
      "goal:s",
      "armies-defense:m",
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

  it("says a linked tag is unknown to the game without asking for a new search", async () => {
    const html = await renderDashboard({
      players: [linked(CHECKING, { name: "Lens New", state: "not_found", rank: null })],
      selectedTag: CHECKING,
    });
    expect(text(html)).toContain(
      "Lens New Player not found. Clash of Clans has no player with this tag.",
    );
    expect(html).not.toContain("search again");
    expect(html).not.toContain("data-card=");
  });

  it("shows only the linking prompt when no player is linked", async () => {
    const html = await renderDashboard({ players: [], selectedTag: null });
    expect(html).toContain("Link your Clash player");
    expect(html).not.toContain("Customise dashboard");
    expect(html).not.toContain("data-card=");
  });

  it("puts search first and says what an account adds after it", async () => {
    const html = await renderPage({
      kind: "signed-out",
      loginAvailable: true,
      dashboardEnabled: true,
      leaderboard: null,
      error: null,
    });
    const plain = text(html);
    expect(plain.indexOf("Find any Legend League player")).toBeLessThan(
      plain.indexOf("Your Legend day, live"),
    );
    expect(plain).toContain("Your Legend day, live");
    expect(plain).toContain("Rank, rivals, Reset clock and goal. Free.");
    expect(plain).toContain("Create account Already have one? Sign in");
  });
});
