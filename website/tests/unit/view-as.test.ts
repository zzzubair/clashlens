import { loginConfig } from "../fixtures/login-config";
import { CALENDAR } from "../fixtures/player-route-data";
vi.mock("../../app/services/python-transport.server", () => ({
  requestJson: mocks.requestJson,
}));
vi.mock("../../app/services/leaderboard.server", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../app/services/leaderboard.server")>();
  return {
    ...actual,
    getTrackedLeaderboard: async (
      ...args: Parameters<typeof actual.getTrackedLeaderboard>
    ) => mocks.createPythonClient().getTrackedLeaderboard(...args),
  };
});
vi.mock("../../app/services/players.server", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../app/services/players.server")>();
  return {
    ...actual,
    getPlayer: async (...args: Parameters<typeof actual.getPlayer>) =>
      mocks.createPythonClient().getPlayer(...args),
    getPlayerSeasons: async (...args: Parameters<typeof actual.getPlayerSeasons>) =>
      mocks.createPythonClient().getPlayerSeasons(...args),
  };
});
vi.mock("../../app/services/public-user.server", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../app/services/public-user.server")>();
  return {
    ...actual,
    getPublicUser: async (...args: Parameters<typeof actual.getPublicUser>) =>
      mocks.createPythonClient().getPublicUser(...args),
  };
}); /**
 * View as: the site owner, and only the owner, opens the dashboard as any
 * player, read only. The Python service decides who the owner is; these
 * tests stand in for its answer and prove what the website does with it.
 */
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createRequestHandler,
  createStaticHandler,
  createStaticRouter,
  RouterContextProvider,
  StaticRouterProvider,
  type ServerBuild,
} from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { LinkedPlayerCard } from "../../app/lib/account-contracts";
import { sanitizeEvent } from "../../app/lib/analytics";
import { middleware as rootMiddleware } from "../../app/root";

const mocks = vi.hoisted(() => ({
  getWebsiteConfig: vi.fn(),
  readLoginIdentity: vi.fn(),
  requireLogin: vi.fn(),
  createPythonClient: vi.fn(),
  requestJson: vi.fn(),
}));

vi.mock("../../app/server/config.server", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/server/config.server")>();
  return { ...actual, getWebsiteConfig: mocks.getWebsiteConfig };
});

vi.mock("../../app/server/actions.server", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/server/actions.server")>();
  return { ...actual, readLoginIdentity: mocks.readLoginIdentity };
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
  };
});

import routes from "../../app/routes";
import { action as ownerAction } from "../../app/routes/account.view-as";
import type { DashboardLoaderData } from "../../app/lib/dashboard-data";
import DashboardRoute, {
  action,
  headers,
  loader,
  shouldRevalidate,
} from "../../app/routes/dashboard";
import { readViewAs } from "../../app/server/view-as.server";
import { PythonApiError } from "../../app/services/python-response.server";

const ORIGIN = "https://clashlens.example";
const OWNER = { provider: "google", providerSubject: "11223344556677889900" } as const;
const SOMEONE = { provider: "discord", providerSubject: "998877665544332211" } as const;
const MINE = "#LQ2V8PJ0";
const VIEWED = "#2PP8QLY0";

function card(tag: string, name: string): LinkedPlayerCard {
  return {
    tag,
    name,
    clan: null,
    state: "tracking",
    reason: null,
    trophies: 5400,
    seasonResetPending: false,
    rank: 12,
    league: null,
    today: { net: 30, attacks: 3, defenses: 2 },
  };
}

/** The viewed player's card as the Python service sends it. */
const VIEWED_PAYLOAD = {
  tag: VIEWED,
  name: "Curtypie",
  clan: null,
  state: "tracking",
  reason: null,
  trophies: 5400,
  season_reset_pending: false,
  rank: 12,
  league: null,
  today: { net: 30, attacks: 3, defenses: 2 },
};

/** Who the Python service says is the owner, and the players it knows. */
let ownerSubject: string | null;
/** The owner's pinned player and own Discord switches, as Python saved them. */
let pinnedTag: string | null;
let follow: { attack: boolean; day_summary: boolean };
let client: Record<string, ReturnType<typeof vi.fn>>;

