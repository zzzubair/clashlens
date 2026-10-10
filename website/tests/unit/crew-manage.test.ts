import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  RouterContextProvider,
  StaticRouterProvider,
} from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getWebsiteConfig: vi.fn(),
  requireLogin: vi.fn(),
  requestJson: vi.fn(),
  createPythonClient: vi.fn(),
  checkPlayerTag: vi.fn(),
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
    requestJson: mocks.requestJson,
    createPythonClient: mocks.createPythonClient,
  };
});

vi.mock("../../app/services/group-players.server", () => ({
  checkPlayerTag: mocks.checkPlayerTag,
}));

import {
  crewRefusal,
  formatInviteExpiry,
  mapCrew,
  mapInvitePreview,
} from "../../app/lib/crew-contracts";
import { action as crewAction } from "../../app/routes/crews.$crewId";
import { action as membersAction } from "../../app/routes/crews.$crewId.members";
import CrewSettingsRoute, {
  action as settingsAction,
  loader as settingsLoader,
} from "../../app/routes/crews.$crewId.settings";
import JoinRoute, {
  action as joinAction,
  loader as joinLoader,
} from "../../app/routes/crews.join.$code";
import {
  action as verifyAction,
  loader as verifyLoader,
} from "../../app/routes/account.verify-player";
import { clientAddressContext } from "../../app/server/client-address.server";
import { loadWebsiteConfig } from "../../app/server/config.server";
import { PythonApiError } from "../../app/services/python.server";

const ORIGIN = "https://clashlens.example";
const IDENTITY = { provider: "google", providerSubject: "11223344556677889900" } as const;
const KEY = "3be934b5-68fa-4741-8c7b-e03592e4ad70";
const CREW_ID = "6f1c2b9e-4a7d-4c1e-9b2a-0d5e8f7a6b3c";
const INVITE_ID = "0b6f2d3e-1c4a-4f5b-8e9d-7a6c5b4d3e2f";
const CODE = "h3Kq8Zp2vT_x-Yb1Wc9Dd0";
const EXPIRES = "2026-10-19T14:20:00+00:00";

type Args = { request: Request; params: Record<string, string>; context: unknown };

function post(path: string, fields: Record<string, string>, params = {}): Args {
  return {
    request: new Request(`${ORIGIN}${path}`, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded", Origin: ORIGIN },
      body: new URLSearchParams({ idempotencyKey: KEY, ...fields }).toString(),
    }),
    params,
    context: { get: () => undefined },
  };
}

const get = (path: string, params: Record<string, string> = {}): Args => ({
  request: new Request(`${ORIGIN}${path}`),
  params,
  context: { get: () => undefined },
});

function unwrap<T>(result: unknown): { data: T; status: number } {
  const wrapped = result as { data: T; init: { status?: number } | null };
  return { data: wrapped.data, status: wrapped.init?.status ?? 200 };
}

const thrown = (run: () => Promise<unknown>) =>
  run().then(
    () => null,
    (error: unknown) => error,
  );

type Answer = {
  idempotencyKey: string;
  notice: string | null;
  error: unknown;
  invite: { link: string; code: string; openPlaces: number } | null;
};

function crewPayload(role = "owner") {
  return {
    kind: "crew",
    crew_id: CREW_ID,
    name: "Red Dawn",
    size: 10,
    used: 4,
    my_role: role,
    members: [
      {
        username: "zara",
        display_name: "Zara",
        role,
        you: true,
        players: [{ tag: "#2PP", name: "Zara", trophies: 5390, status: "tracking" }],
      },
      {
        username: "kenji",
        display_name: "Kenji",
        role: "member",
        you: false,
        players: [
          { tag: "#2QQ", name: "Kenji", trophies: null, status: "not_in_legend" },
          { tag: "#2RR", name: null, trophies: 5100, status: "no_battles_this_season" },
          { tag: "#2YY", name: "Alt", trophies: 5000, status: "tracking" },
        ],
      },
    ],
    invites: [
      { invite_id: INVITE_ID, made_by: "Kenji", expires_at: EXPIRES, mine: false },
    ],
  };
}

