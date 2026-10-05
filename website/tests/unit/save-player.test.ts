import { beforeEach, expect, it, vi } from "vitest";
import { createStaticHandler } from "react-router";

const mocks = vi.hoisted(() => ({
  requireLogin: vi.fn(),
  createPythonClient: vi.fn(),
  addSavedTag: vi.fn(),
  removeSavedTag: vi.fn(),
  listSavedTags: vi.fn(),
  getPlayer: vi.fn(),
  getPlayerSeasons: vi.fn(),
  getPlayerLookup: vi.fn(),
}));
vi.mock("../../app/server/auth-guard.server", () => ({
  requireLogin: mocks.requireLogin,
}));
vi.mock("../../app/server/config.server", () => ({
  getWebsiteConfig: () => ({ publicOrigin: new URL("https://clashlens.example") }),
}));
vi.mock("../../app/services/python.server", () => ({
  createPythonClient: mocks.createPythonClient,
}));
vi.mock("../../app/services/player-lookup.server", () => ({
  getPlayerLookup: mocks.getPlayerLookup,
}));
vi.mock("../../app/services/past-seasons.server", () => ({
  getPastSeasons: async () => [],
}));

import { action, loader, shouldRevalidate } from "../../app/routes/account.saved-players";
import { loader as playerLoader } from "../../app/routes/player";

const identity = { provider: "google", providerSubject: "owner" };
const idempotencyKey = "3be934b5-68fa-4741-8c7b-e03592e4ad70";
function submit(mode: string, origin = "https://clashlens.example", source = "player") {
  return action({
    request: new Request("https://clashlens.example/account/saved-players", {
      method: "POST",
      headers: { Origin: origin },
      body: new URLSearchParams({ source, mode, tag: "#2PP", idempotencyKey }),
    }),
  } as never);
}

beforeEach(() => {
  vi.resetAllMocks();
  mocks.requireLogin.mockResolvedValue(identity);
  mocks.createPythonClient.mockReturnValue(mocks);
});

it.each(["add", "remove"])(
  "%s keeps the profile open and changes only the signed-in account",
  async (mode) => {
    const result = await submit(mode);
    expect(result).toMatchObject({
      data: { tag: "#2PP", saved: mode === "add", generalError: null },
      init: { headers: { "Cache-Control": "no-store" } },
    });
    expect(mocks.createPythonClient).toHaveBeenCalledWith(identity);
    const operation = mode === "add" ? mocks.addSavedTag : mocks.removeSavedTag;
    expect(operation).toHaveBeenCalledWith("#2PP", idempotencyKey);
    expect(result.data.addIdempotencyKey).not.toBe(idempotencyKey);
    expect(result.data.removeIdempotencyKey).not.toBe(idempotencyKey);
  },
);

it("still rejects cross-origin profile saves", async () => {
  expect(await submit("add", "https://other.example")).toMatchObject({
    init: { status: 403 },
  });
  expect(mocks.addSavedTag).not.toHaveBeenCalled();
});

it("requires a login for profile saves", async () => {
  const redirect = new Response(null, { status: 302, headers: { Location: "/login" } });
  mocks.requireLogin.mockRejectedValue(redirect);
  await expect(submit("add")).rejects.toBe(redirect);
  expect(mocks.createPythonClient).not.toHaveBeenCalled();
});

it.each(["", "?tag=%232PP"])(
  "preserves the sign-in redirect when reading saved players%s",
  async (search) => {
    const redirect = new Response(null, { status: 302, headers: { Location: "/login" } });
    mocks.requireLogin.mockRejectedValue(redirect);
    await expect(
      loader({
        request: new Request(`https://clashlens.example/account/saved-players${search}`),
      } as never),
    ).rejects.toBe(redirect);
    expect(mocks.createPythonClient).not.toHaveBeenCalled();
  },
);

it.each(["", "?tag=%232PP"])(
  "returns retryable data without private reads when the login check fails%s",
  async (search) => {
    mocks.requireLogin.mockRejectedValue(new Response(null, { status: 503 }));
    const handler = createStaticHandler([
      { id: "saved", path: "/account/saved-players", loader },
    ]);
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const context = await handler.query(
        new Request(`https://clashlens.example/account/saved-players${search}`),
      );
      if (context instanceof Response) throw new Error("unexpected redirect");
      expect(context.errors).toBeNull();
      expect(context.loaderData.saved).toMatchObject({
        players: [],
        removeIdempotencyKeys: {},
        error: { error: { code: "unavailable" } },
      });
    }
    expect(mocks.requireLogin).toHaveBeenCalledTimes(2);
    expect(mocks.createPythonClient).not.toHaveBeenCalled();
    expect(mocks.listSavedTags).not.toHaveBeenCalled();
    mocks.requireLogin.mockResolvedValue(identity);
    mocks.listSavedTags.mockResolvedValue([{ tag: "#2PP", name: "Player" }]);
    const recovered = await handler.query(
      new Request(`https://clashlens.example/account/saved-players${search}`),
    );
    if (recovered instanceof Response) throw new Error("unexpected redirect");
    expect(recovered.errors).toBeNull();
    expect(recovered.loaderData.saved).toMatchObject({
      players: [{ tag: "#2PP", name: "Player" }],
      error: null,
    });
    expect(mocks.createPythonClient).toHaveBeenCalledWith(identity);
    expect(mocks.listSavedTags).toHaveBeenCalledWith(search ? "#2PP" : undefined);
  },
);