// Pages are read inside the fixtures' Legend day, so none shows yesterday as over.
afterEach(() => {
  vi.useRealTimers();
});

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"], now: Date.parse(CALENDAR.as_of) });
  vi.stubEnv("CLASHLENS_DASHBOARD_ENABLED", "true");
  mocks.getWebsiteConfig.mockReturnValue(loginConfig(ORIGIN));
  ownerSubject = OWNER.providerSubject;
  pinnedTag = null;
  follow = { attack: false, day_summary: false };
  mocks.readLoginIdentity.mockReset();
  mocks.readLoginIdentity.mockResolvedValue(SOMEONE);
  mocks.requireLogin.mockResolvedValue(SOMEONE);
  mocks.requestJson.mockReset();
  // The Python service's answer: the owner check first, from the signed-in
  // account alone; then, for the owner only, the player the tag names.
  mocks.requestJson.mockImplementation(
    async (
      path: string,
      { method, identity }: { method: string; identity?: typeof OWNER },
    ) => {
      if (!path.startsWith("/v1/account/view-as"))
        throw new PythonApiError(503, { error: "unavailable" });
      const owner = identity?.providerSubject === ownerSubject;
      // The owner's pin and follow: anyone else is refused one way.
      if (method === "POST") {
        if (!owner) throw new PythonApiError(404, { error: "not_found" });
        return {};
      }
      if (!owner) return { kind: "view-as", owner: false, player: null };
      const tag = new URLSearchParams(path.split("?")[1] ?? "").get("tag");
      return {
        kind: "view-as",
        owner: true,
        player: tag === VIEWED ? VIEWED_PAYLOAD : null,
        pinned: pinnedTag,
        follow: tag === VIEWED ? follow : { attack: false, day_summary: false },
        calendar: CALENDAR,
      };
    },
  );
  const unavailable = async () => {
    throw new PythonApiError(503, { error: "unavailable" });
  };
  client = {
    getAccount: vi.fn(async () => ({
      username: "nova88",
      displayName: "Nova",
      providers: ["google"],
      preferences: { dashboard: { v: 3, layout: ["goal", "group-watch"], goal: 50 } },
    })),
    getPublicUser: vi.fn(async () => ({
      username: "nova88",
      displayName: "Nova",
      verifiedPlayers: [card(MINE, "Lens Main")],
      calendar: CALENDAR,
    })),
    updateAccount: vi.fn(async () => ({})),
    getPlayer: vi.fn(unavailable),
    getPlayerSeasons: vi.fn(unavailable),
    getTrackedLeaderboard: vi.fn(unavailable),
    listGroups: vi.fn(unavailable),
  };
  mocks.createPythonClient.mockReturnValue(client);
});

function unwrap(result: unknown) {
  const wrapped = result as {
    data: DashboardLoaderData;
    init: { headers?: Record<string, string> } | null;
  };
  return { data: wrapped.data, headers: new Headers(wrapped.init?.headers) };
}

async function load(url: string, init?: RequestInit) {
  const result = await loader({
    request: new Request(url, init),
    params: {},
    context: {},
  } as unknown as Parameters<typeof loader>[0]);
  const { data, headers } = unwrap(result);
  // Each load makes a new save key; nothing else may differ between two loads.
  const shown = data.kind === "signed-in" ? { ...data, idempotencyKey: "" } : data;
  return { data: shown, headers };
}

const viewUrl = (tag: string) => `${ORIGIN}/?${new URLSearchParams({ view_as: tag })}`;

