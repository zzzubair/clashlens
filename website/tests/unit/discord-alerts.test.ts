import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  StaticRouterProvider,
} from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getWebsiteConfig: vi.fn(),
  requireLogin: vi.fn(),
  createPythonClient: vi.fn(),
  getAttackAlerts: vi.fn(),
  setAttackAlert: vi.fn(),
  requestJson: vi.fn(),
  startProviderAuthorization: vi.fn(),
}));

vi.mock("../../app/server/provider-start.server", () => ({
  startProviderAuthorization: mocks.startProviderAuthorization,
}));

vi.mock("../../app/server/config.server", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/server/config.server")>();
  return { ...actual, getWebsiteConfig: mocks.getWebsiteConfig };
});

vi.mock("../../app/server/auth-guard.server", () => ({
  requireLogin: mocks.requireLogin,
}));

vi.mock("../../app/services/python.server", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../app/services/python.server")>();
  return {
    ...actual,
    createPythonClient: mocks.createPythonClient,
    requestJson: mocks.requestJson,
  };
});

import { mapAttackAlerts } from "../../app/lib/discord";
import {
  LOGIN_COOKIE_NAME,
  createLoginCookieValue,
  createLoginSessionBinding,
} from "../../app/server/auth-cookies.server";
import { loadWebsiteConfig } from "../../app/server/config.server";
import AlertsRoute, {
  action as alertsAction,
  loader as alertsLoader,
} from "../../app/routes/account.discord-alerts";
import * as alertsService from "../../app/services/discord-alerts.server";

const ORIGIN = "https://clashlens.example";
const IDENTITY = { provider: "discord", providerSubject: "110022003300440055" } as const;
const KEY = "3be934b5-68fa-4741-8c7b-e03592e4ad70";
const PLAYERS = [
  { tag: "#2PP", name: "Wanderer" },
  { tag: "#8QQ", name: "Nightfall" },
];
// The private API's payload: Wanderer's attack alerts on and summary off,
// Nightfall's the other way round.
const ALERTS = {
  players: [
    { ...PLAYERS[0], on: true, summary_on: false },
    { ...PLAYERS[1], on: false, summary_on: true },
  ],
  undelivered: null,
  full_notice: false,
};

function post(fields: Record<string, string>): Request {
  return new Request(`${ORIGIN}/account/discord-alerts`, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded", Origin: ORIGIN },
    body: new URLSearchParams({ idempotencyKey: KEY, ...fields }).toString(),
  });
}

async function renderPage(search = ""): Promise<string> {
  const path = "/account/discord-alerts";
  const handler = createStaticHandler([
    { path, Component: AlertsRoute, loader: alertsLoader as never },
  ]);
  const context = await handler.query(new Request(`${ORIGIN}${path}${search}`));
  if (context instanceof Response) throw new Error("unexpected route response");
  return renderToString(
    createElement(StaticRouterProvider, {
      router: createStaticRouter(handler.dataRoutes, context),
      context,
    }),
  )
    .replace(/<script[\s\S]*?<\/script>/g, "")
    .replace(/<!-- -->/g, "");
}

