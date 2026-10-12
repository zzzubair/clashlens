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

import { mapAttackAlerts, type AttackAlerts } from "../../app/lib/discord";
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
const ALERTS: AttackAlerts = {
  players: [
    { ...PLAYERS[0], on: true },
    { ...PLAYERS[1], on: false },
  ],
  undelivered: null,
};

function post(fields: Record<string, string>): Request {
  return new Request(`${ORIGIN}/account/discord-alerts`, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded", Origin: ORIGIN },
    body: new URLSearchParams({ idempotencyKey: KEY, ...fields }).toString(),
  });
}

async function renderPage(): Promise<string> {
  const path = "/account/discord-alerts";
  const handler = createStaticHandler([
    { path, Component: AlertsRoute, loader: alertsLoader as never },
  ]);
  const context = await handler.query(new Request(`${ORIGIN}${path}`));
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

  it("shows one switch per verified player, off unless turned on", async () => {
    const html = await renderPage();
    expect(html).toContain("Alerts");
    expect(html).toMatch(
      /role="switch" aria-checked="true" aria-label="Attack alerts for Wanderer #2PP"/,
    );
    expect(html).toMatch(
      /role="switch" aria-checked="false" aria-label="Attack alerts for Nightfall #8QQ"/,
    );
    expect(html).toContain("was attacked");
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
    expect(html).toContain("Attack alerts could not be loaded.");
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
      Buffer.from(JSON.stringify({ tag: "#8QQ", on: true })),
      undefined,
      KEY,
      IDENTITY,
    );
    expect(response.headers.get("Location")).toBe(
      `/account/discord-alerts?alerts=${outcome}`,
    );
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
        players: [],
        undelivered: { tag: "#2PP", reason: "banned", at: "2026-10-12T06:00:00Z" },
      }),
    ).toBe(null);
    mocks.requestJson.mockResolvedValue({ kind: "discord-attack-alerts", players: 1 });
    await expect(alertsService.getAttackAlerts(IDENTITY)).rejects.toMatchObject({
      status: 502,
    });
  });
});