describe("who can View as", () => {
  it("shows a signed-out visitor the front page whatever asks for View as", async () => {
    mocks.readLoginIdentity.mockResolvedValue(null);
    const plain = await load(`${ORIGIN}/`);
    const attempts: [string, RequestInit?][] = [
      [viewUrl(VIEWED)],
      [`${ORIGIN}/`, { headers: { "X-View-As": VIEWED, "View-As": VIEWED } }],
      [`${ORIGIN}/`, { headers: { Cookie: `view_as=${encodeURIComponent(VIEWED)}` } }],
    ];
    for (const [url, init] of attempts)
      expect((await load(url, init)).data).toEqual(plain.data);
    expect(plain.data.kind).toBe("signed-out");
    expect(mocks.requestJson).not.toHaveBeenCalledWith(
      expect.stringContaining("/v1/account/view-as"),
      {
        method: expect.anything(),
        body: expect.anything(),
        expectedKind: expect.anything(),
        requestId: expect.anything(),
        identity: expect.anything(),
      },
    );
  });

  it("gives any other account its own dashboard, the same for every tag and channel", async () => {
    const plain = await load(`${ORIGIN}/`);
    expect(plain.data).toMatchObject({
      kind: "signed-in",
      selectedTag: MINE,
      canViewAs: false,
      viewAs: null,
    });
    // An existing player, an unknown one and a malformed tag, by the address,
    // a header, a cookie or a form field: never another player's numbers,
    // and nothing that says which tags exist or who the owner is.
    for (const attempt of [
      viewUrl(VIEWED),
      viewUrl("#9999999"),
      viewUrl("not a tag"),
      viewUrl(""),
    ])
      expect((await load(attempt)).data).toEqual(plain.data);
    const channels: RequestInit[] = [
      { headers: { "X-View-As": VIEWED } },
      { headers: { Cookie: `view_as=${encodeURIComponent(VIEWED)}` } },
    ];
    for (const init of channels)
      expect((await load(`${ORIGIN}/`, init)).data).toEqual(plain.data);
    expect(client.getPlayer).not.toHaveBeenCalledWith(VIEWED);
  });

  it("saves a posted view_as field only as the signed-in account's own settings", async () => {
    const answer = await action({
      request: new Request(`${ORIGIN}/`, {
        method: "POST",
        headers: { Origin: ORIGIN },
        body: new URLSearchParams({
          idempotencyKey: "3be934b5-68fa-4741-8c7b-e03592e4ad70",
          settings: JSON.stringify({ v: 3, layout: "default" }),
          view_as: VIEWED,
        }),
      }),
      params: {},
      context: {},
    } as unknown as Parameters<typeof action>[0]);
    expect(JSON.stringify(answer)).not.toContain(VIEWED);
    expect(mocks.createPythonClient).toHaveBeenCalledWith(SOMEONE);
    expect(client.updateAccount).toHaveBeenCalledWith(
      expect.objectContaining({ username: "nova88" }),
      expect.any(String),
    );
    expect(mocks.requestJson).not.toHaveBeenCalled();
    expect(client.getPlayer).not.toHaveBeenCalled();
  });

  it("lets nobody View as while no owner is recorded", async () => {
    ownerSubject = null;
    mocks.readLoginIdentity.mockResolvedValue(OWNER);
    const plain = await load(`${ORIGIN}/`);
    expect(plain.data).toMatchObject({
      canViewAs: false,
      viewAs: null,
      selectedTag: MINE,
    });
    expect((await load(viewUrl(VIEWED))).data).toEqual(plain.data);
  });

  it("treats a failed or odd owner check as not the owner", async () => {
    mocks.requestJson.mockRejectedValue(
      new PythonApiError(503, { error: "unavailable" }),
    );
    expect(await readViewAs(OWNER, VIEWED)).toEqual({ owner: false });
    for (const payload of [
      { kind: "view-as", owner: "true", player: VIEWED_PAYLOAD },
      {
        kind: "view-as",
        owner: true,
        player: { ...VIEWED_PAYLOAD, tag: MINE },
      },
      { kind: "view-as", owner: true, player: { tag: VIEWED } },
      // An owner answer without the day it was read in.
      {
        kind: "view-as",
        owner: true,
        player: VIEWED_PAYLOAD,
        pinned: null,
        follow: { attack: false, day_summary: false },
      },
    ]) {
      mocks.requestJson.mockResolvedValue(payload);
      expect(await readViewAs(OWNER, VIEWED)).toEqual({ owner: false });
    }
  });

  it("sends Python only a well-formed tag, with or without its #", async () => {
    mocks.requestJson.mockResolvedValue({
      kind: "view-as",
      owner: false,
      player: null,
    });
    await readViewAs(OWNER, " 2pp8qly0 ");
    await readViewAs(OWNER, "<script>");
    await readViewAs(OWNER, null);
    expect(mocks.requestJson.mock.calls.map(([path]) => path)).toEqual([
      `/v1/account/view-as?tag=${encodeURIComponent(VIEWED)}`,
      "/v1/account/view-as",
      "/v1/account/view-as",
    ]);
    // Always as the signed-in account, never as anyone the address names.
    for (const call of mocks.requestJson.mock.calls)
      expect(call[1].identity).toEqual(OWNER);
  });
});

