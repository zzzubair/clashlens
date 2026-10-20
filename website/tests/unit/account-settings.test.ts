import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  StaticRouterProvider,
  type RouteObject,
} from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getWebsiteConfig: vi.fn(),
  requireLogin: vi.fn(),
  createPythonClient: vi.fn(),
  startProviderAuthorization: vi.fn(),
  unlinkPlayer: vi.fn(),
}));

vi.mock("../../app/server/config.server", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/server/config.server")>();
  return { ...actual, getWebsiteConfig: mocks.getWebsiteConfig };
});

vi.mock("../../app/server/auth-guard.server", () => ({
  requireLogin: mocks.requireLogin,
}));

vi.mock("../../app/server/provider-start.server", () => ({
  startProviderAuthorization: mocks.startProviderAuthorization,
}));

vi.mock("../../app/services/python.server", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../app/services/python.server")>();
  return { ...actual, createPythonClient: mocks.createPythonClient };
});

vi.mock("../../app/services/unlink-player.server", () => ({
  unlinkPlayer: mocks.unlinkPlayer,
}));

import {
  LOGIN_COOKIE_NAME,
  createLoginCookieValue,
  createLoginSessionBinding,
} from "../../app/server/auth-cookies.server";
import { loadWebsiteConfig, type WebsiteConfig } from "../../app/server/config.server";
import { PythonApiError } from "../../app/services/python.server";
import { loader as movedLoader } from "../../app/routes/account.moved";
import UnlinkRoute, {
  action as unlinkAction,
  loader as unlinkLoader,
} from "../../app/routes/account.players.$tag.unlink";
import SettingsRoute, {
  action as settingsAction,
  headers as settingsHeaders,
  loader as settingsLoader,
} from "../../app/routes/account.settings";

const ORIGIN = "https://clashlens.example";
const IDENTITY = { provider: "google", providerSubject: "11223344556677889900" } as const;
const KEY = "3be934b5-68fa-4741-8c7b-e03592e4ad70";
const ACCOUNT = {
  username: "nova88",
  displayName: "Nova",
  preferences: { theme: "dark" },
  providers: ["google"],
};
const SUMMARY = {
  username: "nova88",
  displayName: "Nova",
  verifiedPlayers: [
    { tag: "#2PP", name: "Nova" },
    { tag: "#8PY", name: null },
  ],
  defaultPlayer: "#8PY",
};

let config: WebsiteConfig;
let client: {
  getAccount: ReturnType<typeof vi.fn>;
  getAccountSummary: ReturnType<typeof vi.fn>;
  updateAccount: ReturnType<typeof vi.fn>;
  setDefaultPlayer: ReturnType<typeof vi.fn>;
};

beforeEach(() => {
  vi.clearAllMocks();
  config = loadWebsiteConfig({
    NODE_ENV: "test",
    CLASHLENS_LOGIN_ENABLED: "true",
    CLASHLENS_PUBLIC_ORIGIN: ORIGIN,
    CLASHLENS_GOOGLE_CLIENT_ID: "test-client.apps.googleusercontent.com",
    CLASHLENS_GOOGLE_CLIENT_SECRET: "test-client-secret",
    CLASHLENS_DISCORD_CLIENT_ID: "1234567890123456789",
    CLASHLENS_DISCORD_CLIENT_SECRET: "discord-test-secret",
    CLASHLENS_LOGIN_SECRET_B64: "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8",
  });
  mocks.getWebsiteConfig.mockReturnValue(config);
  mocks.requireLogin.mockResolvedValue(IDENTITY);
  client = {
    getAccount: vi.fn(async () => ({ ...ACCOUNT })),
    getAccountSummary: vi.fn(async () => ({ ...SUMMARY })),
    updateAccount: vi.fn(async () => ({ ...ACCOUNT })),
    setDefaultPlayer: vi.fn(async () => undefined),
  };
  mocks.createPythonClient.mockReturnValue(client);
  mocks.startProviderAuthorization.mockResolvedValue(
    new Response(null, {
      status: 302,
      headers: { Location: "https://provider.example" },
    }),
  );
  mocks.unlinkPlayer.mockResolvedValue(undefined);
});

function post(path: string, fields: Record<string, string>, headers = {}): Request {
  return new Request(`${ORIGIN}${path}`, {
    method: "POST",
    headers: {
      "content-type": "application/x-www-form-urlencoded",
      Origin: ORIGIN,
      ...headers,
    },
    body: new URLSearchParams(fields).toString(),
  });
}