it.each([
  ["add", "player"],
  ["remove", "player"],
  ["add", ""],
  ["remove", ""],
])(
  "returns safe data without private access when login fails for %s/%s",
  async (mode, source) => {
    mocks.requireLogin.mockRejectedValue(new Response(null, { status: 503 }));
    const result = await submit(mode, "https://clashlens.example", source);
    expect(result).toMatchObject({
      init: { status: 503, headers: { "Cache-Control": "no-store" } },
      data: { generalError: { error: { code: "unavailable" } } },
    });
    expect(result.data.saved).toBeUndefined();
    expect(result.data.addIdempotencyKey).not.toBe(idempotencyKey);
    expect(result.data.removeIdempotencyKey).not.toBe(idempotencyKey);
    expect(mocks.createPythonClient).not.toHaveBeenCalled();
    expect(mocks.listSavedTags).not.toHaveBeenCalled();
    expect(mocks.addSavedTag).not.toHaveBeenCalled();
    expect(mocks.removeSavedTag).not.toHaveBeenCalled();
  },
);

it("keeps malformed saved-state tags rejected", async () => {
  await expect(
    loader({
      request: new Request("https://clashlens.example/account/saved-players?tag=bad"),
    } as never),
  ).rejects.toMatchObject({ status: 400 });
  expect(mocks.createPythonClient).not.toHaveBeenCalled();
});

it("keeps a failed save retryable without claiming the player was saved", async () => {
  mocks.addSavedTag.mockRejectedValue(new Error("offline"));
  const result = await submit("add");
  expect(result).toMatchObject({
    init: { status: 422, headers: { "Cache-Control": "no-store" } },
    data: { tag: "#2PP" },
  });
  expect(result.data.saved).toBeUndefined();
  expect(result.data.generalError).not.toBeNull();
  expect(result.data.addIdempotencyKey).not.toBe(idempotencyKey);
});

it("does not reread private saved players during profile polling or Reset", () => {
  const args = {
    currentUrl: new URL("https://clashlens.example/players/%232PP"),
    defaultShouldRevalidate: true,
  };
  expect(shouldRevalidate(args as never)).toBe(false);
  expect(
    shouldRevalidate({
      ...args,
      formAction: "/resources/players/%232PP/refresh",
    } as never),
  ).toBe(false);
  expect(
    shouldRevalidate({ ...args, formAction: "/account/saved-players" } as never),
  ).toBe(true);
  expect(
    shouldRevalidate({
      ...args,
      currentUrl: new URL("https://clashlens.example/account/saved-players"),
    } as never),
  ).toBe(true);
});

it("loads the current player's saved state under the signed-in identity", async () => {
  mocks.listSavedTags.mockResolvedValue([{ tag: "#2PP", name: "Player" }]);
  const result = await loader({
    request: new Request("https://clashlens.example/account/saved-players?tag=%232PP"),
  } as never);
  expect(mocks.createPythonClient).toHaveBeenCalledWith(identity);
  expect(mocks.listSavedTags).toHaveBeenCalledWith("#2PP");
  expect(result.players).toEqual([{ tag: "#2PP", name: "Player" }]);
});

it.each([
  ["/players/%232PP", "#2PP"],
  ["/players/2PP", null],
])("routes %s to the expected player state", async (path, tag) => {
  mocks.getPlayer.mockResolvedValue({ tag: "#2PP" });
  mocks.getPlayerSeasons.mockResolvedValue([]);
  mocks.getPlayerLookup.mockResolvedValue({ tag: "#2PP", state: "tracking" });
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

it.each([
  ["?tag=%232PP", false],
  ["", true],
])(
  "sends an unfinished account to setup only from the list%s",
  async (search, redirects) => {
    mocks.listSavedTags.mockRejectedValue({
      status: 404,
      payload: { error: "account_not_found" },
    });
    const read = loader({
      request: new Request(`https://clashlens.example/account/saved-players${search}`),
    } as never);
    if (redirects) {
      await expect(read).rejects.toMatchObject({ status: 302 });
      return;
    }
    await expect(read).resolves.toMatchObject({ players: [], error: null });
  },
);