function invitePayload(overrides: Record<string, unknown> = {}) {
  return {
    kind: "crew-invite",
    state: "ok",
    crew_id: CREW_ID,
    name: "Night Owls",
    owner_display_name: "Kenji",
    size: 30,
    used: 18,
    expires_at: EXPIRES,
    in_crew: false,
    crew_count: 1,
    accounts: [
      { tag: "#2PP", name: "Zara", trophies: 5390, eligibility: "ok" },
      { tag: "#2QQ", name: "Low", trophies: 4100, eligibility: "not_in_legend" },
      { tag: "#2RR", name: "New", trophies: null, eligibility: "unchecked" },
    ],
    ...overrides,
  };
}

async function render(
  path: string,
  route: {
    path: string;
    Component: () => unknown;
    loader: (a: Args) => unknown;
    action?: (a: Args) => unknown;
  },
  request = new Request(`${ORIGIN}${path}`),
) {
  const handler = createStaticHandler([
    {
      path: route.path,
      Component: route.Component as () => null,
      loader: route.loader as never,
      action: route.action as never,
    },
  ]);
  // The server gives every request the clasher's address.
  const requestContext = new RouterContextProvider();
  requestContext.set(clientAddressContext, undefined);
  const context = await handler.query(request, {
    requestContext,
  });
  if (context instanceof Response) throw new Error("unexpected response");
  return renderToString(
    createElement(StaticRouterProvider, {
      router: createStaticRouter(handler.dataRoutes, context),
      context,
      hydrate: false,
    }),
  ).replaceAll("<!-- -->", "");
}

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
  mocks.requestJson.mockReset();
  mocks.requestJson.mockRejectedValue(new PythonApiError(503, { error: "unavailable" }));
  mocks.createPythonClient.mockReset();
  mocks.checkPlayerTag.mockReset();
});

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("refusal messages", () => {
  it.each([
    ["player_not_linked", { tag: "#2PP" }, "#2PP is not linked to your account."],
    [
      "player_not_in_legend",
      { tag: "#2PP" },
      "#2PP is not in Legend League, so it can't join.",
    ],
    [
      "player_not_checked",
      { tag: "#2PP" },
      "Still checking #2PP with Clash of Clans. Try again in a few seconds.",
    ],
    ["player_already_in_crew", { tag: "#2PP" }, "#2PP is already in this crew."],
    ["crew_full", { open_places: 0 }, "The crew is full."],
    ["crew_full", { open_places: 1 }, "Only 1 place is open. Pick fewer accounts."],
    ["crew_full", { open_places: 3 }, "Only 3 places are open. Pick fewer accounts."],
    ["crew_limit_reached", {}, "You're already in 5 crews, the most you can be in."],
    ["crew_forbidden", {}, "Only the owner or an admin can do that."],
    ["crew_not_found", {}, "This crew no longer exists, or you're no longer in it."],
    [
      "crew_size_below_used",
      { used: 37 },
      "37 places are in use. Pick 37 or more, or kick accounts first.",
    ],
    ["crew_size_below_used", {}, "The crew can't have fewer places than accounts in it."],
    ["invalid_crew_size", {}, "Places must be from 2 to 100."],
    ["invalid_crew_name", {}, "Choose a different crew name."],
    ["owner_must_hand_over", {}, "As owner, hand over the crew or delete it first."],
    ["owner_role_fixed", {}, "The owner's role can only change by handing over."],
    ["member_not_found", {}, "That clasher is no longer in this crew."],
    ["player_not_in_crew", {}, "That account is no longer in this crew."],
    ["invite_not_found", {}, "That link is already off or expired."],
    ["invite_invalid", {}, "This invite link has expired or was turned off."],
  ])("%s with %o reads %j", (code, details, message) => {
    expect(crewRefusal(code, details)).toBe(message);
  });

  it("names no account when the refusal doesn't say which, and has no message for other codes", () => {
    expect(crewRefusal("player_not_linked")).toBe(
      "An account is not linked to your account.",
    );
    expect(crewRefusal("unavailable")).toBeNull();
    expect(crewRefusal(null)).toBeNull();
  });
});

