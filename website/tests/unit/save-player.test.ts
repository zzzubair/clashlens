import { beforeEach, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  requireLogin: vi.fn(),
  createPythonClient: vi.fn(),
  addSavedTag: vi.fn(),
  removeSavedTag: vi.fn(),
  listSavedTags: vi.fn(),
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

import { action, loader, shouldRevalidate } from "../../app/routes/account.saved-players";

const identity = { provider: "google", providerSubject: "owner" };
const idempotencyKey = "3be934b5-68fa-4741-8c7b-e03592e4ad70";
function submit(mode: string, origin = "https://clashlens.example") {
  return action({
    request: new Request("https://clashlens.example/account/saved-players", {
      method: "POST",
      headers: { Origin: origin },
      body: new URLSearchParams({ source: "player", mode, tag: "#2PP", idempotencyKey }),
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
    currentUrl: new URL("https://clashlens.example/players/2PP"),
    defaultShouldRevalidate: true,
  };
  expect(shouldRevalidate(args as never)).toBe(false);
  expect(
    shouldRevalidate({ ...args, formAction: "/resources/players/2PP/refresh" } as never),
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