describe("Discord attack alerts settings", () => {
  let providers: string[];

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
    providers = ["discord"];
    mocks.createPythonClient.mockReturnValue({
      getAccount: async () => ({
        username: "wanderer",
        displayName: "Wanderer",
        preferences: {},
        providers,
      }),
      getAccountSummary: async () => ({
        username: "wanderer",
        displayName: "Wanderer",
        verifiedPlayers: PLAYERS,
        defaultPlayer: null,
      }),
    });
    mocks.requestJson.mockReset();
    mocks.requestJson.mockResolvedValue({ kind: "discord-attack-alerts", ...ALERTS });
  });

  it("shows one row per player with two switches that say where they are", async () => {
    const html = await renderPage();
    const rows = html.split("<li").slice(1);
    // Two players, then the account's full details switch.
    expect(rows).toHaveLength(3);
    const switchOf = (row: string, label: string) =>
      row.match(new RegExp(`<button[^>]*aria-label="${label}"[^>]*>`))?.[0] ?? "";
    // Wanderer: attack alerts on, summary off; Nightfall the other way round.
    expect(rows[0]).toContain("Wanderer");
    expect(switchOf(rows[0], "Attack alerts for Wanderer #2PP")).toContain(
      'aria-checked="true"',
    );
    expect(switchOf(rows[0], "Daily summary for Wanderer #2PP")).toContain(
      'aria-checked="false"',
    );
    expect(switchOf(rows[1], "Attack alerts for Nightfall #8QQ")).toContain(
      'aria-checked="false"',
    );
    expect(switchOf(rows[1], "Daily summary for Nightfall #8QQ")).toContain(
      'aria-checked="true"',
    );
    // Each switch posts the state it flips to.
    expect(rows[0]).toMatch(
      /value="attack-alert"[\s\S]*?name="on" value="off"[\s\S]*?value="day-summary"[\s\S]*?name="on" value="on"/,
    );
    expect(switchOf(rows[2], "Full details in notifications")).toContain(
      'aria-checked="false"',
    );
    expect(html.match(/role="switch"/g)).toHaveLength(5);
    // The state is said once, by the switch: no badges or sentences repeat it.
    for (const text of ["✓ On", ">Off<", "No alerts for", "Turn on", "Turn off"])
      expect(html).not.toContain(text);
    expect(html).toContain("🛡️ Wanderer −24 🏆");
    expect(html).not.toContain("wasn&#x27;t delivered");
    expect(html).not.toContain("Reconnect Discord");
  });

  it("asks to connect Discord first when it isn't connected", async () => {
    providers = ["google"];
    const html = await renderPage();
    expect(html).toContain("Connect Discord first");
    expect(html).toContain('name="intent" value="connect-discord"');
    expect(html).not.toContain('role="switch"');
  });

  it("says Discord connected after connecting it from this page", async () => {
    const html = await renderPage("?saved=discord-connected");
    expect(html).toMatch(/role="status"[^>]*>Discord connected\.</);
    expect(html.match(/role="switch"/g)).toHaveLength(5);
  });

  it("names a player with no saved name by its tag, as Settings and the profile do", async () => {
    mocks.requestJson.mockResolvedValue({
      kind: "discord-attack-alerts",
      players: [{ tag: "#L82PYUC9J", name: null, on: false, summary_on: false }],
      undelivered: null,
      full_notice: false,
    });
    const html = await renderPage();
    expect(html).not.toContain("Unnamed");
    expect(html).toContain("<span>#L82PYUC9J</span>");
    expect(html).toContain('aria-label="Attack alerts for #L82PYUC9J"');
    expect(html).toContain("🛡️ #L82PYUC9J −24 🏆");
  });

  it("connects Discord through the existing flow and comes back here", async () => {
    const config = mocks.getWebsiteConfig();
    const login = createLoginCookieValue(IDENTITY, config.loginSecret, Date.now());
    mocks.startProviderAuthorization.mockResolvedValue(
      new Response(null, {
        status: 302,
        headers: { Location: "https://discord.example" },
      }),
    );
    const request = post({ intent: "connect-discord" });
    request.headers.set("cookie", `${LOGIN_COOKIE_NAME}=${login}`);
    const response = await alertsAction({ request } as never);
    expect(response.headers.get("Location")).toBe("https://discord.example");
    expect(mocks.startProviderAuthorization).toHaveBeenCalledWith(
      config,
      "discord",
      "link",
      "/account/discord-alerts",
      createLoginSessionBinding(login),
    );
  });

  it("says what to do about an alert Discord didn't deliver", async () => {
    mocks.requestJson.mockResolvedValue({
      kind: "discord-attack-alerts",
      ...ALERTS,
      undelivered: {
        tag: "#2PP",
        reason: "no_shared_server",
        at: "2026-10-12T06:00:00Z",
      },
    });
    const html = await renderPage();
    expect(html).toContain("A Discord message about Wanderer #2PP");
    expect(html).toContain("Reconnect Discord to add it");
    expect(html).toContain('name="intent" value="connect-discord"');
  });

  it("says so when the alert settings can't be read", async () => {
    mocks.requestJson.mockRejectedValue(new Error("down"));
    const html = await renderPage();
    expect(html).toContain("Your alerts could not be loaded.");
  });

  it.each([
    { failure: false, outcome: "on" },
    { failure: true, outcome: "failed" },
  ])("saves a switch and says it was $outcome", async ({ failure, outcome }) => {
    if (failure) mocks.requestJson.mockRejectedValue(new Error("refused"));
    const response = await alertsAction({
      request: post({ intent: "attack-alert", tag: "#8QQ", on: "on" }),
    } as never);
    expect(mocks.requestJson).toHaveBeenCalledWith(
      "/v1/account/discord-alerts",
      "POST",
      Buffer.from(JSON.stringify({ tag: "#8QQ", on: true, alert: "attack" })),
      undefined,
      KEY,
      IDENTITY,
    );
    expect(response.headers.get("Location")).toBe(
      `/account/discord-alerts?alerts=${outcome}&tag=%238QQ`,
    );
  });

  it.each([
    [
      "?alerts=on&tag=%238QQ",
      "Attack alerts on for Nightfall. The bot is sending you a confirmation message on Discord now.",
    ],
    ["?alerts=off&tag=%232PP", "Attack alerts off for Wanderer."],
    ["?summary=on&tag=%238QQ", "Daily summary on for Nightfall."],
  ])("says what %s saved in one line at the top", async (search, line) => {
    const html = await renderPage(search);
    expect(html).toContain(
      `<main id="main-content" tabindex="-1" class="page-shell narrow-shell"><p class="status-banner status-banner-success saved-line" role="status">${line}`,
    );
    expect(html.match(/saved-line/g)).toHaveLength(1);
  });

  it("says a failed switch wasn't saved", async () => {
    const html = await renderPage("?alerts=failed&tag=%232PP");
    expect(html).toContain(
      'class="status-banner status-banner-warning saved-line" role="alert">Not saved. Try again.',
    );
  });

  it("saves a daily summary switch", async () => {
    const response = await alertsAction({
      request: post({ intent: "day-summary", tag: "#8QQ", on: "off" }),
    } as never);
    expect(mocks.requestJson).toHaveBeenCalledWith(
      "/v1/account/discord-alerts",
      "POST",
      Buffer.from(JSON.stringify({ tag: "#8QQ", on: false, alert: "day_summary" })),
      undefined,
      KEY,
      IDENTITY,
    );
    expect(response.headers.get("Location")).toBe(
      "/account/discord-alerts?summary=off&tag=%238QQ",
    );
  });

  it("saves the account's full details switch without naming a player", async () => {
    const response = await alertsAction({
      request: post({ intent: "full-notice", on: "on" }),
    } as never);
    expect(mocks.requestJson).toHaveBeenCalledWith(
      "/v1/account/discord-alerts",
      "POST",
      Buffer.from(JSON.stringify({ on: true, alert: "full_notice" })),
      undefined,
      KEY,
      IDENTITY,
    );
    expect(response.headers.get("Location")).toBe("/account/discord-alerts?full=on");
  });

  it("shows full details on, with the attack in the example notification", async () => {
    mocks.requestJson.mockResolvedValue({
      kind: "discord-attack-alerts",
      ...ALERTS,
      full_notice: true,
    });
    const html = await renderPage("?full=on");
    expect(html).toContain('role="status">Full details in notifications on.</p>');
    expect(html).toMatch(
      /<button[^>]*aria-checked="true"[^>]*aria-label="Full details in notifications"/,
    );
    expect(html).toContain("🛡️ Wanderer −24 🏆 · ⚔️ Drift ★★☆ · 87%<br/>🏆 5,312");
  });

  it("refuses a switch that is neither on nor off", async () => {
    const response = await alertsAction({
      request: post({ intent: "attack-alert", tag: "#8QQ", on: "yes" }),
    } as never);
    expect(response.status).toBe(400);
    expect(mocks.requestJson).not.toHaveBeenCalled();
  });

  it("rejects malformed alert settings from the private API", async () => {
    expect(mapAttackAlerts({ players: [{ tag: "#2PP", name: null, on: "yes" }] })).toBe(
      null,
    );
    expect(
      mapAttackAlerts({
        players: [{ tag: "#2PP", name: null, on: true }],
        undelivered: null,
        full_notice: false,
      }),
    ).toBe(null);
    expect(mapAttackAlerts({ players: [], undelivered: null })).toBe(null);
    expect(
      mapAttackAlerts({
        players: [],
        undelivered: { tag: "#2PP", reason: "banned", at: "2026-10-12T06:00:00Z" },
        full_notice: false,
      }),
    ).toBe(null);
    mocks.requestJson.mockResolvedValue({ kind: "discord-attack-alerts", players: 1 });
    await expect(alertsService.getAttackAlerts(IDENTITY)).rejects.toMatchObject({
      status: 502,
    });
  });
});