describe("crew answers", () => {
  it("reads each account's status and the live links", () => {
    const crew = mapCrew(crewPayload());
    expect(crew?.members[1]?.players.map((player) => player.status)).toEqual([
      "not_in_legend",
      "no_battles_this_season",
      "tracking",
    ]);
    expect(crew?.invites).toEqual([
      { inviteId: INVITE_ID, madeBy: "Kenji", expiresAt: EXPIRES, mine: false },
    ]);
    expect(mapCrew({ ...crewPayload(), invites: [{ invite_id: "x" }] })).toBeNull();
  });

  it("says nothing about the crew behind a link that doesn't work", () => {
    expect(
      mapInvitePreview({
        kind: "crew-invite",
        state: "invalid",
        in_crew: false,
        crew_count: 2,
        accounts: [],
      }),
    ).toEqual({ state: "invalid", inCrew: false, crewCount: 2, accounts: [] });
    expect(mapInvitePreview(invitePayload({ state: "maybe" }))).toBeNull();
  });

  it("shows when a link stops working in UTC", () => {
    expect(formatInviteExpiry(EXPIRES)).toBe("Mon 19 Oct, 14:20 UTC");
  });
});

describe("invite", () => {
  const made = {
    invite_id: INVITE_ID,
    code: CODE,
    expires_at: EXPIRES,
    open_places: 6,
    live_count: 1,
  };

  it("gives the clasher's link on this website, and a new one under its own key", async () => {
    mocks.requestJson.mockResolvedValue(made);
    const same = unwrap<Answer>(
      await crewAction(
        post(`/crews/${CREW_ID}`, { intent: "invite" }, { crewId: CREW_ID }) as never,
      ),
    );
    expect(same.data.invite).toMatchObject({
      link: `${ORIGIN}/crews/join/${CODE}`,
      openPlaces: 6,
    });
    await crewAction(
      post(
        `/crews/${CREW_ID}`,
        { intent: "invite", new: "1" },
        { crewId: CREW_ID },
      ) as never,
    );
    const calls = mocks.requestJson.mock.calls;
    expect(calls.map((call) => [call[0], call[1]])).toEqual([
      [`/v1/account/crews/${CREW_ID}/invites`, "POST"],
      [`/v1/account/crews/${CREW_ID}/invites`, "POST"],
    ]);
    expect(JSON.parse((calls[1]![2] as Buffer).toString())).toEqual({ new: true });
    // Each write gets its own key from the page's, never the page's key itself.
    expect(new Set([KEY, calls[0]![4], calls[1]![4]]).size).toBe(3);
  });

  it("says why no link is made for a full crew", async () => {
    mocks.requestJson.mockRejectedValue(
      new PythonApiError(409, { error: "crew_full", open_places: 0 }),
    );
    const { data, status } = unwrap<Answer>(
      await crewAction(
        post(`/crews/${CREW_ID}`, { intent: "invite" }, { crewId: CREW_ID }) as never,
      ),
    );
    expect([status, data.error, data.invite]).toEqual([409, "The crew is full.", null]);
  });

  it("is hidden while crews are switched off", async () => {
    vi.stubEnv("CLASHLENS_DASHBOARD_ENABLED", "false");
    const response = await thrown(() =>
      crewAction(
        post(`/crews/${CREW_ID}`, { intent: "invite" }, { crewId: CREW_ID }) as never,
      ),
    );
    expect((response as { init: { status: number } }).init.status).toBe(404);
    expect(mocks.requestJson).not.toHaveBeenCalled();
  });
});

