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
  return { ...actual, createPythonClient: mocks.createPythonClient };
});

import { LegendClock } from "../../app/components/LegendClock";
import type { LinkedPlayerCard } from "../../app/lib/account-contracts";
import type { PlayerDay } from "../../app/lib/dashboard";
import {
  MAX_CARDS_PER_TAB,
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

function saveRequest(layout: string, origin = ORIGIN): Parameters<typeof action>[0] {
  return {
    request: new Request(`${ORIGIN}/dashboard`, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded", Origin: origin },
      body: new URLSearchParams({ idempotencyKey: IDEMPOTENCY_KEY, layout }).toString(),
    }),
    params: {},
    context: {},
  } as unknown as Parameters<typeof action>[0];
}

describe("dashboard layout", () => {
  it("falls back to the default layout when nothing is saved", () => {
    expect(readSavedLayout(undefined)).toEqual(defaultLayout());
    expect(readSavedLayout({ v: 2, today: [] })).toEqual(defaultLayout());
  });

  it("round-trips a saved layout with pins and a time zone", () => {
    const layout = defaultLayout();
    layout.timeZone = "Asia/Tokyo";
    layout.tabs.today = [
      { card: "clock", size: "l", player: null },
      { card: "clock", size: "s", player: MAIN },
    ];
    layout.tabs.crew = [];
    const stored = serializeLayout(layout);
    expect(stored.today).toEqual([
      ["clock", "l"],
      ["clock", "s", MAIN],
    ]);
    expect(readSavedLayout(stored)).toEqual(layout);
    expect(parsePostedLayout(JSON.parse(JSON.stringify(stored)))).toEqual(layout);
  });

  it("reads an old saved layout leniently and keeps cards on their own tab", () => {
    const layout = readSavedLayout({
      v: 1,
      tz: "Not/AZone",
      today: [["retired-card", "l"], ["shield", "xl"], ["invite", "s", MAIN], "junk"],
    });
    expect(layout.timeZone).toBe("auto");
    expect(layout.tabs.today).toEqual([{ card: "shield", size: "s", player: null }]);
    expect(layout.tabs.season).toEqual(defaultLayout().tabs.season);
  });

  it("rejects a posted layout the page would never send", () => {
    const valid = serializeLayout(defaultLayout());
    const posted = (changes: Record<string, unknown>) =>
      parsePostedLayout({ ...valid, ...changes });
    expect(posted({})).not.toBeNull();
    expect(posted({ today: [["nope", "l"]] })).toBeNull();
    expect(posted({ today: [["shield", "xl"]] })).toBeNull();
    expect(posted({ crew: [["invite", "l", MAIN]] })).toBeNull();
    expect(posted({ season: [["clock", "l"]] })).toBeNull();
    expect(posted({ today: [["clock", "l", "#lq2v8pj0"]] })).toBeNull();
    expect(posted({ tz: "Not/AZone" })).toBeNull();
    expect(posted({ season: undefined })).toBeNull();
    expect(
      posted({
        today: Array.from({ length: MAX_CARDS_PER_TAB + 1 }, () => ["clock", "l"]),
      }),
    ).toBeNull();
  });

  it("finds the next 05:00 UTC Reset", () => {
    expect(nextResetMs(Date.UTC(2026, 9, 7, 1, 48))).toBe(Date.UTC(2026, 9, 7, 5));
    expect(nextResetMs(Date.UTC(2026, 9, 7, 5))).toBe(Date.UTC(2026, 9, 8, 5));
    expect(nextResetMs(Date.UTC(2026, 9, 7, 23, 59))).toBe(Date.UTC(2026, 9, 8, 5));
  });
});

function clockText(day: PlayerDay | null, today: LinkedPlayerCard["today"]) {
  const html = renderToStaticMarkup(
    createElement(LegendClock, {
      player: linked(MAIN),
      day,
      today,
      size: "l",
      nowMs: null,
      timeZone: "UTC",
    }),
  );
  const rows = [...html.matchAll(/<p class="clock-battles-title">(.*?)<\/p>/g)].map(
    (match) => (match[1] ?? "").replace(/<[^>]+>/g, "").trim(),
  );
  return { html, rows };
}