describe("the owner's View as", () => {
  beforeEach(() => mocks.readLoginIdentity.mockResolvedValue(OWNER));

  it("shows the viewed player's cards in the owner's own layout, without the owner's Watchlists", async () => {
    const { data, headers } = await load(viewUrl("2pp8qly0"));
    expect(data).toMatchObject({
      kind: "signed-in",
      canViewAs: true,
      viewAs: { tag: VIEWED, name: "Curtypie" },
      players: [card(VIEWED, "Curtypie")],
      selectedTag: VIEWED,
      // The owner's own settings: never the viewed player's account's.
      settings: { layout: ["goal", "group-watch"], goal: 50 },
    });
    if (data.kind !== "signed-in") throw new Error("expected signed in");
    expect(Object.keys(data.cards)).toEqual(["goal"]);
    await Promise.all(Object.values(data.cards));
    expect(client.getPlayer).toHaveBeenCalledWith(VIEWED, true, 50);
    expect(client.getPlayer).not.toHaveBeenCalledWith(MINE);
    expect(client.listGroups).not.toHaveBeenCalled();
    // Python is asked as the owner's own account.
    expect(mocks.createPythonClient).toHaveBeenCalledWith(OWNER);
    expect(headers.get("Cache-Control")).toBe("private, no-store");
  });

  it("expires the viewed card at View as's own Reset when the owner's players read fails", async () => {
    client.getPublicUser.mockRejectedValue(
      new PythonApiError(503, { error: "unavailable" }),
    );
    const { data } = await load(viewUrl(VIEWED));
    expect(data).toMatchObject({ selectedTag: VIEWED });
    if (data.kind !== "signed-in") throw new Error("expected the dashboard");
    expect(data.dayEndsMs).toBe(Date.parse(CALENDAR.day_end));
  });

  it("says a plain Player not found for an unknown or malformed tag", async () => {
    for (const tag of ["#9999999", "not a tag", ""]) {
      const { data } = await load(viewUrl(tag));
      expect(data).toMatchObject({ viewAs: "not-found", players: [], cards: {} });
    }
  });

  it("offers the owner, and only the owner, the View as box", async () => {
    const own = await load(`${ORIGIN}/`);
    expect(own.data).toMatchObject({ canViewAs: true, viewAs: null, selectedTag: MINE });
    expect(await page(own.data)).toContain('name="view_as"');
    mocks.readLoginIdentity.mockResolvedValue(SOMEONE);
    const theirs = await page((await load(viewUrl(VIEWED))).data, viewUrl(VIEWED));
    // The same page as without view_as: not even its links carry the tag on.
    expect(theirs).toBe(await page((await load(`${ORIGIN}/`)).data));
    expect(theirs).not.toContain('name="view_as"');
    expect(theirs).not.toContain("Viewing as");
    expect(theirs).not.toContain(VIEWED.slice(1));
  });

  it("marks View as read only, with Exit and no Customise or Link player", async () => {
    const html = await page((await load(viewUrl(VIEWED))).data);
    expect(text(html)).toContain("Viewing as Curtypie (#2PP8QLY0) – read only");
    expect(html).toContain('href="/"');
    expect(html).toContain("Exit");
    expect(html).not.toContain("Customise");
    expect(html).not.toContain("Link player");
    expect(text(html)).toContain("Your Watchlists stay off while you view as");
    const missing = await page((await load(viewUrl("#9999999"))).data);
    expect(text(missing)).toContain("Player not found");
  });

  it("reads the cards again when View as starts, changes or ends", () => {
    const at = (search: string) => new URL(`${ORIGIN}/${search}`);
    const revalidates = (from: string, to: string) =>
      shouldRevalidate({
        currentUrl: at(from),
        nextUrl: at(to),
        defaultShouldRevalidate: false,
      } as unknown as Parameters<typeof shouldRevalidate>[0]);
    expect(revalidates("", "?view_as=%232PP8QLY0")).toBe(true);
    expect(revalidates("?view_as=%232PP8QLY0", "?view_as=%23LQ2V8PJ0")).toBe(true);
    expect(revalidates("?view_as=%232PP8QLY0", "")).toBe(true);
  });
});