describe("members", () => {
  const members = (fields: Record<string, string>) =>
    membersAction(
      post(`/crews/${CREW_ID}/members`, fields, { crewId: CREW_ID }) as never,
    );

  it("kicks one account", async () => {
    mocks.requestJson.mockResolvedValue({ removed: true, tag: "#2QQ", left_crew: false });
    const { data } = unwrap<Answer>(await members({ intent: "remove", tag: "#2qq" }));
    expect(data.notice).toBe("Kicked #2QQ.");
    const [target, method] = mocks.requestJson.mock.calls[0]!;
    expect([target, method]).toEqual([
      `/v1/account/crews/${CREW_ID}/players/%232QQ`,
      "DELETE",
    ]);
  });

  it("leads back to Crews when your last account leaves, or you leave", async () => {
    mocks.requestJson.mockResolvedValue({ removed: true, tag: "#2PP", left_crew: true });
    const removed = await thrown(() =>
      members({ intent: "remove", tag: "#2PP", own: "1" }),
    );
    expect((removed as Response).headers.get("Location")).toBe("/crews");
    mocks.requestJson.mockResolvedValue({ left: true, crew_id: CREW_ID });
    const left = await thrown(() => members({ intent: "leave" }));
    expect((left as Response).headers.get("Location")).toBe("/crews");
    expect(mocks.requestJson.mock.calls[1]!.slice(0, 2)).toEqual([
      `/v1/account/crews/${CREW_ID}/members/me`,
      "DELETE",
    ]);
  });

  it("makes and removes an admin", async () => {
    mocks.requestJson.mockResolvedValue({
      username: "kenji",
      display_name: "Kenji",
      role: "admin",
    });
    const promoted = unwrap<Answer>(
      await members({ intent: "promote", username: "kenji" }),
    );
    expect(promoted.data.notice).toBe("Kenji is now an admin.");
    const demoted = unwrap<Answer>(
      await members({ intent: "demote", username: "kenji" }),
    );
    expect(demoted.data.notice).toBe("Kenji is no longer an admin.");
    expect(
      mocks.requestJson.mock.calls.map((call) =>
        JSON.parse((call[2] as Buffer).toString()),
      ),
    ).toEqual([{ role: "admin" }, { role: "member" }]);
  });

  it("adds your accounts, checking one Clash Lens hasn't checked yet", async () => {
    mocks.requestJson
      .mockRejectedValueOnce(
        new PythonApiError(409, {
          error: "player_not_checked",
          tag: "#2RR",
          state: "unknown",
        }),
      )
      .mockResolvedValueOnce({ crew_id: CREW_ID, tags: ["#2PP", "#2RR"] });
    mocks.checkPlayerTag.mockResolvedValue({ state: "tracking" });
    const { data } = unwrap<Answer>(await members({ intent: "add", "join:#2RR": "on" }));
    expect(data.notice).toBe("Added 1 account.");
    expect(mocks.checkPlayerTag).toHaveBeenCalledWith(undefined, "#2RR");
    const keys = mocks.requestJson.mock.calls.map((call) => call[4]);
    expect(keys[0]).not.toBe(keys[1]);
  });

  it("says why a write was refused, with a fresh key", async () => {
    mocks.requestJson.mockRejectedValue(
      new PythonApiError(403, { error: "crew_forbidden" }),
    );
    const { data, status } = unwrap<Answer>(
      await members({ intent: "remove", tag: "#2QQ" }),
    );
    expect([status, data.error]).toEqual([
      403,
      "Only the owner or an admin can do that.",
    ]);
    expect(data.idempotencyKey).not.toBe(KEY);
  });

  it("keeps the page's key after a lost answer, so the same write replays", async () => {
    const { data, status } = unwrap<Answer>(
      await members({ intent: "remove", tag: "#2QQ" }),
    );
    expect(status).toBe(503);
    expect(data.idempotencyKey).toBe(KEY);
  });

  it("refuses a form from another site or with an unknown intent", async () => {
    const foreign = post(
      `/crews/${CREW_ID}/members`,
      { intent: "leave" },
      { crewId: CREW_ID },
    );
    foreign.request.headers.set("Origin", "https://evil.example");
    expect(unwrap<Answer>(await membersAction(foreign as never)).status).toBe(403);
    expect(unwrap<Answer>(await members({ intent: "delete" })).status).toBe(400);
    expect(mocks.requestJson).not.toHaveBeenCalled();
  });
});