/** The page as a browser gets it after `request`, a GET or a form POST. */
async function render(route: RouteObject, request: Request): Promise<string> {
  const handler = createStaticHandler([route]);
  const context = await handler.query(request);
  if (context instanceof Response) throw context;
  return renderToString(
    createElement(StaticRouterProvider, {
      router: createStaticRouter(handler.dataRoutes, context),
      context,
      hydrate: false,
    }),
  )
    .replaceAll("<!-- -->", "")
    .replaceAll(' data-discover="true"', "");
}

const settingsPage = {
  path: "/account/settings",
  Component: SettingsRoute,
  loader: settingsLoader as never,
  action: settingsAction as never,
};

function settings(search = ""): Promise<string> {
  return render(settingsPage, new Request(`${ORIGIN}/account/settings${search}`));
}

function location(thrown: unknown): string | null {
  expect(thrown).toBeInstanceOf(Response);
  return (thrown as Response).headers.get("Location");
}

describe("Settings", () => {
  it("shows the name, the Clash players with the default, and each sign-in", async () => {
    const html = await settings();
    for (const text of [
      ">Settings</h1>",
      'value="Nova"',
      "@nova88",
      "Usernames can&#x27;t be changed.",
      ">Clash players</h2>",
      "✓</span> Default",
      ">Make default</button>",
      'href="/account/players/%232PP/unlink"',
      'href="/account/players/%238PY/unlink"',
      ">Link another player</a>",
      ">Sign-in</h2>",
    ])
      expect(html).toContain(text);
    // #8PY is the default, so only #2PP offers Make default.
    expect(html.match(/>Make default</g)).toHaveLength(1);
    expect(html).not.toContain(IDENTITY.providerSubject);
    expect(settingsHeaders()).toEqual({ "Cache-Control": "no-store" });
  });

  it("marks each sign-in connected or not, and keeps the only one", async () => {
    const only = await settings();
    expect(only).toContain("Connected. It&#x27;s your only way to sign in");
    expect(only).toMatch(/disabled=""[^>]*>Disconnect<\/button>/);
    expect(only).toContain("Not connected.");
    expect(only).toContain(">Connect</button>");

    client.getAccount.mockResolvedValue({ ...ACCOUNT, providers: ["google", "discord"] });
    const both = await settings();
    expect(both.match(/>Disconnect<\/button>/g)).toHaveLength(2);
    expect(both).not.toContain("disabled");
    expect(both).not.toContain("only way to sign in");
  });

  it("offers to link a first player when none is linked", async () => {
    client.getAccountSummary.mockResolvedValue({
      ...SUMMARY,
      verifiedPlayers: [],
      defaultPlayer: null,
    });
    const html = await settings();
    expect(html).toContain("You haven&#x27;t linked a Clash player yet.");
    expect(html).toContain(">Link your Clash player</a>");
    expect(html).not.toContain("Make default");
  });

  it.each([
    ["?saved=name", "Display name saved."],
    ["?saved=default", "Default player saved."],
    ["?saved=unlinked&tag=%232PP", "#2PP unlinked."],
    ["?saved=unlinked&tag=nonsense", "Player unlinked."],
    ["?saved=google-connected", "Google connected."],
    ["?saved=discord-disconnected", "Discord disconnected."],
  ])("says what %s saved in one line at the top", async (search, line) => {
    const html = await settings(search);
    expect(html).toContain(
      `<main id="main-content" tabindex="-1" class="page-shell narrow-shell"><p class="status-banner status-banner-success saved-line" role="status">${line}</p>`,
    );
  });

  it("says a failed default save failed, and ignores unknown words", async () => {
    const failed = await settings("?saved=default-failed");
    expect(failed).toContain(
      'class="status-banner status-banner-warning saved-line" role="alert">The default player could not be saved. Try again.',
    );
    expect(await settings("?saved=<b>hi</b>")).not.toContain("saved-line");
  });

  it.each([403, 404])("sends an account not set up yet (%s) to setup", async (status) => {
    client.getAccount.mockRejectedValue(
      new PythonApiError(status, { error: "account_not_found" }),
    );
    await expect(settings()).rejects.toSatisfy(
      (thrown) => location(thrown) === "/account/setup",
    );
  });

  it("shows a read failure with Try again, then recovers", async () => {
    client.getAccountSummary.mockRejectedValueOnce(
      new PythonApiError(503, { error: "unavailable" }),
    );
    const failed = await settings();
    expect(failed).toContain("Your settings could not be loaded.");
    expect(failed).toContain('<a href="/account/settings">Try again</a>');
    expect(failed).not.toContain('name="displayName"');
    expect(await settings()).toContain('name="displayName"');
  });

  it("saves the display name, keeping the username and preferences", async () => {
    const response = (await settingsAction({
      request: post("/account/settings", {
        intent: "display-name",
        displayName: "  Nova Nova ",
        idempotencyKey: KEY,
      }),
    } as never)) as Response;
    expect(response.headers.get("Location")).toBe("/account/settings?saved=name");
    expect(client.updateAccount).toHaveBeenCalledWith(
      { username: "nova88", displayName: "Nova Nova", preferences: { theme: "dark" } },
      KEY,
    );
  });

  it("keeps a refused name in the box with the reason beside it", async () => {
    const html = await render(
      settingsPage,
      post("/account/settings", {
        intent: "display-name",
        displayName: "FuckYou",
        idempotencyKey: KEY,
      }),
    );
    expect(html).toContain('value="FuckYou"');
    expect(html).toContain('aria-invalid="true"');
    expect(html).toContain("Choose a different display name.");
    expect(html).not.toContain("saved-line");
    expect(client.updateAccount).not.toHaveBeenCalled();

    client.updateAccount.mockRejectedValue(
      new PythonApiError(503, { error: "unavailable" }),
    );
    const failed = await render(
      settingsPage,
      post("/account/settings", {
        intent: "display-name",
        displayName: "New name",
        idempotencyKey: KEY,
      }),
    );
    expect(failed).toContain('value="New name"');
    expect(failed).toContain('role="alert"');
  });

  it.each([
    { fails: false, saved: "default" },
    { fails: true, saved: "default-failed" },
  ])("chooses the default player and says $saved", async ({ fails, saved }) => {
    if (fails) client.setDefaultPlayer.mockRejectedValue(new PythonApiError(404, {}));
    const response = (await settingsAction({
      request: post("/account/settings", {
        intent: "default-player",
        tag: "#2PP",
        idempotencyKey: KEY,
      }),
    } as never)) as Response;
    expect(client.setDefaultPlayer).toHaveBeenCalledWith("#2PP", KEY);
    expect(response.headers.get("Location")).toBe(`/account/settings?saved=${saved}`);
  });

  it("starts connecting or disconnecting only from this site, bound to the login", async () => {
    const cookie = createLoginCookieValue(IDENTITY, config.loginSecret, Date.now());
    const fields = { intent: "unlink", provider: "google", idempotencyKey: KEY };
    const started = (await settingsAction({
      request: post("/account/settings", fields, {
        cookie: `${LOGIN_COOKIE_NAME}=${cookie}`,
      }),
    } as never)) as Response;
    expect(started.status).toBe(302);
    expect(mocks.startProviderAuthorization).toHaveBeenCalledWith(
      config,
      "google",
      "unlink",
      "/account/settings",
      createLoginSessionBinding(cookie),
    );

    mocks.startProviderAuthorization.mockClear();
    const hostile = (await settingsAction({
      request: post("/account/settings", fields, { Origin: "https://evil.example" }),
    } as never)) as Response;
    expect(hostile.status).toBe(403);
    const unknown = (await settingsAction({
      request: post("/account/settings", { ...fields, provider: "github" }),
    } as never)) as Response;
    expect(unknown.status).toBe(400);
    const noKey = (await settingsAction({
      request: post("/account/settings", { ...fields, idempotencyKey: "" }),
    } as never)) as Response;
    expect(noKey.status).toBe(400);
    expect(mocks.startProviderAuthorization).not.toHaveBeenCalled();
    expect(client.updateAccount).not.toHaveBeenCalled();
  });

  it("sends the old Edit profile and Sign-in connections pages to Settings", () => {
    expect(() => movedLoader()).toThrow();
    try {
      movedLoader();
    } catch (thrown) {
      expect(location(thrown)).toBe("/account/settings");
    }
  });
});

