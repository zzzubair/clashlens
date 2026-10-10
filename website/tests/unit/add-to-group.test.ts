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
  createPythonClient: vi.fn(),
  requestJson: vi.fn(),
  listGroups: vi.fn(),
  createGroup: vi.fn(),
  getPlayer: vi.fn(),
  getPlayerSeasons: vi.fn(),
  getPlayerLookup: vi.fn(),
  startPlayerLookup: vi.fn(),
}));
vi.mock("../../app/server/auth-guard.server", () => ({
  requireLogin: mocks.requireLogin,
}));
vi.mock("../../app/server/config.server", () => ({
  getWebsiteConfig: () => ({ publicOrigin: new URL("https://clashlens.example") }),
}));
vi.mock("../../app/services/python.server", async () => ({
  createPythonClient: mocks.createPythonClient,
  requestJson: mocks.requestJson,
  PythonApiError: (await import("../../app/services/python-response.server"))
    .PythonApiError,
}));
vi.mock("../../app/services/player-lookup.server", () => ({
  getPlayerLookup: mocks.getPlayerLookup,
  startPlayerLookup: mocks.startPlayerLookup,
}));
vi.mock("../../app/services/past-seasons.server", () => ({
  getPastSeasons: async () => [],
}));

import { PlayerActions } from "../../app/components/PlayerActions";
import AddToGroupRoute, { action, loader } from "../../app/routes/account.groups.add";
import { loader as playerLoader } from "../../app/routes/player";

const identity = { provider: "google", providerSubject: "owner" };
const ADD_KEY = "3be934b5-68fa-4741-8c7b-e03592e4ad70";
const CREATE_KEY = "6f0c8a9e-2c1d-4b7e-9a51-0d6f1e2b3c4d";
const FRIENDS = "11111111-2222-4333-8444-555555555555";
const WAR = "11111111-2222-4333-8444-666666666666";
const FULL = "11111111-2222-4333-8444-777777777777";
const NOVA = {
  tag: "#2PP",
  name: "Nova",
  trophies: 5400,
  seasonResetPending: false,
  state: "tracking",
};

function group(groupId: string, name: string, tags: string[]) {
  return { groupId, name, tags, players: tags.map((tag) => ({ ...NOVA, tag })) };
}
const twentyTags = Array.from(
  { length: 20 },
  (_, index) => `#P${"0289"[index % 4]}${"QGRJC"[index % 5]}`,
);

function submit(fields: Record<string, string>, origin = "https://clashlens.example") {
  return action({
    request: new Request("https://clashlens.example/account/groups/add?tag=%232PP", {
      method: "POST",
      headers: { Origin: origin },
      body: new URLSearchParams({ tag: "#2PP", addIdempotencyKey: ADD_KEY, ...fields }),
    }),
    context: undefined,
  } as never);
}

async function renderPage() {
  const handler = createStaticHandler([
    { path: "/account/groups/add", Component: AddToGroupRoute, loader: loader as never },
  ]);
  const context = await handler.query(
    new Request("https://clashlens.example/account/groups/add?tag=%232PP"),
  );
  if (context instanceof Response) throw new Error("unexpected route response");
  return renderToString(
    createElement(StaticRouterProvider, {
      router: createStaticRouter(handler.dataRoutes, context),
      context,
    }),
  ).replace(/<script[\s\S]*?<\/script>/g, "");
}

beforeEach(() => {
  vi.resetAllMocks();
  mocks.requireLogin.mockResolvedValue(identity);
  mocks.createPythonClient.mockReturnValue(mocks);
  mocks.getPlayerLookup.mockResolvedValue({ tag: "#2PP", state: "tracking" });
  mocks.requestJson.mockResolvedValue({
    tag: "#2PP",
    name: "Nova",
    trophies: 5400,
    state: "tracking",
    season_reset_pending: false,
  });
});

it("asks a Clasher with no groups to name one, then puts the player straight in", async () => {
  mocks.listGroups.mockResolvedValue({ season: "1791176400", groups: [] });
  const html = await renderPage();
  expect(html).toContain("Create your first group");
  expect(html).toContain(
    "You have no groups yet. Name one and #2PP goes straight into it.",
  );

  mocks.createGroup.mockResolvedValue({ groupId: FRIENDS, name: "Friends", tags: [] });
  const result = await submit({
    action: "create",
    name: " Friends ",
    createIdempotencyKey: CREATE_KEY,
  });
  expect(mocks.createGroup).toHaveBeenCalledWith({ name: "Friends" }, CREATE_KEY);
  expect(mocks.requestJson.mock.calls[0]?.slice(0, 2)).toEqual([
    `/v1/account/groups/${FRIENDS}/players`,
    "POST",
  ]);
  expect(result.data.notice).toBe("Added Nova (#2PP) to Friends.");
  expect(result.data.addedTo).toBe(FRIENDS);
});