describe("edit crew", () => {
  const settings = (fields: Record<string, string>) =>
    settingsAction(
      post(`/crews/${CREW_ID}/settings`, fields, { crewId: CREW_ID }) as never,
    );

  it("won't make the crew smaller than the places in use", async () => {
    mocks.requestJson.mockRejectedValue(
      new PythonApiError(422, { error: "crew_size_below_used", used: 4 }),
    );
    const { data } = unwrap<Answer>(await settings({ intent: "resize", size: "3" }));
    expect(data.error).toBe(
      "4 places are in use. Pick 4 or more, or kick accounts first.",
    );
    const outside = unwrap<Answer>(await settings({ intent: "resize", size: "101" }));
    expect(outside.data.error).toBe("Places must be a whole number from 2 to 100.");
    expect(mocks.requestJson).toHaveBeenCalledTimes(1);
  });

  it("takes an owner with no places left out to their crews after handing over", async () => {
    mocks.requestJson.mockResolvedValue({ crew_id: CREW_ID, left_crew: true });
    const handed = await thrown(() => settings({ intent: "transfer", username: "kenji" }));
    expect((handed as Response).headers.get("Location")).toBe("/crews");
  });

  it("renames, resizes, turns off a link and hands over", async () => {
    mocks.requestJson.mockResolvedValue({});
    const notices = [];
    for (const fields of <Record<string, string>[]>[
      { intent: "rename", name: "  Blue Moon " },
      { intent: "resize", size: "12" },
      { intent: "revoke", invite: INVITE_ID },
      { intent: "transfer", username: "kenji" },
    ]) {
      notices.push(unwrap<Answer>(await settings(fields)).data.notice);
    }
    expect(notices).toEqual([
      "Name saved.",
      "Size saved: 12 places.",
      "Link turned off.",
      "@kenji owns the crew now.",
    ]);
    expect(
      mocks.requestJson.mock.calls.map((call) => [
        call[0],
        call[1],
        call[2] === undefined ? null : JSON.parse((call[2] as Buffer).toString()),
      ]),
    ).toEqual([
      [`/v1/account/crews/${CREW_ID}`, "PATCH", { name: "Blue Moon" }],
      [`/v1/account/crews/${CREW_ID}`, "PATCH", { size: 12 }],
      [`/v1/account/crews/${CREW_ID}/invites/${INVITE_ID}`, "DELETE", null],
      [`/v1/account/crews/${CREW_ID}/owner`, "POST", { username: "kenji" }],
    ]);
  });

  it("deletes only once the box is ticked", async () => {
    const unticked = unwrap<Answer>(await settings({ intent: "delete" }));
    expect(unticked.data.error).toBe("Tick the box to delete the crew.");
    expect(mocks.requestJson).not.toHaveBeenCalled();
    mocks.requestJson.mockResolvedValue({ deleted: true, crew_id: CREW_ID });
    const deleted = await thrown(() => settings({ intent: "delete", confirm: "on" }));
    expect((deleted as Response).headers.get("Location")).toBe("/crews");
  });

  it("shows a member only who can change settings", async () => {
    mocks.requestJson.mockResolvedValue(crewPayload("member"));
    const html = await render(`/crews/${CREW_ID}/settings`, {
      path: "/crews/:crewId/settings",
      Component: CrewSettingsRoute,
      loader: settingsLoader as never,
    });
    expect(html).toContain("Only the owner and admins can change crew settings.");
    expect(html).not.toContain("Save name");
  });

  it("shows the owner the live links and the size floor", async () => {
    mocks.requestJson.mockResolvedValue(crewPayload("owner"));
    const html = await render(`/crews/${CREW_ID}/settings`, {
      path: "/crews/:crewId/settings",
      Component: CrewSettingsRoute,
      loader: settingsLoader as never,
    });
    expect(html).toContain("Made by Kenji");
    expect(html).toContain('min="4"');
    expect(html).toContain("Delete crew");
  });
});

