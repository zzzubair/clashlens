import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getWebsiteConfig: vi.fn(),
  requireLogin: vi.fn(),
  createPythonClient: vi.fn(),
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
  return { ...actual, createPythonClient: mocks.createPythonClient };
});

import { loadWebsiteConfig, type WebsiteConfig } from "../../app/server/config.server";
import { PythonApiError, type PythonClient } from "../../app/services/python.server";
import { loader as accountLoader } from "../../app/routes/account";
import {
  action as groupsAction,
  loader as groupsLoader,
} from "../../app/routes/account.groups";
import {
  action as profileAction,
  loader as profileLoader,
} from "../../app/routes/account.profile";
import {
  action as savedPlayersAction,
  loader as savedPlayersLoader,
} from "../../app/routes/account.saved-players";
import {
  action as setupAction,
  loader as setupLoader,
} from "../../app/routes/account.setup";
import {
  action as verifyPlayerAction,
  loader as verifyPlayerLoader,
} from "../../app/routes/account.verify-player";

const TEST_SECRET = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8";
const ORIGIN = "https://clashlens.example";
const IDENTITY = { provider: "google", providerSubject: "11223344556677889900" } as const;
const IDEMPOTENCY_KEY = "3be934b5-68fa-4741-8c7b-e03592e4ad70";
const GROUP_ID = "6c1e3f8a-2a44-4b7d-9c0e-1f2a3b4c5d6e";
const TAG = "#P0LQ2Y8";
const TAG2 = "#G2Y8P0LQ";
const ACCOUNT = {
  username: "nova88",
  displayName: "Nova",
  preferences: {},
  providers: ["google"],
};
const SUMMARY = { username: "nova88", displayName: "Nova", verifiedPlayers: [] };
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

interface DataWithResponseInit<T> {
  type: "DataWithResponseInit";
  data: T;
  init: { status?: number; headers?: Record<string, string> };
}

function dataOf<T>(result: unknown): {
  data: T;
  status: number;
  headers: Record<string, string>;
} {
  expect(result).toMatchObject({ type: "DataWithResponseInit" });
  const wrapped = result as DataWithResponseInit<T>;
  return {
    data: wrapped.data,
    status: wrapped.init.status ?? 200,
    headers: wrapped.init.headers ?? {},
  };
}

function expectRedirectTo(location: string) {
  return (thrown: unknown): boolean => {
    expect(thrown).toBeInstanceOf(Response);
    expect((thrown as Response).status).toBe(302);
    expect((thrown as Response).headers.get("Location")).toBe(location);
    return true;
  };
}