describe("Unlink player", () => {
  const unlinkPage = {
    path: "/account/players/:tag/unlink",
    Component: UnlinkRoute,
    loader: unlinkLoader as never,
    action: unlinkAction as never,
  };
  const path = "/account/players/%232PP/unlink";

  it("says calmly what changes and what stays before anything is unlinked", async () => {
    const html = await render(unlinkPage, new Request(`${ORIGIN}${path}`));
    for (const text of [
      "Unlink Nova?",
      "#2PP",
      ">What changes</h2>",
      "Nova is no longer on your Clash Lens account or your profile.",
      "Discord alerts, the daily summary and your Stream overlay stop for Nova.",
      "Nova leaves the crews it&#x27;s in.",
      ">What stays</h2>",
      "Nova itself, with its Legend days, rankings and history.",
      "You can link it again any time with its API token.",
      'class="button button-primary">Unlink Nova</button>',
      'href="/account/settings">Cancel</a>',
    ])
      expect(html).toContain(text);
    expect(html).not.toContain("danger");
    // Only the default player's page says the bot moves on to the next one.
    expect(html).not.toContain("/stats uses your next player");
    const defaultPage = await render(
      unlinkPage,
      new Request(`${ORIGIN}/account/players/%238PY/unlink`),
    );
    expect(defaultPage).toContain("Unlink #8PY?");
    expect(defaultPage).toContain("/stats uses your next player instead.");
    // An only player is the default too, but no player is left to take over.
    client.getAccountSummary.mockResolvedValue({
      ...SUMMARY,
      verifiedPlayers: [{ tag: "#8PY", name: null }],
    });
    const onlyPage = await render(
      unlinkPage,
      new Request(`${ORIGIN}/account/players/%238PY/unlink`),
    );
    expect(onlyPage).toContain("Unlink #8PY?");
    expect(onlyPage).not.toContain("/stats uses your next player");
    expect(mocks.unlinkPlayer).not.toHaveBeenCalled();
  });

  it("sends a player that isn't theirs back to Settings", async () => {
    await expect(
      render(unlinkPage, new Request(`${ORIGIN}/account/players/%239QQ/unlink`)),
    ).rejects.toSatisfy((thrown) => location(thrown) === "/account/settings");
  });

  it("unlinks it and says so on Settings", async () => {
    const response = (await unlinkAction({
      params: { tag: "#2PP" },
      request: post(path, { idempotencyKey: KEY }),
    } as never)) as Response;
    expect(mocks.unlinkPlayer).toHaveBeenCalledWith(IDENTITY, "#2PP", KEY);
    expect(response.headers.get("Location")).toBe(
      "/account/settings?saved=unlinked&tag=%232PP",
    );
  });

  it("keeps the page with a fresh key when unlinking fails", async () => {
    mocks.unlinkPlayer.mockRejectedValue(
      new PythonApiError(503, { error: "unavailable" }),
    );
    const html = await render(unlinkPage, post(path, { idempotencyKey: KEY }));
    expect(html).toContain("Nova wasn&#x27;t unlinked. Try again.");
    expect(html).not.toContain(`value="${KEY}"`);
  });

  it("goes back to Settings when the player was already unlinked", async () => {
    mocks.unlinkPlayer.mockRejectedValue(
      new PythonApiError(404, { error: "player_not_verified" }),
    );
    await expect(
      unlinkAction({
        params: { tag: "#2PP" },
        request: post(path, { idempotencyKey: KEY }),
      } as never),
    ).rejects.toSatisfy((thrown) => location(thrown) === "/account/settings");
  });

  it("refuses another site, a missing key or a bad tag before unlinking", async () => {
    const hostile = (await unlinkAction({
      params: { tag: "#2PP" },
      request: post(path, { idempotencyKey: KEY }, { Origin: "https://evil.example" }),
    } as never)) as Response;
    expect(hostile.status).toBe(403);
    const noKey = (await unlinkAction({
      params: { tag: "#2PP" },
      request: post(path, { idempotencyKey: "nope" }),
    } as never)) as Response;
    expect(noKey.status).toBe(400);
    const badTag = (await unlinkAction({
      params: { tag: "not a tag" },
      request: post(path, { idempotencyKey: KEY }),
    } as never)) as Response;
    expect(badTag.status).toBe(400);
    expect(mocks.unlinkPlayer).not.toHaveBeenCalled();
  });
});