describe("join by invite", () => {
  const joinRoute = {
    path: "/crews/join/:code",
    Component: JoinRoute,
    loader: joinLoader as never,
  };

  it("answers a malformed code, or crews switched off, as a missing page", async () => {
    const malformed = await thrown(() =>
      joinLoader(get("/crews/join/short", { code: "short" }) as never),
    );
    expect((malformed as { init: { status: number } }).init.status).toBe(404);
    vi.stubEnv("CLASHLENS_DASHBOARD_ENABLED", "false");
    const off = await thrown(() =>
      joinLoader(get(`/crews/join/${CODE}`, { code: CODE }) as never),
    );
    expect((off as { init: { status: number } }).init.status).toBe(404);
    expect(mocks.requestJson).not.toHaveBeenCalled();
  });

  it("checks accounts Clash Lens hasn't checked, then shows who can join", async () => {
    mocks.requestJson.mockResolvedValueOnce(invitePayload()).mockResolvedValueOnce(
      invitePayload({
        accounts: [
          { tag: "#2PP", name: "Zara", trophies: 5390, eligibility: "ok" },
          { tag: "#2QQ", name: "Low", trophies: 4100, eligibility: "not_in_legend" },
          { tag: "#2RR", name: "New", trophies: 5200, eligibility: "ok" },
        ],
      }),
    );
    mocks.checkPlayerTag.mockResolvedValue({ state: "tracking" });
    const html = await render(`/crews/join/${CODE}`, joinRoute);
    expect(mocks.checkPlayerTag).toHaveBeenCalledTimes(1);
    expect(mocks.checkPlayerTag).toHaveBeenCalledWith(undefined, "#2RR");
    expect(html).toContain("Night Owls");
    expect(html).toContain("12 places open");
    expect(html).toContain("Not in Legend League");
    expect(html).toContain('name="join:#2PP" checked=""');
    expect(html).toContain("Join with the picked accounts");
    expect(html).toContain(`/account/verify-player?return=/crews/join/${CODE}`);
  });

  it("keeps Join working without JavaScript after a join with no picks", async () => {
    mocks.requestJson.mockResolvedValue(
      invitePayload({
        accounts: [{ tag: "#2PP", name: "Zara", trophies: 5390, eligibility: "ok" }],
      }),
    );
    const html = await render(
      `/crews/join/${CODE}`,
      { ...joinRoute, action: joinAction as never },
      post(`/crews/join/${CODE}`, {}, { code: CODE }).request,
    );
    expect(html).toContain("Pick at least one account.");
    expect(html).toMatch(/<button type="submit" class="button button-primary">Join with/);
  });

  it.each([
    [{ state: "full" }, "This crew is full."],
    [{ state: "limit", crew_count: 5 }, "You&#x27;re already in 5 crews"],
    [{ accounts: [] }, "Link a Clash of Clans account to join."],
  ])("shows %o as %j", async (overrides, text) => {
    mocks.requestJson.mockResolvedValue(invitePayload(overrides));
    expect(await render(`/crews/join/${CODE}`, joinRoute)).toContain(text);
  });

  it("says a link that doesn't work has expired or was turned off", async () => {
    mocks.requestJson.mockResolvedValue({
      kind: "crew-invite",
      state: "invalid",
      in_crew: false,
      crew_count: 0,
      accounts: [],
    });
    const html = await render(`/crews/join/${CODE}`, joinRoute);
    expect(html).toContain("It expired or was turned off.");
  });

  it("joins with the picked accounts and opens the crew", async () => {
    mocks.requestJson.mockResolvedValue({ crew_id: CREW_ID });
    const response = await thrown(() =>
      joinAction(
        post(
          `/crews/join/${CODE}`,
          { "join:#2PP": "on", "join:#2RR": "on" },
          { code: CODE },
        ) as never,
      ),
    );
    expect((response as Response).headers.get("Location")).toBe(`/crews/${CREW_ID}`);
    const [target, method, body, , key] = mocks.requestJson.mock.calls[0]!;
    expect([target, method, key]).toEqual([
      `/v1/account/crew-invites/${CODE}/accept`,
      "POST",
      KEY,
    ]);
    expect(JSON.parse((body as Buffer).toString())).toEqual({ tags: ["#2PP", "#2RR"] });
  });

  it("says why a join was refused and keeps the picks", async () => {
    mocks.requestJson.mockRejectedValue(
      new PythonApiError(409, { error: "crew_full", open_places: 1 }),
    );
    const { data, status } = unwrap<{ error: string; tags: string[] }>(
      await joinAction(
        post(
          `/crews/join/${CODE}`,
          { "join:#2PP": "on", "join:#2RR": "on" },
          { code: CODE },
        ) as never,
      ),
    );
    expect([status, data.error, data.tags]).toEqual([
      409,
      "Only 1 place is open. Pick fewer accounts.",
      ["#2PP", "#2RR"],
    ]);
  });
});

describe("linking an account from an invite", () => {
  const verified = vi.fn();
  beforeEach(() => {
    verified.mockReset();
    verified.mockResolvedValue({ status: "linked", tag: "#2PP" });
    mocks.createPythonClient.mockReturnValue({ verifyPlayerToken: verified });
  });

  const link = (returnPath: string) =>
    verifyAction({
      request: post("/account/verify-player", {
        tag: "#2PP",
        token: "SECRET-TOKEN-123",
        return: returnPath,
      }).request,
    } as never);

  it("returns to the invite once the account is linked", async () => {
    const response = (await link(`/crews/join/${CODE}`)) as Response;
    expect([response.status, response.headers.get("Location")]).toEqual([
      303,
      `/crews/join/${CODE}`,
    ]);
    const loaded = await verifyLoader({
      request: new Request(`${ORIGIN}/account/verify-player?return=/crews/join/${CODE}`),
    } as never);
    expect(loaded.returnPath).toBe(`/crews/join/${CODE}`);
  });

  it.each(["/account/groups", "https://evil.example/crews/join/x", "//evil.example"])(
    "ignores %s and goes to the account as before",
    async (returnPath) => {
      const response = (await link(returnPath)) as Response;
      expect(response.headers.get("Location")).toBe("/account?linked=%232PP");
    },
  );
});
