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
  getAttackAlerts: vi.fn(),
  getOverlay: vi.fn(),
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

vi.mock("../../app/services/discord-alerts.server", () => ({
  getAttackAlerts: mocks.getAttackAlerts,
}));

vi.mock("../../app/services/obs-overlay.server", () => ({
  getOverlay: mocks.getOverlay,
}));

import UserRoute, { loader } from "../../app/routes/users.$username";
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

const client = {
  getPublicUser: vi.fn(),
  getAccount: vi.fn(),
};

const alertsFor = (...switches: Array<[boolean, boolean]>) => ({
  players: switches.map(([on, summaryOn], index) => ({
    tag: `#${index}PP`,
    name: null,
    on,
    summaryOn,
  })),
  undelivered: null,
});

beforeEach(() => {
  vi.clearAllMocks();
  mocks.requireLogin.mockResolvedValue(IDENTITY);
  mocks.readLoginIdentity.mockResolvedValue(IDENTITY);
  client.getPublicUser.mockResolvedValue(data.user);
  client.getAccount.mockResolvedValue({
    username: "nova88",
    displayName: "Nova",
    preferences: {},
    providers: ["google", "discord"],
  });
  mocks.getAttackAlerts.mockResolvedValue(alertsFor([true, false], [false, false]));
  mocks.getOverlay.mockResolvedValue({
    tag: "#2PP",
    players: [{ tag: "#2PP", name: "Nova", league: null }],
  });
  mocks.createPythonClient.mockReturnValue(client);
});

/** The profile page; `signedInAs` is who the top bar says is signed in. */
async function renderProfile(
  search: string,
  loaderData: object = data,
  signedInAs: string | null = null,
) {
  const handler = createStaticHandler([
    {
      id: "root",
      path: "/",
      loader: () => ({ accountUsername: signedInAs }),
      children: [
        { path: "users/:username", Component: UserRoute, loader: () => loaderData },
      ],
    },
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
  )
    .replaceAll("<!-- -->", "")
    .replaceAll(' data-discover="true"', "");
}

it("confirms a just-linked player by name in one line at the top", async () => {
  const html = await renderProfile("?linked=%232PP");
  expect(html).toContain(
    '<main id="main-content" tabindex="-1" class="page-shell narrow-shell"><p class="status-banner status-banner-success saved-line" role="status">Nova linked.</p>',
  );
  expect(html).not.toContain(">Edit<");
  expect(await renderProfile("?linked=%232PY")).not.toContain("saved-line");
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

it("says a linked tag is unknown to the game without asking for a new search", async () => {
  const lost = { ...data.user.verifiedPlayers[0], state: "not_found", today: null };
  const html = await renderProfile("", {
    ...data,
    user: { ...data.user, verifiedPlayers: [lost] },
  });
  expect(html).toContain("Player not found. Clash of Clans has no player with this tag.");
  expect(html).not.toContain("search again");
});

it("says a failed profile read could not load instead of showing no linked accounts", async () => {
  const html = await renderProfile("?linked=%232PP", {
    user: null,
    notFound: false,
    error: { error: { code: "unavailable", message: "Try again later." } },
  });
  expect(html).toContain("Profile could not be loaded.");
  expect(html).toContain('<a href="/users/nova88">Try again</a>');
  expect(html).not.toContain("linked a Clash player");
  expect(html).not.toContain("Clash players");
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

it("reads the owner's tool statuses only for the profile's signed-in owner", async () => {
  const own = await loadProfile();
  expect(own.user).toEqual(data.user);
  expect(own.owner).toEqual({ alerts: "On for 1 of 2 players", overlay: "Showing Nova" });
  expect(mocks.createPythonClient).toHaveBeenCalledWith(IDENTITY);

  mocks.getAttackAlerts.mockResolvedValue(alertsFor([true, true]));
  mocks.getOverlay.mockResolvedValue({ tag: null, players: [] });
  expect((await loadProfile()).owner).toEqual({ alerts: "On", overlay: "Not set up" });
  // Only attack alerts count; a daily summary alone is not an attack alert.
  mocks.getAttackAlerts.mockResolvedValue(alertsFor([false, true]));
  mocks.getOverlay.mockRejectedValue(new PythonApiError(503, { error: "x" }));
  expect((await loadProfile()).owner).toEqual({ alerts: "Off", overlay: null });

  // Saved switches send nothing once Discord is disconnected.
  mocks.getAttackAlerts.mockClear();
  mocks.getAttackAlerts.mockResolvedValue(alertsFor([true, true]));
  client.getAccount.mockResolvedValue({
    username: "nova88",
    displayName: "Nova",
    preferences: {},
    providers: ["google"],
  });
  expect((await loadProfile()).owner).toEqual({
    alerts: "Discord not connected",
    overlay: null,
  });
  expect(mocks.getAttackAlerts).not.toHaveBeenCalled();

  client.getAccount.mockResolvedValue({
    username: "other",
    displayName: "Other",
    preferences: {},
    providers: ["google", "discord"],
  });
  expect((await loadProfile()).owner).toBeNull();

  mocks.readLoginIdentity.mockResolvedValue(null);
  expect((await loadProfile()).owner).toBeNull();

  // A failed owner read still shows the public profile, without the statuses.
  mocks.readLoginIdentity.mockResolvedValue(IDENTITY);
  client.getAccount.mockRejectedValue(new PythonApiError(503, { error: "x" }));
  const failed = await loadProfile();
  expect(failed.user).toEqual(data.user);
  expect(failed.owner).toBeNull();
});

it("gives the owner one small Edit button and their two tools, and no settings", async () => {
  const owner = { alerts: "On for 1 of 2 players", overlay: "Not set up" };
  const html = await renderProfile("", { ...data, owner }, "nova88");
  expect(html).toContain(
    '<a class="button button-secondary profile-edit" aria-label="Edit your profile in Settings" href="/account/settings">Edit</a>',
  );
  const tools = html.match(/<a class="profile-tool"[\s\S]*?<\/a>/g) ?? [];
  expect(tools).toHaveLength(2);
  expect(tools[0]).toContain('href="/account/discord-alerts"');
  expect(tools[0]).toContain("On for 1 of 2 players");
  expect(tools[1]).toContain('href="/account/obs-overlay"');
  expect(tools[1]).toContain("Stream overlay");
  expect(tools[1]).toContain("Not set up");
  for (const text of ["Make default", "Sign-in", "OBS overlay", "Default"])
    expect(html).not.toContain(text);
  // The cards are the same ones every visitor sees.
  expect(html.match(/<a class="linked-player-card"/g)).toHaveLength(4);
});

it("shows other visitors no Edit button and no tools", async () => {
  const html = await renderProfile("", data, "someone");
  for (const text of [">Edit<", "Your tools", "profile-tool"])
    expect(html).not.toContain(text);
});

it("asks the owner, and only the owner, to link a first player", async () => {
  const empty = { ...data, user: { ...data.user, verifiedPlayers: [] } };
  const own = await renderProfile("", empty, "nova88");
  expect(own).toContain("Link your Clash player to see your Legend days here.");
  expect(own).toContain('href="/account/verify-player">Link your Clash player</a>');
  const other = await renderProfile("", empty);
  expect(other).toContain("This user hasn&#x27;t linked a Clash player yet.");
  expect(other).not.toContain("verify-player");
});
