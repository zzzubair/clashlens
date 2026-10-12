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
import { loadWebsiteConfig } from "../../app/server/config.server";
import ProvidersRoute, {
  action as providersAction,
  loader as providersLoader,
} from "../../app/routes/account.providers";
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
  return new Request(`${ORIGIN}/account/providers`, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded", Origin: ORIGIN },
    body: new URLSearchParams({ idempotencyKey: KEY, ...fields }).toString(),
  });
}

async function renderPage(): Promise<string> {
  const path = "/account/providers";
  const handler = createStaticHandler([
    { path, Component: ProvidersRoute, loader: providersLoader as never },
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
  });

  it("asks to link Discord first when it isn't linked", async () => {
    providers = ["google"];
    const html = await renderPage();
    expect(html).toContain("Link Discord above to get a Discord message");
    expect(html).not.toContain('role="switch"');
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
    expect(html).toContain("An attack alert for Wanderer #2PP");
    expect(html).toContain("Join the Clash Lens Discord server");
  });

  it("keeps the rest of the page when the alert settings can't be read", async () => {
    mocks.requestJson.mockRejectedValue(new Error("down"));
    const html = await renderPage();
    expect(html).toContain("Attack alerts could not be loaded.");
    expect(html).toContain("Unlink");
  });

  it.each([
    { failure: false, outcome: "saved" },
    { failure: true, outcome: "failed" },
  ])("saves a switch and says it was $outcome", async ({ failure, outcome }) => {
    if (failure) mocks.requestJson.mockRejectedValue(new Error("refused"));
    const response = await providersAction({
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
    expect(response.headers.get("Location")).toBe(`/account/providers?alerts=${outcome}`);
  });

  it("refuses a switch that is neither on nor off", async () => {
    const response = await providersAction({
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