describe("nothing leaks or changes from View as", () => {
  it("keeps every dashboard answer out of shared caches", () => {
    expect(headers()).toEqual({ "Cache-Control": "private, no-store" });
  });

  it("never sends View as to analytics", () => {
    const event = sanitizeEvent({
      event: "$pageview",
      properties: { $current_url: `${ORIGIN}/?view_as=%232PP8QLY0` },
    } as unknown as Parameters<typeof sanitizeEvent>[0]);
    expect(JSON.stringify(event)).not.toContain("view_as");
    expect(JSON.stringify(event)).not.toContain("2PP8QLY0");
  });

  it("refuses View as on every request but a read of the dashboard", async () => {
    // Every route the site has, read from routes.ts, so a new one is checked too.
    const ran: string[] = [];
    const built: Record<string, unknown> = {
      root: {
        id: "root",
        path: "",
        module: { default: () => null, middleware: rootMiddleware },
      },
    };
    const paths: string[] = [];
    const writes: string[] = [];
    for (const entry of routes) {
      const file = `../../app/${entry.file}`;
      const module = (await import(/* @vite-ignore */ file)) as Record<string, unknown>;
      const id = entry.id ?? entry.file;
      const path = entry.index
        ? "/"
        : `/${entry.path}`.replace(/:[A-Za-z]+/g, "a1").replace("*", "a1");
      paths.push(path);
      const spy = (kind: string) => () => {
        ran.push(`${kind} ${path}`);
        return { ran: true };
      };
      const stub: Record<string, unknown> = { default: () => null };
      if (typeof module.loader === "function") stub.loader = spy("load");
      if (typeof module.action === "function") {
        stub.action = spy("act");
        writes.push(path);
      }
      built[id] = {
        id,
        parentId: "root",
        path: entry.path,
        index: entry.index,
        module: stub,
      };
    }
    // The routes that change things today, so the walk cannot quietly find none.
    expect(writes).toEqual(
      expect.arrayContaining([
        "/",
        "/account/settings",
        "/account/players/a1/unlink",
        "/account/discord-alerts",
        "/account/view-as",
        "/account/obs-overlay/save",
        "/account/verify-player",
        "/account/watchlists",
        "/crews/new",
        "/crews/a1",
        "/logout",
      ]),
    );
    // Pages whose load signs in or starts work count too.
    expect(paths).toEqual(
      expect.arrayContaining(["/auth/discord/callback", "/auth/google/callback"]),
    );
    const handle = createRequestHandler({
      entry: { module: { default: () => new Response("page") } },
      routes: built,
      assets: { version: "test", routes: {}, entry: { module: "" }, url: "" },
      future: {},
      ssr: true,
      isSpaMode: false,
      prerender: [],
      publicPath: "/",
      assetsBuildDirectory: "build",
      routeDiscovery: { mode: "initial", manifestPath: "/__manifest" },
    } as unknown as ServerBuild);
    const send = (path: string, search: string, method: string, data: boolean) => {
      const address = data ? (path === "/" ? "/_.data" : `${path}.data`) : path;
      const query = path === "/" ? `?index${search ? `&${search}` : ""}` : `?${search}`;
      const reads = method === "GET" || method === "HEAD";
      return handle(
        new Request(`${ORIGIN}${address}${query}`, {
          method,
          body: reads ? undefined : new URLSearchParams({ idempotencyKey: "x" }),
        }),
        new RouterContextProvider(),
      );
    };
    const viewing = "view_as=%232PP8QLY0";
    for (const path of paths)
      for (const method of ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
        for (const data of [false, true]) {
          if (path === "/" && (method === "GET" || method === "HEAD")) continue;
          const refused = await send(path, viewing, method, data);
          expect(refused.status, `${method} ${path}`).toBe(400);
          expect(refused.headers.get("Cache-Control")).toBe("private, no-store");
        }
    expect(ran).toEqual([]);
    // The dashboard's own read is the one thing that takes it.
    for (const data of [false, true]) await send("/", viewing, "GET", data);
    expect(ran).toEqual(["load /", "load /"]);
    // Without View as, each change reaches its own action.
    ran.length = 0;
    for (const path of writes) await send(path, "", "POST", true);
    expect(ran).toEqual(writes.map((path) => `act ${path}`));
  });
});

describe("the owner's pin and own Discord alerts", () => {
  beforeEach(() => mocks.readLoginIdentity.mockResolvedValue(OWNER));

  /** The loader's redirect for a plain load, or null when it shows a page. */
  async function redirected(url: string) {
    try {
      await load(url);
      return null;
    } catch (thrown) {
      if (!(thrown instanceof Response)) throw thrown;
      return thrown;
    }
  }

  it("opens the pinned player through View as on a plain load only", async () => {
    pinnedTag = VIEWED;
    const plain = await redirected(`${ORIGIN}/`);
    expect(plain?.status).toBe(302);
    expect(plain?.headers.get("Location")).toBe("/?view_as=%232PP8QLY0");
    expect(plain?.headers.get("Cache-Control")).toBe("private, no-store");
    // The owner's own dashboard, its tabs and card reads stay where they are.
    for (const search of [
      "?mine=1",
      `?player=${encodeURIComponent(MINE)}`,
      "?cards=goal",
    ])
      expect(await redirected(`${ORIGIN}/${search}`), search).toBeNull();
    const own = await load(`${ORIGIN}/?mine=1`);
    expect(own.data).toMatchObject({
      selectedTag: MINE,
      viewAs: null,
      ownerChoices: { pinned: VIEWED, saved: null, discord: false },
    });
    const pinned = await load(viewUrl(VIEWED));
    expect(pinned.data).toMatchObject({
      viewAs: { tag: VIEWED },
      players: [card(VIEWED, "Curtypie")],
    });
    pinnedTag = null;
    expect(await redirected(`${ORIGIN}/`)).toBeNull();
  });

  it("never redirects or shows the owner's choices to anyone else", async () => {
    mocks.readLoginIdentity.mockResolvedValue(SOMEONE);
    pinnedTag = VIEWED;
    const plain = await load(`${ORIGIN}/`);
    expect(plain.data).toMatchObject({ selectedTag: MINE, ownerChoices: null });
    for (const search of ["?saved=saved", "?saved=failed", "?mine=1"])
      expect((await load(`${ORIGIN}/${search}`)).data).toEqual(plain.data);
    // A not-owner answer carrying a pin or switches is still not the owner.
    mocks.requestJson.mockResolvedValue({
      kind: "view-as",
      owner: false,
      player: null,
      pinned: VIEWED,
      follow: { attack: true, day_summary: true },
    });
    expect(await readViewAs(SOMEONE, null)).toEqual({ owner: false });
    expect((await load(`${ORIGIN}/`)).data).toEqual(plain.data);
  });

  it("shows Pin and the owner's own switches in the View as bar", async () => {
    const unpinned = await page((await load(viewUrl(VIEWED))).data);
    expect(text(unpinned)).toContain("Pin Attack alerts Day summary");
    expect(unpinned.match(/aria-checked="false"/g)).toHaveLength(2);
    expect(unpinned).toContain('action="/account/view-as"');
    expect(unpinned).toContain('href="/"');
    expect(unpinned).not.toContain("My dashboard");
    expect(text(unpinned)).toContain("Connect Discord to get these.");

    pinnedTag = VIEWED;
    follow = { attack: true, day_summary: false };
    const pinned = await page((await load(`${viewUrl(VIEWED)}&saved=saved`)).data);
    expect(text(pinned)).toContain("Saved.");
    expect(text(pinned)).toContain("Unpin");
    expect(pinned).toMatch(/aria-checked="true" aria-label="Attack alerts for #2PP8QLY0/);
    expect(pinned).toContain('href="/?mine=1"');
    expect(text(pinned)).toContain("My dashboard");
    // Exit would only open the pinned player again.
    expect(text(pinned)).not.toContain("Exit");

    pinnedTag = MINE;
    const other = await page((await load(viewUrl(VIEWED))).data);
    expect(text(other)).toContain("Exit");
    expect(text(other)).toContain("My dashboard");
    const own = await page((await load(`${ORIGIN}/?mine=1`)).data);
    expect(text(own)).toContain(`Pinned: ${MINE}`);
    expect(own).toContain('href="/?view_as=%23LQ2V8PJ0"');
    expect(own).not.toContain("Attack alerts");
  });
});

describe("the owner's choices are saved for the owner alone", () => {
  const KEY = "3be934b5-68fa-4741-8c7b-e03592e4ad70";
  const post = (fields: Record<string, string>, init: RequestInit = {}) =>
    ownerAction({
      request: new Request(`${ORIGIN}/account/view-as`, {
        method: "POST",
        ...init,
        headers: { Origin: ORIGIN, ...init.headers },
        body: new URLSearchParams({ idempotencyKey: KEY, ...fields }),
      }),
      params: {},
      context: {},
    } as unknown as Parameters<typeof ownerAction>[0]);
  const saves = () =>
    mocks.requestJson.mock.calls.map(
      ([path, { method, body, requestId: key, identity }]) => ({
        path,
        method,
        body: JSON.parse((body as Buffer).toString("utf8")) as unknown,
        key,
        identity,
      }),
    );
  const CHOICES: Record<string, string>[] = [
    { intent: "pin" },
    { intent: "unpin" },
    { intent: "attack", on: "on" },
    { intent: "day_summary", on: "off" },
  ];

  it("saves the owner's pin and switches as the signed-in owner", async () => {
    mocks.requireLogin.mockResolvedValue(OWNER);
    const answers = [];
    for (const choice of CHOICES)
      answers.push(await post({ ...choice, tag: "2pp8qly0", view_as: MINE }));
    for (const answer of answers) {
      expect(answer.status).toBe(302);
      expect(answer.headers.get("Location")).toBe("/?view_as=%232PP8QLY0&saved=saved");
    }
    expect(saves()).toEqual(
      [
        { path: "/v1/account/view-as/pin", body: { tag: VIEWED } },
        { path: "/v1/account/view-as/pin", body: { tag: null } },
        {
          path: "/v1/account/view-as/follow",
          body: { tag: VIEWED, alert: "attack", on: true },
        },
        {
          path: "/v1/account/view-as/follow",
          body: { tag: VIEWED, alert: "day_summary", on: false },
        },
      ].map((save) => ({ ...save, method: "POST", key: KEY, identity: OWNER })),
    );
  });

  it("changes nothing for anyone else, the same way whatever the tag", async () => {
    for (const tag of [VIEWED, "#9999999", MINE]) {
      for (const choice of CHOICES) {
        const answer = await post(
          { ...choice, tag },
          { headers: { "X-View-As": VIEWED, Cookie: "view_as=%232PP8QLY0" } },
        );
        expect(answer.status).toBe(302);
        expect(answer.headers.get("Location")).toBe(
          `/?${new URLSearchParams({ view_as: tag, saved: "failed" })}`,
        );
      }
    }
    // Python decided, as the signed-in account, and refused each one.
    expect(new Set(saves().map((save) => save.identity))).toEqual(new Set([SOMEONE]));
  });

  it("refuses signed-out, cross-site and malformed requests before asking Python", async () => {
    mocks.requireLogin.mockRejectedValue(new Response(null, { status: 302 }));
    await expect(post({ intent: "pin", tag: VIEWED })).rejects.toBeInstanceOf(Response);
    mocks.requireLogin.mockResolvedValue(OWNER);
    const crossSite = await post(
      { intent: "pin", tag: VIEWED },
      { headers: { Origin: "https://evil.example" } },
    );
    expect(crossSite.status).toBe(403);
    const malformed: Record<string, string>[] = [
      { intent: "pin", tag: VIEWED, idempotencyKey: "x" },
      { intent: "promote", tag: VIEWED },
      { intent: "pin", tag: "not a tag" },
      { intent: "attack", tag: VIEWED },
      { intent: "attack", tag: VIEWED, on: "maybe" },
    ];
    for (const fields of malformed) expect((await post(fields)).status).toBe(400);
    expect(mocks.requestJson).not.toHaveBeenCalled();
  });
});

/** The dashboard page as it renders for `data`. */
async function page(data: DashboardLoaderData, url = `${ORIGIN}/`) {
  const handler = createStaticHandler([
    { path: "/", loader: () => data, Component: DashboardRoute },
  ]);
  const context = await handler.query(new Request(url));
  if (context instanceof Response) throw new Error("unexpected response");
  return renderToString(
    createElement(StaticRouterProvider, {
      router: createStaticRouter(handler.dataRoutes, context),
      context,
      hydrate: false,
    }),
  );
}

const text = (html: string) =>
  html
    .replace(/<[^>]+>/g, " ")
    .replace(/&#x27;/g, "'")
    .replace(/\s+/g, " ")
    .replace(/ \)/g, ")")
    .replace(/\( /g, "(");
