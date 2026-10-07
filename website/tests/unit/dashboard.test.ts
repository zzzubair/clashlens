import { beforeEach, describe, expect, it, vi } from "vitest";

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

import type { LinkedPlayerCard } from "../../app/lib/account-contracts";
import {
  MAX_CARDS_PER_TAB,
  defaultLayout,
  nextResetMs,
  parsePostedLayout,
  readSavedLayout,
  serializeLayout,
} from "../../app/lib/dashboard";
import {
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

  it("reads an old saved layout leniently", () => {
    const layout = readSavedLayout({
      v: 1,
      tz: "Not/AZone",
      today: [["retired-card", "l"], ["shield", "xl"], ["invite", "s", MAIN], "junk"],
    });
    expect(layout.timeZone).toBe("auto");
    expect(layout.tabs.today).toEqual([
      { card: "shield", size: "s", player: null },
      { card: "invite", size: "s", player: null },
    ]);
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