describe("Legend clock", () => {
  const attack = { at: Date.UTC(2026, 9, 7, 9), kind: "attack" as const, stars: 3 };

  it("shows battle totals only for a complete day", () => {
    const battles = [{ ...attack, trophyChange: 40 }];
    const day = { dayNumber: 3, dayCount: 28, battles, net: 40, attacks: 1, defenses: 0 };
    expect(
      clockText({ ...day, complete: true }, { net: 40, attacks: 1, defenses: 0 }).rows,
    ).toEqual(["Attacks 1/8 +40", "Defenses 0/8 0"]);
    expect(
      clockText(
        { ...day, complete: false, net: null, attacks: 5, defenses: 2 },
        { net: null, attacks: 4, defenses: 2 },
      ).rows,
    ).toEqual(["Attacks 5/8", "Defenses 2/8"]);
    expect(clockText(null, null).rows).toEqual(["Attacks –/8", "Defenses –/8"]);
    expect(clockText(null, { net: null, attacks: 4, defenses: 1 }).rows).toEqual([
      "Attacks 4/8",
      "Defenses 1/8",
    ]);
  });

  it("takes the day's gain and counts from one read", () => {
    const html = clockText(
      {
        dayNumber: 3,
        dayCount: 28,
        battles: [
          { ...attack, trophyChange: 40 },
          { ...attack, at: attack.at + 60_000, trophyChange: 40 },
        ],
        complete: true,
        net: 80,
        attacks: 2,
        defenses: 0,
      },
      { net: 40, attacks: 1, defenses: 0 },
    );
    expect(html.rows[0]).toBe("Attacks 2/8 +80");
    expect(html.html).toContain("+80 today");
    expect(html.html).not.toContain("+40 today");
  });

  it("names the upcoming Reset in local time across a clock change", () => {
    const html = renderToStaticMarkup(
      createElement(LegendClock, {
        player: linked(MAIN),
        day: null,
        today: null,
        size: "s",
        nowMs: Date.UTC(2026, 9, 25, 2),
        timeZone: "Europe/London",
      }),
    );
    expect(html).toContain("to Reset at 05:00");
    expect(html).toContain('<text class="clock-reset-time" x="150" y="36">05:00</text>');
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
  });

  beforeEach(() => {
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

  it("shows the signed-out preview without a login", async () => {
    mocks.readLoginIdentity.mockResolvedValue(null);
    const { data } = unwrap<DashboardLoaderData>(
      await loader(loaderArgs(`${ORIGIN}/dashboard`)),
    );
    expect(data).toEqual({ kind: "signed-out", loginAvailable: true });
    expect(client.getAccount).not.toHaveBeenCalled();
  });

  it("selects the first Legend player and survives a missing player page", async () => {
    const { data } = unwrap<DashboardLoaderData>(
      await loader(loaderArgs(`${ORIGIN}/dashboard`)),
    );
    expect(data.kind).toBe("signed-in");
    if (data.kind !== "signed-in") return;
    expect(data.selectedTag).toBe(MAIN);
    expect(data.layout).toEqual(defaultLayout());
    expect(data.days).toEqual({});
    expect(client.getPlayer).toHaveBeenCalledWith(MAIN);
  });

  it("keeps the day's battles, whether they are complete, and when the day ends", async () => {
    vi.useFakeTimers({ toFake: ["Date"], now: Date.UTC(2026, 9, 7, 9, 30) });
    client.getPlayer.mockResolvedValue({
      season: { currentDayNumber: 3, dayCount: 28 },
      currentDay: {
        battlesComplete: false,
        trophyChange: null,
        offense: { attacks: 2 },
        defense: { defenses: 1 },
        offenseEvents: [
          { battleTimestamp: "2026-10-07T09:00:00Z", stars: 3, trophyChange: 40 },
        ],
        defenseEvents: [],
      },
    });
    const { data } = unwrap<DashboardLoaderData>(
      await loader(loaderArgs(`${ORIGIN}/dashboard`)),
    );
    if (data.kind !== "signed-in") throw new Error("expected signed in");
    expect(data.dayEndsMs).toBe(Date.UTC(2026, 9, 8, 5));
    expect(data.days[MAIN]).toEqual({
      dayNumber: 3,
      dayCount: 28,
      battles: [
        { at: Date.UTC(2026, 9, 7, 9), kind: "attack", stars: 3, trophyChange: 40 },
      ],
      complete: false,
      net: null,
      attacks: 2,
      defenses: 1,
    });
  });

  it("works out a complete live day's gain from its attacks and defenses", async () => {
    const day = {
      battlesComplete: true,
      trophyChange: null,
      offense: { attacks: 2, trophyGain: 80 },
      defense: { defenses: 1, trophyLoss: 16 },
      offenseEvents: [],
      defenseEvents: [],
    };
    client.getPlayer.mockResolvedValue({ season: null, currentDay: day });
    const complete = unwrap<DashboardLoaderData>(
      await loader(loaderArgs(`${ORIGIN}/dashboard`)),
    ).data;
    if (complete.kind !== "signed-in") throw new Error("expected signed in");
    expect(complete.days[MAIN]).toMatchObject({ complete: true, net: 64 });
    client.getPlayer.mockResolvedValue({
      season: null,
      currentDay: { ...day, battlesComplete: false },
    });
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
    account.preferences = { dashboard: { v: 1, tz: "auto", today: [["goal", "s"]] } };
    const { data } = unwrap<DashboardLoaderData>(
      await loader(loaderArgs(`${ORIGIN}/dashboard?player=%232pp8qly0`)),
    );
    if (data.kind !== "signed-in") throw new Error("expected signed in");
    expect(data.selectedTag).toBe(ALT);
    expect(data.layout.tabs.today).toEqual([{ card: "goal", size: "s", player: null }]);
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
    layout.tabs.today = [{ card: "clock", size: "s", player: MAIN }];
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
    const bad = unwrap<DashboardActionData>(await action(saveRequest('{"v":1}')));
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

describe("dashboard page", () => {
  const CHECKING = "#9YJ2C0QL";

  async function renderDashboard(loaderData: DashboardLoaderData, search = "") {
    const handler = createStaticHandler([
      { path: "/dashboard", Component: DashboardRoute, loader: () => loaderData },
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

  it("says each player's status once and shows live numbers only on the clock", async () => {
    const layout = defaultLayout();
    layout.tabs.today = [
      { card: "clock", size: "l", player: null },
      { card: "shield", size: "s", player: ALT },
      { card: "around", size: "s", player: ALT },
      { card: "goal", size: "s", player: CHECKING },
    ];
    const html = await renderDashboard({
      kind: "signed-in",
      players: [
        linked(ALT, { name: "Lens Alt", state: "not_in_legend", trophies: null }),
        linked(MAIN),
        linked(CHECKING, { name: "Lens New", state: "checking", rank: null }),
      ],
      playersUnavailable: false,
      selectedTag: MAIN,
      layout,
      days: {},
      dayEndsMs: Date.UTC(2026, 9, 8, 5),
      idempotencyKey: IDEMPOTENCY_KEY,
    });
    expect(html.match(/not in Legends/gi)).toHaveLength(1);
    expect(html).toContain("Lens Alt is not in Legends");
    expect(html).toContain("Lens New</b> Checking this tag with Clash of Clans.");
    expect(html.match(/5,696/g)).toHaveLength(1);
    expect(html.match(/#318/g)).toHaveLength(1);
    expect(html).not.toContain('data-card="shield"');
    expect(html).not.toContain('data-card="goal"');
  });

  it("shows only the linking prompt on every tab when no player is linked", async () => {
    for (const tab of ["today", "season", "crew"]) {
      const html = await renderDashboard(
        {
          kind: "signed-in",
          players: [],
          playersUnavailable: false,
          selectedTag: null,
          layout: defaultLayout(),
          days: {},
          dayEndsMs: Date.UTC(2026, 9, 8, 5),
          idempotencyKey: IDEMPOTENCY_KEY,
        },
        `?tab=${tab}`,
      );
      expect(html).toContain("Link your Clash player");
      expect(html).not.toContain("data-card=");
    }
  });
});