function formRequest(
  path: string,
  fields: Record<string, string>,
  headers: Record<string, string> = {},
): Request {
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

function fakeClient(overrides: Partial<PythonClient> = {}): PythonClient {
  return {
    getAccount: vi.fn(async () => ({ ...ACCOUNT })),
    createAccount: vi.fn(async () => ({ ...ACCOUNT })),
    updateAccount: vi.fn(async () => ({ ...ACCOUNT })),
    listSavedTags: vi.fn(async () => []),
    addSavedTag: vi.fn(async () => ({ tag: TAG, saved: true })),
    removeSavedTag: vi.fn(async () => ({ tag: TAG, saved: false })),
    listGroups: vi.fn(async () => []),
    createGroup: vi.fn(async () => ({
      groupId: GROUP_ID,
      name: "Clanmates",
      tags: [TAG],
    })),
    updateGroup: vi.fn(async () => ({
      groupId: GROUP_ID,
      name: "Clanmates",
      tags: [TAG],
    })),
    deleteGroup: vi.fn(async () => ({ groupId: GROUP_ID, deleted: true })),
    getAccountSummary: vi.fn(async () => ({ ...SUMMARY })),
    getPublicUser: vi.fn(),
    verifyPlayerToken: vi.fn(async () => ({ status: "linked", tag: TAG })),
    ...overrides,
  } as PythonClient;
}

function assertNoStoreHeaders(headers: Record<string, string>): void {
  expect(headers["Cache-Control"]).toBe("no-store");
}

function assertNoProviderData(payload: unknown): void {
  const serialized = JSON.stringify(payload);
  expect(serialized.toLowerCase()).not.toContain("provider");
  expect(serialized).not.toContain(IDENTITY.providerSubject);
}

describe("account routes", () => {
  let config: WebsiteConfig;
  let client: PythonClient;

  beforeEach(() => {
    config = loadWebsiteConfig({
      NODE_ENV: "test",
      CLASHLENS_LOGIN_ENABLED: "true",
      CLASHLENS_PUBLIC_ORIGIN: ORIGIN,
      CLASHLENS_GOOGLE_CLIENT_ID: "test-client.apps.googleusercontent.com",
      CLASHLENS_GOOGLE_CLIENT_SECRET: "test-client-secret",
      CLASHLENS_LOGIN_SECRET_B64: TEST_SECRET,
    });
    mocks.getWebsiteConfig.mockReturnValue(config);
    mocks.requireLogin.mockResolvedValue(IDENTITY);
    client = fakeClient();
    mocks.createPythonClient.mockReturnValue(client);
  });

  describe("account.setup", () => {
    it("exposes only a fresh idempotency key from the loader", async () => {
      const result = await setupLoader({
        request: new Request(`${ORIGIN}/account/setup`),
      } as never);
      expect(UUID_PATTERN.test(result.idempotencyKey)).toBe(true);
      assertNoProviderData(result);
    });

    it("creates the account with normalized names and the canonical key, then redirects", async () => {
      await expect(
        setupAction({
          request: formRequest("/account/setup", {
            idempotencyKey: IDEMPOTENCY_KEY,
            username: " Nova88 ",
            displayName: " Nova ",
          }),
        } as never),
      ).rejects.toSatisfy(expectRedirectTo("/account"));
      expect(client.createAccount).toHaveBeenCalledWith(
        { username: "nova88", displayName: "Nova" },
        IDEMPOTENCY_KEY,
      );
    });

    it("rejects strict bad names before calling Python and returns a fresh key", async () => {
      for (const username of ["FuckYou", "admin", "8nova"]) {
        const result = await setupAction({
          request: formRequest("/account/setup", {
            idempotencyKey: IDEMPOTENCY_KEY,
            username,
            displayName: "Nova",
          }),
        } as never);
        const { data, status, headers } = dataOf<{
          idempotencyKey: string;
          fieldErrors: Record<string, string>;
        }>(result);
        expect(status).toBe(400);
        expect(data.fieldErrors.username).toBeTruthy();
        expect(data.idempotencyKey).not.toBe(IDEMPOTENCY_KEY);
        expect(UUID_PATTERN.test(data.idempotencyKey)).toBe(true);
        assertNoStoreHeaders(headers);
        assertNoProviderData(data);
      }
      expect(client.createAccount).not.toHaveBeenCalled();
    });

    it("redirects to /account when Python reports account_exists", async () => {
      client.createAccount = vi.fn(async () => {
        throw new PythonApiError(409, { error: "account_exists" });
      });
      await expect(
        setupAction({
          request: formRequest("/account/setup", {
            idempotencyKey: IDEMPOTENCY_KEY,
            username: "nova88",
            displayName: "Nova",
          }),
        } as never),
      ).rejects.toSatisfy(expectRedirectTo("/account"));
    });

    it("maps username_unavailable to a 409 field error", async () => {
      client.createAccount = vi.fn(async () => {
        throw new PythonApiError(409, { error: "username_unavailable" });
      });
      const result = await setupAction({
        request: formRequest("/account/setup", {
          idempotencyKey: IDEMPOTENCY_KEY,
          username: "nova88",
          displayName: "Nova",
        }),
      } as never);
      const { data, status } = dataOf<{
        fieldErrors: Record<string, string>;
        idempotencyKey: string;
      }>(result);
      expect(status).toBe(409);
      expect(data.fieldErrors.username).toBe("That username is already taken.");
      expect(UUID_PATTERN.test(data.idempotencyKey)).toBe(true);
    });

    it("maps Python validation and general failures to safe 422 responses", async () => {
      client.createAccount = vi.fn(async () => {
        throw new PythonApiError(422, { error: "invalid_request" });
      });
      const validation = await setupAction({
        request: formRequest("/account/setup", {
          idempotencyKey: IDEMPOTENCY_KEY,
          username: "nova88",
          displayName: "Nova",
        }),
      } as never);
      expect(dataOf(validation).status).toBe(422);

      client.createAccount = vi.fn(async () => {
        throw new PythonApiError(503, { error: "unavailable" });
      });
      const unavailable = await setupAction({
        request: formRequest("/account/setup", {
          idempotencyKey: IDEMPOTENCY_KEY,
          username: "nova88",
          displayName: "Nova",
        }),
      } as never);
      const { data, status, headers } = dataOf<{
        generalError: { error: { code: string } };
      }>(unavailable);
      expect(status).toBe(422);
      expect(data.generalError.error.code).toBe("unavailable");
      assertNoStoreHeaders(headers);
      assertNoProviderData(data);
    });

    it("rejects cross-origin requests before any Python call", async () => {
      const hostileHeaders: Array<Record<string, string>> = [
        { Origin: "https://evil.example" },
      ];
      for (const headers of hostileHeaders) {
        const result = await setupAction({
          request: formRequest(
            "/account/setup",
            {
              idempotencyKey: IDEMPOTENCY_KEY,
              username: "nova88",
              displayName: "Nova",
            },
            headers,
          ),
        } as never);
        const { data, status } = dataOf<{ generalError: { error: { code: string } } }>(
          result,
        );
        expect(status).toBe(403);
        expect(data.generalError.error.code).toBe("forbidden");
      }
      expect(client.createAccount).not.toHaveBeenCalled();
    });

    it("rejects malformed forms and invalid idempotency keys without calling Python", async () => {
      const duplicatedBody = new URLSearchParams({
        idempotencyKey: IDEMPOTENCY_KEY,
        username: "nova88",
        displayName: "Nova",
      });
      const bad = new Request(`${ORIGIN}/account/setup`, {
        method: "POST",
        headers: { "content-type": "application/x-www-form-urlencoded", Origin: ORIGIN },
        body: `${duplicatedBody.toString()}&username=other`,
      });
      for (const request of [
        bad,
        formRequest("/account/setup", {
          idempotencyKey: "not-a-uuid",
          username: "nova88",
          displayName: "Nova",
        }),
        new Request(`${ORIGIN}/account/setup`, {
          method: "POST",
          headers: { "content-type": "application/json", Origin: ORIGIN },
          body: "{}",
        }),
      ]) {
        const result = await setupAction({ request } as never);
        const { data, status, headers } = dataOf<{
          generalError: { error: { code: string } };
        }>(result);
        expect(status).toBe(400);
        expect(data.generalError.error.code).toBe("invalid_input");
        assertNoStoreHeaders(headers);
      }
      expect(client.createAccount).not.toHaveBeenCalled();
    });

    it("exports a no-store headers policy", async () => {
      const { headers } = await import("../../app/routes/account.setup");
      expect(headers()).toEqual({ "Cache-Control": "no-store" });
    });
  });

  describe("account.profile", () => {
    it("loads the current names with a fresh idempotency key", async () => {
      const result = await profileLoader({
        request: new Request(`${ORIGIN}/account/profile`),
      } as never);
      expect(result).toMatchObject({
        username: "nova88",
        displayName: "Nova",
        error: null,
      });
      expect(UUID_PATTERN.test(result.idempotencyKey)).toBe(true);
      assertNoProviderData(result);
    });

    it("redirects an unresolved account to setup", async () => {
      client.getAccount = vi.fn(async () => {
        throw new PythonApiError(403, { error: "account_not_found" });
      });
      await expect(
        profileLoader({ request: new Request(`${ORIGIN}/account/profile`) } as never),
      ).rejects.toSatisfy(expectRedirectTo("/account/setup"));
    });

    it("returns a safe error when the account service is unavailable", async () => {
      client.getAccount = vi.fn(async () => {
        throw new PythonApiError(503, { error: "unavailable" });
      });
      const result = await profileLoader({
        request: new Request(`${ORIGIN}/account/profile`),
      } as never);
      expect(result.error?.error.code).toBe("unavailable");
      expect(result.username).toBe("");
      assertNoProviderData(result);
    });

    it("updates names with stored preferences and redirects", async () => {
      await expect(
        profileAction({
          request: formRequest("/account/profile", {
            idempotencyKey: IDEMPOTENCY_KEY,
            username: "nova88",
            displayName: "Nova Nova",
          }),
        } as never),
      ).rejects.toSatisfy(expectRedirectTo("/account/profile"));
      expect(client.updateAccount).toHaveBeenCalledWith(
        { username: "nova88", displayName: "Nova Nova", preferences: {} },
        IDEMPOTENCY_KEY,
      );
    });

    it("redirects an unresolved account to setup from the action too", async () => {
      client.getAccount = vi.fn(async () => {
        throw new PythonApiError(404, { error: "account_not_found" });
      });
      await expect(
        profileAction({
          request: formRequest("/account/profile", {
            idempotencyKey: IDEMPOTENCY_KEY,
            username: "nova88",
            displayName: "Nova",
          }),
        } as never),
      ).rejects.toSatisfy(expectRedirectTo("/account/setup"));
    });

    it("rejects bad names and cross-origin requests before calling Python", async () => {
      const badName = await profileAction({
        request: formRequest("/account/profile", {
          idempotencyKey: IDEMPOTENCY_KEY,
          username: "FuckYou",
          displayName: "Nova",
        }),
      } as never);
      expect(dataOf(badName).status).toBe(400);

      const crossOrigin = await profileAction({
        request: formRequest(
          "/account/profile",
          { idempotencyKey: IDEMPOTENCY_KEY, username: "nova88", displayName: "Nova" },
          { Origin: "https://evil.example" },
        ),
      } as never);
      expect(dataOf(crossOrigin).status).toBe(403);
      expect(client.updateAccount).not.toHaveBeenCalled();
      expect(client.getAccount).not.toHaveBeenCalled();
    });

    it("maps general Python failures to a safe 422 response", async () => {
      client.getAccount = vi.fn(async () => ({ ...ACCOUNT }));
      client.updateAccount = vi.fn(async () => {
        throw new PythonApiError(503, { error: "unavailable" });
      });
      const result = await profileAction({
        request: formRequest("/account/profile", {
          idempotencyKey: IDEMPOTENCY_KEY,
          username: "nova88",
          displayName: "Nova",
        }),
      } as never);
      const { data, status, headers } = dataOf<{
        generalError: { error: { code: string } };
      }>(result);
      expect(status).toBe(422);
      expect(data.generalError.error.code).toBe("unavailable");
      assertNoStoreHeaders(headers);
      assertNoProviderData(data);
    });

    it("exports a no-store headers policy", async () => {
      const { headers } = await import("../../app/routes/account.profile");
      expect(headers()).toEqual({ "Cache-Control": "no-store" });
    });
  });

  describe("account.saved-players", () => {
    it("loads players with fresh per-player remove keys", async () => {
      client.listSavedTags = vi.fn(async () => [
        { tag: TAG, name: "Alpha" },
        { tag: TAG2, name: null },
      ]);
      const result = await savedPlayersLoader({
        request: new Request(`${ORIGIN}/account/saved-players`),
      } as never);
      expect(result.players).toHaveLength(2);
      expect(UUID_PATTERN.test(result.addIdempotencyKey)).toBe(true);
      expect(UUID_PATTERN.test(result.removeIdempotencyKeys[TAG])).toBe(true);
      expect(UUID_PATTERN.test(result.removeIdempotencyKeys[TAG2])).toBe(true);
      expect(result.error).toBeNull();
      assertNoProviderData(result);
    });

    it("redirects an unresolved account to setup", async () => {
      client.listSavedTags = vi.fn(async () => {
        throw new PythonApiError(403, { error: "account_not_found" });
      });
      await expect(
        savedPlayersLoader({
          request: new Request(`${ORIGIN}/account/saved-players`),
        } as never),
      ).rejects.toSatisfy(expectRedirectTo("/account/setup"));
    });

    it("returns a safe error when listing fails for another reason", async () => {
      client.listSavedTags = vi.fn(async () => {
        throw new PythonApiError(503, { error: "unavailable" });
      });
      const result = await savedPlayersLoader({
        request: new Request(`${ORIGIN}/account/saved-players`),
      } as never);
      expect(result.error?.error.code).toBe("unavailable");
      expect(result.players).toEqual([]);
      assertNoProviderData(result);
    });

    it("adds and removes canonical tags through Python, then redirects", async () => {
      await expect(
        savedPlayersAction({
          request: formRequest("/account/saved-players", {
            mode: "add",
            tag: " p0lq2y8 ",
            idempotencyKey: IDEMPOTENCY_KEY,
          }),
        } as never),
      ).rejects.toSatisfy(expectRedirectTo("/account/saved-players"));
      expect(client.addSavedTag).toHaveBeenCalledWith(TAG, IDEMPOTENCY_KEY);

      await expect(
        savedPlayersAction({
          request: formRequest("/account/saved-players", {
            mode: "remove",
            tag: TAG,
            idempotencyKey: IDEMPOTENCY_KEY,
          }),
        } as never),
      ).rejects.toSatisfy(expectRedirectTo("/account/saved-players"));
      expect(client.removeSavedTag).toHaveBeenCalledWith(TAG, IDEMPOTENCY_KEY);
    });

    it("rejects invalid tags, modes, and cross-origin requests before calling Python", async () => {
      const invalidTag = await savedPlayersAction({
        request: formRequest("/account/saved-players", {
          mode: "add",
          tag: "not a tag",
          idempotencyKey: IDEMPOTENCY_KEY,
        }),
      } as never);
      const tagResult = dataOf<{ fieldErrors: Record<string, string> }>(invalidTag);
      expect(tagResult.status).toBe(400);
      expect(tagResult.data.fieldErrors.tag).toBeTruthy();

      const invalidMode = await savedPlayersAction({
        request: formRequest("/account/saved-players", {
          mode: "rename",
          tag: TAG,
          idempotencyKey: IDEMPOTENCY_KEY,
        }),
      } as never);
      expect(dataOf(invalidMode).status).toBe(400);

      const crossOrigin = await savedPlayersAction({
        request: formRequest(
          "/account/saved-players",
          { mode: "add", tag: TAG, idempotencyKey: IDEMPOTENCY_KEY },
          { Origin: "https://evil.example" },
        ),
      } as never);
      expect(dataOf(crossOrigin).status).toBe(403);
      expect(client.addSavedTag).not.toHaveBeenCalled();
      expect(client.removeSavedTag).not.toHaveBeenCalled();
    });

    it("redirects unresolved and maps general failures safely", async () => {
      client.addSavedTag = vi.fn(async () => {
        throw new PythonApiError(403, { error: "account_not_found" });
      });
      await expect(
        savedPlayersAction({
          request: formRequest("/account/saved-players", {
            mode: "add",
            tag: TAG,
            idempotencyKey: IDEMPOTENCY_KEY,
          }),
        } as never),
      ).rejects.toSatisfy(expectRedirectTo("/account/setup"));

      client.addSavedTag = vi.fn(async () => {
        throw new PythonApiError(503, { error: "unavailable" });
      });
      const result = await savedPlayersAction({
        request: formRequest("/account/saved-players", {
          mode: "add",
          tag: TAG,
          idempotencyKey: IDEMPOTENCY_KEY,
        }),
      } as never);
      const { data, status, headers } = dataOf<{
        generalError: { error: { code: string } };
      }>(result);
      expect(status).toBe(422);
      expect(data.generalError.error.code).toBe("unavailable");
      assertNoStoreHeaders(headers);
      assertNoProviderData(data);
    });

    it("exports a no-store headers policy", async () => {
      const { headers } = await import("../../app/routes/account.saved-players");
      expect(headers()).toEqual({ "Cache-Control": "no-store" });
    });
  });

  describe("account.groups", () => {
    it("loads groups with fresh per-group update and delete keys", async () => {
      client.listGroups = vi.fn(async () => [
        { groupId: GROUP_ID, name: "Clanmates", tags: [TAG] },
      ]);
      const result = await groupsLoader({
        request: new Request(`${ORIGIN}/account/groups`),
      } as never);
      expect(result.groups).toHaveLength(1);
      expect(UUID_PATTERN.test(result.createIdempotencyKey)).toBe(true);
      expect(UUID_PATTERN.test(result.updateIdempotencyKeys[GROUP_ID])).toBe(true);
      expect(UUID_PATTERN.test(result.deleteIdempotencyKeys[GROUP_ID])).toBe(true);
      expect(result.error).toBeNull();
      assertNoProviderData(result);
    });

    it("redirects an unresolved account to setup", async () => {
      client.listGroups = vi.fn(async () => {
        throw new PythonApiError(404, { error: "account_not_found" });
      });
      await expect(
        groupsLoader({ request: new Request(`${ORIGIN}/account/groups`) } as never),
      ).rejects.toSatisfy(expectRedirectTo("/account/setup"));
    });

    it("creates and updates groups through Python, then redirects", async () => {
      await expect(
        groupsAction({
          request: formRequest("/account/groups", {
            action: "create",
            name: " Clanmates ",
            tags: "p0lq2y8, #G2Y8P0LQ",
            idempotencyKey: IDEMPOTENCY_KEY,
          }),
        } as never),
      ).rejects.toSatisfy(expectRedirectTo("/account/groups"));
      expect(client.createGroup).toHaveBeenCalledWith(
        { name: "Clanmates", tags: [TAG2, TAG] },
        IDEMPOTENCY_KEY,
      );

      await expect(
        groupsAction({
          request: formRequest("/account/groups", {
            action: "update",
            groupId: GROUP_ID,
            name: "Clanmates",
            tags: "p0lq2y8",
            idempotencyKey: IDEMPOTENCY_KEY,
          }),
        } as never),
      ).rejects.toSatisfy(expectRedirectTo("/account/groups"));
      expect(client.updateGroup).toHaveBeenCalledWith(
        GROUP_ID,
        { name: "Clanmates", tags: [TAG] },
        IDEMPOTENCY_KEY,
      );
    });

    it("requires a confirmation checkbox before deleting a group", async () => {
      const unconfirmed = await groupsAction({
        request: formRequest("/account/groups", {
          action: "delete",
          groupId: GROUP_ID,
          idempotencyKey: IDEMPOTENCY_KEY,
        }),
      } as never);
      const { data, status, headers } = dataOf<{
        fieldErrors: Record<string, string>;
      }>(unconfirmed);
      expect(status).toBe(400);
      expect(data.fieldErrors.confirm).toBe("Confirm the deletion to continue.");
      assertNoStoreHeaders(headers);
      expect(client.deleteGroup).not.toHaveBeenCalled();

      await expect(
        groupsAction({
          request: formRequest("/account/groups", {
            action: "delete",
            groupId: GROUP_ID,
            confirm: "on",
            idempotencyKey: IDEMPOTENCY_KEY,
          }),
        } as never),
      ).rejects.toSatisfy(expectRedirectTo("/account/groups"));
      expect(client.deleteGroup).toHaveBeenCalledWith(GROUP_ID, IDEMPOTENCY_KEY);
    });

    it("rejects invalid names, tags, and group IDs before calling Python", async () => {
      const badName = await groupsAction({
        request: formRequest("/account/groups", {
          action: "create",
          name: "bad\u0000name",
          tags: "abc123",
          idempotencyKey: IDEMPOTENCY_KEY,
        }),
      } as never);
      expect(dataOf(badName).status).toBe(400);

      const badTags = await groupsAction({
        request: formRequest("/account/groups", {
          action: "create",
          name: "Clanmates",
          tags: "not a tag",
          idempotencyKey: IDEMPOTENCY_KEY,
        }),
      } as never);
      expect(dataOf(badTags).status).toBe(400);

      const badGroupId = await groupsAction({
        request: formRequest("/account/groups", {
          action: "update",
          groupId: "not-a-uuid",
          name: "Clanmates",
          tags: "abc123",
          idempotencyKey: IDEMPOTENCY_KEY,
        }),
      } as never);
      expect(dataOf(badGroupId).status).toBe(400);
      expect(client.createGroup).not.toHaveBeenCalled();
      expect(client.updateGroup).not.toHaveBeenCalled();
      expect(client.deleteGroup).not.toHaveBeenCalled();
    });

    it("rejects cross-origin requests before any mutation", async () => {
      const result = await groupsAction({
        request: formRequest(
          "/account/groups",
          {
            action: "create",
            name: "Clanmates",
            tags: "abc123",
            idempotencyKey: IDEMPOTENCY_KEY,
          },
          { Origin: "https://evil.example" },
        ),
      } as never);
      const { data, status } = dataOf<{ generalError: { error: { code: string } } }>(
        result,
      );
      expect(status).toBe(403);
      expect(data.generalError.error.code).toBe("forbidden");
      expect(client.createGroup).not.toHaveBeenCalled();
    });

    it("maps Python group outcomes: conflict, validation, missing, and general", async () => {
      client.createGroup = vi.fn(async () => {
        throw new PythonApiError(409, { error: "group_name_conflict" });
      });
      const conflict = await groupsAction({
        request: formRequest("/account/groups", {
          action: "create",
          name: "Clanmates",
          tags: TAG,
          idempotencyKey: IDEMPOTENCY_KEY,
        }),
      } as never);
      const conflictResult = dataOf<{ fieldErrors: Record<string, string> }>(conflict);
      expect(conflictResult.status).toBe(409);
      expect(conflictResult.data.fieldErrors.name).toBe(
        "A group with this name already exists.",
      );

      client.createGroup = vi.fn(async () => {
        throw new PythonApiError(422, { error: "invalid_request" });
      });
      const validation = await groupsAction({
        request: formRequest("/account/groups", {
          action: "create",
          name: "Clanmates",
          tags: TAG,
          idempotencyKey: IDEMPOTENCY_KEY,
        }),
      } as never);
      expect(dataOf(validation).status).toBe(422);

      client.deleteGroup = vi.fn(async () => {
        throw new PythonApiError(404, { error: "group_not_found" });
      });
      const missing = await groupsAction({
        request: formRequest("/account/groups", {
          action: "delete",
          groupId: GROUP_ID,
          confirm: "on",
          idempotencyKey: IDEMPOTENCY_KEY,
        }),
      } as never);
      const missingResult = dataOf<{ generalError: { error: { message: string } } }>(
        missing,
      );
      expect(missingResult.status).toBe(422);
      expect(missingResult.data.generalError.error.message).toBe(
        "The group no longer exists. Refresh the page.",
      );

      client.createGroup = vi.fn(async () => {
        throw new PythonApiError(503, { error: "unavailable" });
      });
      const unavailable = await groupsAction({
        request: formRequest("/account/groups", {
          action: "create",
          name: "Clanmates",
          tags: TAG,
          idempotencyKey: IDEMPOTENCY_KEY,
        }),
      } as never);
      const { data, status, headers } = dataOf<{
        generalError: { error: { code: string } };
      }>(unavailable);
      expect(status).toBe(422);
      expect(data.generalError.error.code).toBe("unavailable");
      assertNoStoreHeaders(headers);
      assertNoProviderData(data);
    });

    it("redirects an unresolved account to setup from the action", async () => {
      client.createGroup = vi.fn(async () => {
        throw new PythonApiError(403, { error: "account_not_found" });
      });
      await expect(
        groupsAction({
          request: formRequest("/account/groups", {
            action: "create",
            name: "Clanmates",
            tags: "p0lq2y8",
            idempotencyKey: IDEMPOTENCY_KEY,
          }),
        } as never),
      ).rejects.toSatisfy(expectRedirectTo("/account/setup"));
    });

    it("exports a no-store headers policy", async () => {
      const { headers } = await import("../../app/routes/account.groups");
      expect(headers()).toEqual({ "Cache-Control": "no-store" });
    });
  });

  describe("account.verify-player", () => {
    it("exposes only a fresh idempotency key from the loader", async () => {
      const result = await verifyPlayerLoader({
        request: new Request(`${ORIGIN}/account/verify-player`),
      } as never);
      expect(UUID_PATTERN.test(result.idempotencyKey)).toBe(true);
      assertNoProviderData(result);
    });

    it("returns safe status data and never echoes the one-time token", async () => {
      const result = await verifyPlayerAction({
        request: formRequest("/account/verify-player", {
          idempotencyKey: IDEMPOTENCY_KEY,
          tag: TAG.toLowerCase(),
          token: "SECRET-TOKEN-123",
        }),
      } as never);
      const { data, status, headers } = dataOf<{
        status: string;
        verificationRequestId: string | null;
        values: Record<string, string>;
        fieldErrors: Record<string, string>;
      }>(result);
      expect(status).toBe(200);
      expect(data.status).toBe("linked");
      expect(data.verificationRequestId).toBeNull();
      expect(data.values).toEqual({ tag: TAG });
      expect(data.fieldErrors).toEqual({});
      assertNoStoreHeaders(headers);
      const serialized = JSON.stringify(data);
      expect(serialized).not.toContain("SECRET-TOKEN-123");
      expect(serialized).not.toContain("token");
      assertNoProviderData(data);
      expect(client.verifyPlayerToken).toHaveBeenCalledWith(
        TAG,
        "SECRET-TOKEN-123",
        IDEMPOTENCY_KEY,
      );
    });

    it("safely includes the support request reference for support_required outcomes", async () => {
      client.verifyPlayerToken = vi.fn(async () => ({
        status: "support_required" as const,
        tag: TAG,
        verificationRequestId: "req-2026-0001",
      }));
      const result = await verifyPlayerAction({
        request: formRequest("/account/verify-player", {
          idempotencyKey: IDEMPOTENCY_KEY,
          tag: TAG,
          token: "SECRET-TOKEN-456",
        }),
      } as never);
      const { data } = dataOf<{
        status: string;
        verificationRequestId: string | null;
        values: Record<string, string>;
      }>(result);
      expect(data.status).toBe("support_required");
      expect(data.verificationRequestId).toBe("req-2026-0001");
      const serialized = JSON.stringify(data);
      expect(serialized).not.toContain("SECRET-TOKEN-456");
      expect(serialized).not.toContain("token");
    });

    it("rejects invalid tags and tokens before calling Python", async () => {
      const badTag = await verifyPlayerAction({
        request: formRequest("/account/verify-player", {
          idempotencyKey: IDEMPOTENCY_KEY,
          tag: "not a tag",
          token: "TOKEN123",
        }),
      } as never);
      expect(dataOf(badTag).status).toBe(400);

      const badToken = await verifyPlayerAction({
        request: formRequest("/account/verify-player", {
          idempotencyKey: IDEMPOTENCY_KEY,
          tag: TAG,
          token: "has space",
        }),
      } as never);
      const tokenResult = dataOf<{ fieldErrors: Record<string, string> }>(badToken);
      expect(tokenResult.status).toBe(400);
      expect(tokenResult.data.fieldErrors.token).toBeTruthy();

      const longToken = await verifyPlayerAction({
        request: formRequest("/account/verify-player", {
          idempotencyKey: IDEMPOTENCY_KEY,
          tag: TAG,
          token: "T".repeat(513),
        }),
      } as never);
      expect(dataOf(longToken).status).toBe(400);
      expect(client.verifyPlayerToken).not.toHaveBeenCalled();
    });

    it("rejects cross-origin requests before calling Python", async () => {
      const result = await verifyPlayerAction({
        request: formRequest(
          "/account/verify-player",
          { idempotencyKey: IDEMPOTENCY_KEY, tag: TAG, token: "TOKEN123" },
          { Origin: "https://evil.example" },
        ),
      } as never);
      const { data, status, headers } = dataOf<{
        generalError: { error: { code: string } };
      }>(result);
      expect(status).toBe(403);
      expect(data.generalError.error.code).toBe("forbidden");
      assertNoStoreHeaders(headers);
      expect(client.verifyPlayerToken).not.toHaveBeenCalled();
    });

    it("redirects unresolved and maps general failures without leaking the token", async () => {
      client.verifyPlayerToken = vi.fn(async () => {
        throw new PythonApiError(403, { error: "account_not_found" });
      });
      await expect(
        verifyPlayerAction({
          request: formRequest("/account/verify-player", {
            idempotencyKey: IDEMPOTENCY_KEY,
            tag: TAG,
            token: "SECRET-TOKEN-789",
          }),
        } as never),
      ).rejects.toSatisfy(expectRedirectTo("/account/setup"));

      client.verifyPlayerToken = vi.fn(async () => {
        throw new PythonApiError(503, { error: "unavailable" });
      });
      const result = await verifyPlayerAction({
        request: formRequest("/account/verify-player", {
          idempotencyKey: IDEMPOTENCY_KEY,
          tag: TAG,
          token: "SECRET-TOKEN-789",
        }),
      } as never);
      const { data, status, headers } = dataOf<{
        generalError: { error: { code: string } };
      }>(result);
      expect(status).toBe(422);
      expect(data.generalError.error.code).toBe("unavailable");
      assertNoStoreHeaders(headers);
      expect(JSON.stringify(data)).not.toContain("SECRET-TOKEN-789");
      assertNoProviderData(data);
    });

    it("exports a no-store headers policy", async () => {
      const { headers } = await import("../../app/routes/account.verify-player");
      expect(headers()).toEqual({ "Cache-Control": "no-store" });
    });
  });

  describe("account overview", () => {
    it("loads summary, saved players, and groups together", async () => {
      client.getAccountSummary = vi.fn(async () => ({
        ...SUMMARY,
        verifiedPlayers: [{ tag: TAG, name: "Alpha" }],
      }));
      client.listSavedTags = vi.fn(async () => [{ tag: TAG2, name: null }]);
      client.listGroups = vi.fn(async () => [
        { groupId: GROUP_ID, name: "Clanmates", tags: [TAG] },
      ]);
      const result = await accountLoader({
        request: new Request(`${ORIGIN}/account`),
      } as never);
      expect(result.summary?.verifiedPlayers).toHaveLength(1);
      expect(result.savedPlayers).toHaveLength(1);
      expect(result.groups).toHaveLength(1);
      expect(result.error).toBeNull();
      assertNoProviderData(result);
    });

    it("does not swallow the unresolved-account redirect to setup", async () => {
      client.getAccountSummary = vi.fn(async () => {
        throw new PythonApiError(403, { error: "account_not_found" });
      });
      await expect(
        accountLoader({ request: new Request(`${ORIGIN}/account`) } as never),
      ).rejects.toSatisfy(expectRedirectTo("/account/setup"));

      client.getAccountSummary = vi.fn(async () => ({ ...SUMMARY }));
      client.listGroups = vi.fn(async () => {
        throw new PythonApiError(404, { error: "account_not_found" });
      });
      await expect(
        accountLoader({ request: new Request(`${ORIGIN}/account`) } as never),
      ).rejects.toSatisfy(expectRedirectTo("/account/setup"));
    });

    it("keeps partial data and a safe error when one call fails for another reason", async () => {
      client.getAccountSummary = vi.fn(async () => {
        throw new PythonApiError(503, { error: "unavailable" });
      });
      const result = await accountLoader({
        request: new Request(`${ORIGIN}/account`),
      } as never);
      expect(result.summary).toBeNull();
      expect(result.error?.error.code).toBe("unavailable");
      expect(result.savedPlayers).toEqual([]);
      expect(result.groups).toEqual([]);
      assertNoProviderData(result);
    });

    it("exports a no-store headers policy", async () => {
      const { headers } = await import("../../app/routes/account");
      expect(headers()).toEqual({ "Cache-Control": "no-store" });
    });
  });
});