it("leaves no empty group behind for a tag the game does not know", async () => {
  mocks.listGroups.mockResolvedValue({ season: "1791176400", groups: [] });
  mocks.getPlayerLookup.mockResolvedValue({ tag: "#2PP", state: "not_found" });
  const result = await submit({
    action: "create",
    name: "Friends",
    createIdempotencyKey: CREATE_KEY,
  });
  expect(result.init?.status).toBe(422);
  expect(result.data.fieldErrors).toEqual({
    tag: "Clash of Clans has no player with the tag #2PP. Check the tag and try again.",
  });
  expect(mocks.createGroup).not.toHaveBeenCalled();
});

it("adds to the only group and names it", async () => {
  mocks.listGroups.mockResolvedValue({
    season: "1791176400",
    groups: [group(FRIENDS, "Friends", [])],
  });
  const html = await renderPage();
  expect(html).toContain("Friends is your only group, so #2PP goes there.");
  expect(html).toContain("Add to Friends");

  const result = await submit({ action: "add", groupId: FRIENDS });
  expect(result.data.notice).toBe("Added Nova (#2PP) to Friends.");
});

it("makes a Clasher with several groups choose, and explains the ones that cannot take the player", async () => {
  mocks.listGroups.mockResolvedValue({
    season: "1791176400",
    groups: [
      group(FRIENDS, "Friends", ["#2PP"]),
      group(WAR, "War", []),
      group(FULL, "Packed", twentyTags),
    ],
  });
  const html = await renderPage();
  expect(html).toContain("Choose a group");
  expect(html).toContain("#2PP is already in this group");
  expect(html).toContain("Full: 20 of 20 players");
  expect(html.match(/type="radio"[^>]*disabled=""/g)).toHaveLength(2);

  const unchosen = await submit({ action: "add" });
  expect(unchosen.init?.status).toBe(400);
  expect(unchosen.data.fieldErrors).toEqual({ tag: "Choose a group." });

  const member = await submit({ action: "add", groupId: FRIENDS });
  expect(member.init?.status).toBe(409);
  expect(member.data.fieldErrors).toEqual({
    tag: "Nova (#2PP) is already in this group.",
  });

  const full = await submit({ action: "add", groupId: FULL });
  expect(full.init?.status).toBe(422);
  expect(full.data.fieldErrors.tag).toContain("already has 20 players");
  // Refused before spending a player check or a write.
  expect(mocks.getPlayerLookup).not.toHaveBeenCalled();
  expect(mocks.requestJson).not.toHaveBeenCalled();

  const chosen = await submit({ action: "add", groupId: WAR });
  expect(chosen.data.notice).toBe("Added Nova (#2PP) to War.");
});

it("refuses other sites and needs a login", async () => {
  const crossSite = await submit(
    { action: "add", groupId: WAR },
    "https://other.example",
  );
  expect(crossSite.init?.status).toBe(400);
  expect(mocks.listGroups).not.toHaveBeenCalled();

  const login = new Response(null, { status: 302, headers: { Location: "/login" } });
  mocks.requireLogin.mockRejectedValue(login);
  await expect(submit({ action: "add", groupId: WAR })).rejects.toBe(login);
  await expect(
    loader({
      request: new Request("https://clashlens.example/account/groups/add?tag=%232PP"),
    } as never),
  ).rejects.toBe(login);
  expect(mocks.createPythonClient).not.toHaveBeenCalled();
});

it("shows Add to group on a profile only to a signed-in Clasher", async () => {
  for (const loggedIn of [true, false]) {
    const handler = createStaticHandler([
      {
        id: "root",
        path: "/",
        loader: () => ({ loggedIn }),
        Component: () => createElement(PlayerActions, { tag: "#2PP" }),
      },
    ]);
    const context = await handler.query(new Request("https://clashlens.example/"));
    if (context instanceof Response) throw new Error("unexpected route response");
    const html = renderToString(
      createElement(StaticRouterProvider, {
        router: createStaticRouter(handler.dataRoutes, context),
        context,
      }),
    );
    expect(html.includes('href="/account/groups/add?tag=%232PP"')).toBe(loggedIn);
  }
});

it.each([
  ["/players/%232PP", "#2PP"],
  ["/players/2PP", null],
])("routes %s to the expected player state", async (path, tag) => {
  mocks.getPlayer.mockResolvedValue({ tag: "#2PP" });
  mocks.getPlayerSeasons.mockResolvedValue([]);
  const handler = createStaticHandler([
    { id: "player", path: "/players/:tag", loader: playerLoader },
  ]);
  const context = await handler.query(new Request(`https://clashlens.example${path}`));
  if (context instanceof Response) throw new Error("unexpected redirect");
  expect(context.errors).toBeNull();
  expect(context.loaderData.player.requestedTag).toBe(tag);
  if (tag === null) {
    expect(context.loaderData.player.error.error.code).toBe("invalid_input");
    expect(mocks.getPlayer).not.toHaveBeenCalled();
  } else {
    expect(context.loaderData.player.error).toBeNull();
    expect(context.loaderData.player.player.tag).toBe(tag);
    expect(mocks.getPlayer).toHaveBeenCalledWith(tag);
  }
});
