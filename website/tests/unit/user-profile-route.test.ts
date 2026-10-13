import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  StaticRouterProvider,
} from "react-router";
import { beforeEach, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  requireLogin: vi.fn(),
  readLoginIdentity: vi.fn(),
  createPythonClient: vi.fn(),
}));

vi.mock("../../app/server/config.server", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/server/config.server")>();
  return {
    ...actual,
    getWebsiteConfig: () =>
      actual.loadWebsiteConfig({
        NODE_ENV: "test",
        CLASHLENS_PUBLIC_ORIGIN: "https://clashlens.example",
      }),
  };
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

import UserRoute, { action, loader } from "../../app/routes/users.$username";
import { PythonApiError } from "../../app/services/python.server";
import { worstPublicUser } from "../fixtures/worst-case-accounts";

const data = {
  user: {
    username: "nova88",
    displayName: "Nova",
    verifiedPlayers: [
      {
        tag: "#2PP",
        name: "Nova",
        clan: "Night Owls",
        state: "tracking",
        reason: null,
        trophies: 5300,
        seasonResetPending: false,
        rank: 1234,
        league: null,
        today: { net: 28, attacks: 5, defenses: 3 },
      },
      {
        tag: "#8PY",
        name: "Second",
        clan: null,
        state: "tracking",
        reason: null,
        trophies: 5100,
        seasonResetPending: false,
        rank: null,
        league: null,
        today: { net: null, attacks: 2, defenses: 0 },
      },
      {
        tag: "#2PQ",
        name: "Quiet",
        clan: null,
        state: "tracking",
        reason: "no_legend_battles",
        trophies: null,
        seasonResetPending: false,
        rank: null,
        league: null,
        today: null,
      },
      {
        tag: "#9PY",
        name: "Demoted",
        clan: null,
        state: "not_in_legend",
        reason: null,
        trophies: 4900,
        seasonResetPending: false,
        rank: null,
        league: "Electro League 33",
        today: { net: -40, attacks: 0, defenses: 1 },
      },
    ],
  },
  notFound: false,
  error: null,
  owner: null,
};

const IDENTITY = { provider: "google", providerSubject: "11223344556677889900" };
const IDEMPOTENCY_KEY = "3be934b5-68fa-4741-8c7b-e03592e4ad70";

const client = {
  getPublicUser: vi.fn(),
  getAccountSummary: vi.fn(),
  setDefaultPlayer: vi.fn(),
};

beforeEach(() => {
  vi.clearAllMocks();
  mocks.requireLogin.mockResolvedValue(IDENTITY);
  mocks.readLoginIdentity.mockResolvedValue(IDENTITY);
  client.getPublicUser.mockResolvedValue(data.user);
  client.getAccountSummary.mockResolvedValue({
    username: "nova88",
    displayName: "Nova",
    verifiedPlayers: [],
    defaultPlayer: "#8PY",
  });
  client.setDefaultPlayer.mockResolvedValue(undefined);
  mocks.createPythonClient.mockReturnValue(client);
});

async function renderProfile(search: string, loaderData: object = data) {
  const handler = createStaticHandler([
    { path: "/users/:username", Component: UserRoute, loader: () => loaderData },
  ]);
  const context = await handler.query(
    new Request(`https://clashlens.example/users/nova88${search}`),
  );
  if (context instanceof Response) throw new Error("unexpected response");
  return renderToString(
    createElement(StaticRouterProvider, {
      router: createStaticRouter(handler.dataRoutes, context),
      context,
      hydrate: false,
    }),
  ).replaceAll("<!-- -->", "");
}

it("confirms a just-linked player without knowing who is signed in", async () => {
  const html = await renderProfile("?linked=%232PP");
  expect(html).toContain("Linked #2PP");
  expect(html).not.toContain("Edit profile");
  expect(await renderProfile("?linked=%232PY")).not.toContain("Linked #2PY");
});

it("shows each linked account as one card linking to its player page", async () => {
  const html = await renderProfile("");
  const cards = html.match(/<a class="linked-player-card"[\s\S]*?<\/a>/g) ?? [];
  expect(cards).toHaveLength(4);
  const [tracked, unknownNet, season0, demoted] = cards;
  expect(tracked).toContain('href="/players/%232PP"');
  expect(tracked).toContain(
    'aria-labelledby="linked-player-2PP-name linked-player-2PP-tag"',
  );
  for (const text of ["Nova", "#2PP", "Night Owls", "5,300", "#1,234", "+28", " so far"])
    expect(tracked).toContain(text);
  expect(tracked).toContain("5/8 attacks · 3/8 defenses");
  // Not every battle so far is recorded, so the net is not claimed.
  expect(unknownNet).toContain("Unranked");
  expect(unknownNet).toContain("Unknown");
  expect(unknownNet).not.toContain("so far");
  expect(unknownNet).toContain("2/8 attacks · 0/8 defenses");
  expect(tracked).not.toContain("linked-player-note");
  // The same words as the player page, with nothing valid to count.
  expect(season0).toContain(
    "Quiet is in Legend League but hasn&#x27;t played a Legend League battle this Season.",
  );
  for (const text of ["Trophies", "Unknown", "Unranked", "Not available yet"])
    expect(season0).toContain(text);
  // A player who left Legend I keeps their saved trophies and today's battles,
  // and shows its saved league where a Legend I player shows its rank.
  expect(demoted).toContain("This player is not in Legend I.");
  for (const text of ["4,900", "Electro League 33", "-40", "0/8 attacks · 1/8 defenses"])
    expect(demoted).toContain(text);
  expect(demoted).not.toContain("Unranked");
  expect(demoted).toContain(
    'aria-describedby="linked-player-9PY-details linked-player-9PY-note"',
  );
});

it("says a failed profile read could not load instead of showing no linked accounts", async () => {
  const html = await renderProfile("?linked=%232PP", {
    user: null,
    notFound: false,
    error: { error: { code: "unavailable", message: "Try again later." } },
  });
  expect(html).toContain("Profile could not be loaded.");
  expect(html).toContain('<a href="/users/nova88">Try again</a>');
  expect(html).not.toContain("No linked accounts yet");
  expect(html).not.toContain("Linked accounts");
});

it("shows worst-case linked players with separators and every missing value named", async () => {
  const html = (
    await renderProfile("", { user: worstPublicUser(), notFound: false, error: null })
  ).replaceAll("&#x27;", "'");
  expect(html.match(/<a class="linked-player-card"/g)).toHaveLength(6);
  for (const text of [
    "#13,204",
    "6,499",
    "#1<",
    "0/8 attacks · 0/8 defenses",
    "1/8 attacks · 8/8 defenses",
    "Waiting for this player's Season reset",
    "Not available yet",
    "4,812",
    "Electro League 33",
  ])
    expect(html).toContain(text);
});

async function loadProfile() {
  const result = (await loader({
    params: { username: "nova88" },
    request: new Request("https://clashlens.example/users/nova88"),
  } as never)) as unknown as { data: { owner: unknown; user: unknown } };
  return result.data;
}

it("gives the default account controls only to the profile's signed-in owner", async () => {
  const own = await loadProfile();
  expect(own.user).toEqual(data.user);
  expect(own.owner).toEqual({
    defaultPlayer: "#8PY",
    idempotencyKey: expect.any(String),
  });
  expect(mocks.createPythonClient).toHaveBeenCalledWith(IDENTITY);

  client.getAccountSummary.mockResolvedValue({
    username: "other",
    displayName: "Other",
    verifiedPlayers: [],
    defaultPlayer: "#9PY",
  });
  expect((await loadProfile()).owner).toBeNull();

  mocks.readLoginIdentity.mockResolvedValue(null);
  expect((await loadProfile()).owner).toBeNull();

  // A failed owner read still shows the public profile, without the controls.
  mocks.readLoginIdentity.mockResolvedValue(IDENTITY);
  client.getAccountSummary.mockRejectedValue(new PythonApiError(503, { error: "x" }));
  const failed = await loadProfile();
  expect(failed.user).toEqual(data.user);
  expect(failed.owner).toBeNull();
});

it("marks the owner's default account and offers Make default on the others", async () => {
  const html = await renderProfile("", {
    ...data,
    owner: { defaultPlayer: "#8PY", idempotencyKey: IDEMPOTENCY_KEY },
  });
  const items = html.match(/<li class="linked-player-owned">[\s\S]*?<\/li>/g) ?? [];
  expect(items).toHaveLength(4);
  const [nova, second, ...rest] = items;
  expect(second).toContain("✓</span> Default");
  expect(second).not.toContain("Make default");
  for (const item of [nova, ...rest]) {
    expect(item).toContain(">Make default</button>");
    expect(item).not.toContain("linked-player-default");
  }
  // The button sits beside the card link, never inside it.
  for (const card of html.match(/<a class="linked-player-card"[\s\S]*?<\/a>/g) ?? [])
    expect(card).not.toContain("<form");
  expect(nova).toContain('name="tag" value="#2PP"');
  expect(nova).toContain(`value="${IDEMPOTENCY_KEY}"`);
  expect(
    await renderProfile("?default=saved", {
      ...data,
      owner: { defaultPlayer: "#8PY", idempotencyKey: IDEMPOTENCY_KEY },
    }),
  ).toContain("Default account saved.");
});

it("shows other visitors neither the Default badge nor Make default", async () => {
  const html = await renderProfile("?default=saved");
  for (const text of ["Make default", "linked-player-default", "Default account saved."])
    expect(html).not.toContain(text);
});

function defaultRequest(
  fields: Record<string, string>,
  origin = "https://clashlens.example",
) {
  return new Request("https://clashlens.example/users/nova88", {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded", Origin: origin },
    body: new URLSearchParams(fields).toString(),
  });
}

it.each([
  { failure: false, outcome: "saved" },
  { failure: true, outcome: "failed" },
])("saves the default account and says it was $outcome", async ({ failure, outcome }) => {
  if (failure)
    client.setDefaultPlayer.mockRejectedValue(
      new PythonApiError(404, { error: "player_not_verified" }),
    );
  const response = await action({
    params: { username: "nova88" },
    request: defaultRequest({
      intent: "default-player",
      tag: "#2PP",
      idempotencyKey: IDEMPOTENCY_KEY,
    }),
  } as never);
  expect(mocks.createPythonClient).toHaveBeenCalledWith(IDENTITY);
  expect(client.setDefaultPlayer).toHaveBeenCalledWith("#2PP", IDEMPOTENCY_KEY);
  expect(response.status).toBe(302);
  expect(response.headers.get("Location")).toBe(`/users/nova88?default=${outcome}`);
});

it("refuses a default account change from another site or without a key", async () => {
  const fields = {
    intent: "default-player",
    tag: "#2PP",
    idempotencyKey: IDEMPOTENCY_KEY,
  };
  const crossSite = await action({
    params: { username: "nova88" },
    request: defaultRequest(fields, "https://evil.example"),
  } as never);
  expect(crossSite.status).toBe(403);
  const noKey = await action({
    params: { username: "nova88" },
    request: defaultRequest({ ...fields, idempotencyKey: "" }),
  } as never);
  expect(noKey.status).toBe(400);
  expect(client.setDefaultPlayer).not.toHaveBeenCalled();
});
