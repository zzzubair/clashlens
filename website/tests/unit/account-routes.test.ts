import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  RouterContextProvider,
  StaticRouterProvider,
} from "react-router";
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

vi.mock("../../app/server/login-session.server", () => ({
  isLoginRevoked: async () => false,
}));

vi.mock("../../app/services/python.server", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../app/services/python.server")>();
  return { ...actual, createPythonClient: mocks.createPythonClient };
});

import { loadWebsiteConfig, type WebsiteConfig } from "../../app/server/config.server";
import { PythonApiError, type PythonClient } from "../../app/services/python.server";
import { clearPublicRefreshLimits } from "../../app/server/abuse.server";
import { clientAddressContext } from "../../app/server/client-address.server";
import { loader as accountLoader } from "../../app/routes/account";
import GroupsRoute, {
  action as groupsAction,
  loader as groupsLoader,
} from "../../app/routes/account.groups";
import { loader as savedPlayersLoader } from "../../app/routes/account.saved-players";
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
const ACCOUNT = {
  username: "nova88",
  displayName: "Nova",
  preferences: {},
  providers: ["google"],
};
const SUMMARY = {
  username: "nova88",
  displayName: "Nova",
  verifiedPlayers: [],
  defaultPlayer: null,
  nextDefaultPlayer: null,
};
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
    listGroups: vi.fn(async () => ({ groups: [], season: "1791176400" })),
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
    setDefaultPlayer: vi.fn(async () => undefined),
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
      CLASHLENS_DISCORD_CLIENT_ID: "1234567890123456789",
      CLASHLENS_DISCORD_CLIENT_SECRET: "discord-test-secret",
      CLASHLENS_LOGIN_SECRET_B64: TEST_SECRET,
    });
    mocks.getWebsiteConfig.mockReturnValue(config);
    mocks.requireLogin.mockResolvedValue(IDENTITY);
    client = fakeClient();
    mocks.createPythonClient.mockReturnValue(client);
  });

  describe("account read failures", () => {
    async function renderAccount(
      path: string,
      Component: typeof GroupsRoute,
      loader: unknown,
    ) {
      const handler = createStaticHandler([{ path, Component, loader: loader as never }]);
      const context = await handler.query(new Request(`${ORIGIN}${path}`));
      if (context instanceof Response) throw new Error("unexpected route response");
      return renderToString(
        createElement(StaticRouterProvider, {
          router: createStaticRouter(handler.dataRoutes, context),
          context,
        }),
      ).replace(/<script[\s\S]*?<\/script>/g, "");
    }

    it.each([
      [
        "watchlists",
        GroupsRoute,
        groupsLoader,
        "listGroups",
        "Watchlists could not be loaded.",
        "No watchlists yet",
        "Create watchlist",
      ],
    ] as const)(
      "shows a retryable read failure and recovers on %s",
      async (
        route,
        Component,
        loader,
        read,
        message,
        unknownContent,
        independentAction,
      ) => {
        const original = client[read];
        client[read] = vi
          .fn()
          .mockRejectedValueOnce(new PythonApiError(503, { error: "unavailable" }))
          .mockImplementation(original) as never;
        const path = `/account/${route}`;
        const failedHtml = await renderAccount(path, Component, loader);
        expect(failedHtml).toContain(message);
        expect(failedHtml).toContain(`href="${path}">Try again</a>`);
        expect(failedHtml).not.toContain(unknownContent);
        expect(failedHtml).not.toContain("Saved data is still available");
        if (independentAction) expect(failedHtml).toContain(independentAction);

        const recoveredHtml = await renderAccount(path, Component, loader);
        expect(recoveredHtml).not.toContain(message);
        expect(recoveredHtml).toContain(unknownContent);
      },
    );

    it.each([
      [
        "groups",
        GroupsRoute,
        groupsLoader,
        groupsAction,
        "createGroup",
        { action: "create", name: "New watchlist" },
        "Clanmates",
      ],
    ] as const)(
      "keeps loaded values after a failed save on %s",
      async (route, Component, loader, action, write, fields, savedContent) => {
        client.listGroups = vi.fn(async () => ({
          season: "1791176400",
          groups: [{ groupId: GROUP_ID, name: "Clanmates", tags: [], players: [] }],
        }));
        client[write] = vi
          .fn()
          .mockRejectedValue(new PythonApiError(503, { error: "unavailable" }));
        const path = `/account/${route}`;
        const handler = createStaticHandler([
          { path, Component, loader: loader as never, action: action as never },
        ]);
        const context = await handler.query(
          formRequest(path, { ...fields, idempotencyKey: IDEMPOTENCY_KEY }),
        );
        if (context instanceof Response) throw new Error("unexpected route response");
        const html = renderToString(
          createElement(StaticRouterProvider, {
            router: createStaticRouter(handler.dataRoutes, context),
            context,
          }),
        ).replace(/<script[\s\S]*?<\/script>/g, "");
        expect(html).toContain(savedContent);
        expect(html).toContain('role="alert"');
        expect(html).not.toContain("could not be loaded");
      },
    );
  });

  describe("account.setup", () => {
    it("exposes only a fresh idempotency key from the loader", async () => {
      const result = await setupLoader({
        request: new Request(`${ORIGIN}/account/setup`),
      } as never);
      expect(UUID_PATTERN.test(result.idempotencyKey)).toBe(true);
      expect(result.returnPath).toBe("/account");
      assertNoProviderData(result);
    });

    it("keeps only a safe return path through setup", async () => {
      const returnPathOf = async (value: string) =>
        (
          await setupLoader({
            request: new Request(
              `${ORIGIN}/account/setup?returnPath=${encodeURIComponent(value)}`,
            ),
          } as never)
        ).returnPath;
      expect(await returnPathOf("/account/watchlists")).toBe("/account/watchlists");
      for (const unsafe of [
        "https://evil.example/account/watchlists",
        "//evil.example/account/watchlists",
        "/account/watchlists?days=3&sort=defense",
        "/account/setup",
      ]) {
        expect(await returnPathOf(unsafe)).toBe("/account");
      }

      const submit = (returnPath: string) =>
        setupAction({
          request: formRequest("/account/setup", {
            idempotencyKey: IDEMPOTENCY_KEY,
            username: "nova88",
            displayName: "Nova",
            returnPath,
          }),
        } as never);
      // A new account links a player first, then goes on.
      await expect(submit("/account/watchlists")).rejects.toSatisfy(
        expectRedirectTo("/account/verify-player?return=%2Faccount%2Fwatchlists"),
      );
      await expect(submit("https://evil.example/account/watchlists")).rejects.toSatisfy(
        expectRedirectTo("/account/verify-player"),
      );
      client.createAccount = vi.fn(async () => {
        throw new PythonApiError(409, { error: "account_exists" });
      });
      await expect(submit("/account/watchlists")).rejects.toSatisfy(
        expectRedirectTo("/account/watchlists"),
      );
      client.createAccount = vi.fn(async () => {
        throw new PythonApiError(403, { error: "account_not_found" });
      });
      await expect(submit("/account/watchlists")).rejects.toSatisfy(
        expectRedirectTo("/account/setup?returnPath=%2Faccount%2Fwatchlists"),
      );
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
      ).rejects.toSatisfy(expectRedirectTo("/account/verify-player"));
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

  describe("account.saved-players", () => {
    it("sends the old address on to Watchlists", async () => {
      await expect(savedPlayersLoader()).rejects.toSatisfy(
        expectRedirectTo("/account/watchlists"),
      );
    });
  });

  describe("account.groups", () => {
    it("loads watchlists with fresh per-group update and delete keys", async () => {
      client.listGroups = vi.fn(async () => ({
        season: "1791176400",
        groups: [
          {
            groupId: GROUP_ID,
            name: "Clanmates",
            tags: [TAG],
            players: [
              {
                tag: TAG,
                name: "Nova",
                trophies: 5400,
                seasonResetPending: false,
                state: "tracking" as const,
              },
            ],
          },
        ],
      }));
      const result = await groupsLoader({
        request: new Request(`${ORIGIN}/account/watchlists`),
      } as never);
      expect(result.groups).toHaveLength(1);
      expect(UUID_PATTERN.test(result.createIdempotencyKey)).toBe(true);
      expect(UUID_PATTERN.test(result.updateIdempotencyKeys[GROUP_ID])).toBe(true);
      expect(UUID_PATTERN.test(result.deleteIdempotencyKeys[GROUP_ID])).toBe(true);
      expect(result.error).toBeNull();
      assertNoProviderData(result);
    });

    it("redirects an unresolved account to setup, then back to watchlists", async () => {
      client.listGroups = vi.fn(async () => {
        throw new PythonApiError(404, { error: "account_not_found" });
      });
      await expect(
        groupsLoader({ request: new Request(`${ORIGIN}/account/watchlists`) } as never),
      ).rejects.toSatisfy(
        expectRedirectTo("/account/setup?returnPath=%2Faccount%2Fwatchlists"),
      );
    });

    it("creates a watchlist with a redirect and renames one in place", async () => {
      await expect(
        groupsAction({
          request: formRequest("/account/watchlists", {
            action: "create",
            name: " Clanmates ",
            idempotencyKey: IDEMPOTENCY_KEY,
          }),
        } as never),
      ).rejects.toSatisfy(expectRedirectTo(`/account/watchlists#group-${GROUP_ID}`));
      expect(client.createGroup).toHaveBeenCalledWith(
        { name: "Clanmates" },
        IDEMPOTENCY_KEY,
      );

      const renamed = await groupsAction({
        request: formRequest("/account/watchlists", {
          action: "update",
          groupId: GROUP_ID,
          name: "Clanmates",
          idempotencyKey: IDEMPOTENCY_KEY,
        }),
      } as never);
      expect(dataOf<{ notice: string }>(renamed).data.notice).toBe("Saved the new name.");
      expect(client.updateGroup).toHaveBeenCalledWith(
        GROUP_ID,
        { name: "Clanmates" },
        IDEMPOTENCY_KEY,
      );
    });

    it("stops a Clasher at ten watchlists and says why", async () => {
      client.listGroups = vi.fn(async () => ({
        season: "1791176400",
        groups: Array.from({ length: 10 }, (_, index) => ({
          groupId: `${GROUP_ID.slice(0, -2)}${String(index).padStart(2, "0")}`,
          name: `Watchlist ${index}`,
          tags: [],
          players: [],
        })),
      }));
      const handler = createStaticHandler([
        {
          path: "/account/watchlists",
          Component: GroupsRoute,
          loader: groupsLoader as never,
        },
      ]);
      const context = await handler.query(new Request(`${ORIGIN}/account/watchlists`));
      if (context instanceof Response) throw new Error("unexpected route response");
      const html = renderToString(
        createElement(StaticRouterProvider, {
          router: createStaticRouter(handler.dataRoutes, context),
          context,
        }),
      );
      expect(html).toMatch(
        /<button type="submit" class="button button-primary" disabled="">Create watchlist/,
      );
      expect(html).toContain("You have 10 watchlists, the most an account can have.");

      client.createGroup = vi.fn(async () => {
        throw new PythonApiError(422, { error: "group_limit_reached" });
      });
      const refused = await groupsAction({
        request: formRequest("/account/watchlists", {
          action: "create",
          name: "Eleventh",
          idempotencyKey: IDEMPOTENCY_KEY,
        }),
      } as never);
      const { status, data } = dataOf<{ fieldErrors: { name?: string } }>(refused);
      expect(status).toBe(422);
      expect(data.fieldErrors.name).toContain("the most an account can have");
    });

    it("requires a confirmation before deleting a watchlist", async () => {
      const unconfirmed = await groupsAction({
        request: formRequest("/account/watchlists", {
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

      const deleted = await groupsAction({
        request: formRequest("/account/watchlists", {
          action: "delete",
          groupId: GROUP_ID,
          confirm: "on",
          idempotencyKey: IDEMPOTENCY_KEY,
        }),
      } as never).catch((thrown: unknown) => thrown);
      expect(expectRedirectTo("/account/watchlists")(deleted)).toBe(true);
      // Replacing the history entry keeps Back from returning to the deleted group.
      expect((deleted as Response).headers.get("X-Remix-Replace")).toBe("true");
      expect(client.deleteGroup).toHaveBeenCalledWith(GROUP_ID, IDEMPOTENCY_KEY);
    });

    it("rejects invalid names, tags, and watchlist IDs before calling Python", async () => {
      const badName = await groupsAction({
        request: formRequest("/account/watchlists", {
          action: "create",
          name: "bad\u0000name",
          tags: "abc123",
          idempotencyKey: IDEMPOTENCY_KEY,
        }),
      } as never);
      expect(dataOf(badName).status).toBe(400);

      const badTags = await groupsAction({
        request: formRequest("/account/watchlists", {
          action: "add-player",
          groupId: GROUP_ID,
          tag: "not a tag",
          idempotencyKey: IDEMPOTENCY_KEY,
        }),
      } as never);
      expect(dataOf(badTags).status).toBe(400);

      const badGroupId = await groupsAction({
        request: formRequest("/account/watchlists", {
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
      expect(client.listGroups).not.toHaveBeenCalled();
    });

    it("rejects cross-origin requests before any mutation", async () => {
      const result = await groupsAction({
        request: formRequest(
          "/account/watchlists",
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

    it("maps Python watchlist outcomes: conflict, validation, missing, and general", async () => {
      client.createGroup = vi.fn(async () => {
        throw new PythonApiError(409, { error: "group_name_conflict" });
      });
      const conflict = await groupsAction({
        request: formRequest("/account/watchlists", {
          action: "create",
          name: "Clanmates",
          tags: TAG,
          idempotencyKey: IDEMPOTENCY_KEY,
        }),
      } as never);
      const conflictResult = dataOf<{ fieldErrors: Record<string, string> }>(conflict);
      expect(conflictResult.status).toBe(409);
      expect(conflictResult.data.fieldErrors.name).toBe(
        "A watchlist with this name already exists.",
      );

      client.createGroup = vi.fn(async () => {
        throw new PythonApiError(422, { error: "invalid_request" });
      });
      const validation = await groupsAction({
        request: formRequest("/account/watchlists", {
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
        request: formRequest("/account/watchlists", {
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
        "The watchlist no longer exists. Refresh the page.",
      );

      for (const [status, code] of [
        [503, "unavailable"],
        [502, "malformed"],
      ] as const) {
        client.createGroup = vi.fn(async () => {
          throw new PythonApiError(status, { error: code });
        });
        const unconfirmed = await groupsAction({
          request: formRequest("/account/watchlists", {
            action: "create",
            name: "Clanmates",
            tags: TAG,
            idempotencyKey: IDEMPOTENCY_KEY,
          }),
        } as never);
        const result = dataOf<{
          generalError: { error: { code: string; message: string } };
        }>(unconfirmed);
        expect(result.status).toBe(422);
        expect(result.data.generalError.error.code).toBe(code);
        expect(result.data.generalError.error.message).toBe(
          "Could not confirm the watchlist was created. Refresh the page before trying again.",
        );
        assertNoStoreHeaders(result.headers);
        assertNoProviderData(result.data);
      }
    });

    it("redirects an unresolved account to setup from the action", async () => {
      client.createGroup = vi.fn(async () => {
        throw new PythonApiError(403, { error: "account_not_found" });
      });
      await expect(
        groupsAction({
          request: formRequest("/account/watchlists", {
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
    beforeEach(() => clearPublicRefreshLimits());

    const visitor = (address: string | undefined = "203.0.113.9") => {
      const context = new RouterContextProvider();
      context.set(clientAddressContext, address);
      return context;
    };

    it("exposes only a fresh idempotency key from the loader", async () => {
      const result = await verifyPlayerLoader({
        request: new Request(`${ORIGIN}/account/verify-player`),
      } as never);
      expect(UUID_PATTERN.test(result.idempotencyKey)).toBe(true);
      assertNoProviderData(result);
    });

    it.each(["linked", "already_linked"] as const)(
      "returns to the account overview with the linked tag after %s without exposing the token",
      async (status) => {
        client.verifyPlayerToken = vi.fn(async () => ({ status, tag: TAG }));
        const result = await verifyPlayerAction({
          context: visitor(),
          request: formRequest("/account/verify-player", {
            idempotencyKey: IDEMPOTENCY_KEY,
            tag: TAG.toLowerCase(),
            token: "SECRET-TOKEN-123",
          }),
        } as never);
        expect(result).toBeInstanceOf(Response);
        const response = result as Response;
        expect(response.status).toBe(303);
        expect(response.headers.get("Location")).toBe(
          `/account?linked=${encodeURIComponent(TAG)}`,
        );
        expect(response.headers.get("Cache-Control")).toBe("no-store");
        const serialized =
          JSON.stringify([...response.headers]) + (await response.text());
        expect(serialized).not.toContain("SECRET-TOKEN-123");
        expect(serialized).not.toContain("token");
        assertNoProviderData(serialized);
        expect(client.verifyPlayerToken).toHaveBeenCalledWith(
          TAG,
          "SECRET-TOKEN-123",
          IDEMPOTENCY_KEY,
        );
      },
    );

    it.each(["invalid_token", "verification_unavailable", "in_progress"] as const)(
      "keeps the form available after %s without exposing the token",
      async (status) => {
        client.verifyPlayerToken = vi.fn(async () => ({ status, tag: TAG }));
        const result = await verifyPlayerAction({
          context: visitor(),
          request: formRequest("/account/verify-player", {
            idempotencyKey: IDEMPOTENCY_KEY,
            tag: TAG,
            token: "SECRET-TOKEN-123",
          }),
        } as never);
        const response = dataOf<{
          status: string;
          idempotencyKey: string;
          values: { tag: string };
        }>(result);
        expect(response.data.status).toBe(status);
        expect(response.data.idempotencyKey).not.toBe(IDEMPOTENCY_KEY);
        expect(response.data.values).toEqual({ tag: TAG });
        expect(JSON.stringify(response.data)).not.toContain("SECRET-TOKEN-123");
        assertNoStoreHeaders(response.headers);
        assertNoProviderData(response.data);
      },
    );

    it("safely includes the support request reference for support_required outcomes", async () => {
      client.verifyPlayerToken = vi.fn(async () => ({
        status: "support_required" as const,
        tag: TAG,
        verificationRequestId: "req-2026-0001",
      }));
      const result = await verifyPlayerAction({
        context: visitor(),
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
        context: visitor(),
        request: formRequest("/account/verify-player", {
          idempotencyKey: IDEMPOTENCY_KEY,
          tag: "not a tag",
          token: "TOKEN123",
        }),
      } as never);
      expect(dataOf(badTag).status).toBe(400);

      const badToken = await verifyPlayerAction({
        context: visitor(),
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
        context: visitor(),
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
        context: visitor(),
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
          context: visitor(),
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
        context: visitor(),
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

    it("stops one address's seventh attempt in a minute before calling Python", async () => {
      client.verifyPlayerToken = vi.fn(async () => ({
        status: "invalid_token" as const,
        tag: TAG,
      }));
      const attempt = (address?: string) =>
        verifyPlayerAction({
          context: visitor(address),
          request: formRequest("/account/verify-player", {
            idempotencyKey: crypto.randomUUID(),
            tag: TAG,
            token: "SECRET-TOKEN-123",
          }),
        } as never);
      for (let index = 0; index < 6; index += 1) {
        expect(dataOf(await attempt()).status).toBe(200);
      }
      const refused = dataOf<{ generalError: { error: { code: string } } }>(
        await attempt(),
      );
      expect(refused.status).toBe(429);
      expect(refused.data.generalError.error.code).toBe("rate_limited");
      assertNoStoreHeaders(refused.headers);
      expect(JSON.stringify(refused.data)).not.toContain("SECRET-TOKEN-123");
      expect(client.verifyPlayerToken).toHaveBeenCalledTimes(6);

      expect(dataOf(await attempt("203.0.113.10")).status).toBe(200);
      // No known address goes nowhere.
      expect(dataOf(await attempt("")).status).toBe(503);
      expect(client.verifyPlayerToken).toHaveBeenCalledTimes(7);
    });

    it("exports a no-store headers policy", async () => {
      const { headers } = await import("../../app/routes/account.verify-player");
      expect(headers()).toEqual({ "Cache-Control": "no-store" });
    });
  });

  describe("account overview", () => {
    it("opens the public profile even when watchlists are unavailable", async () => {
      client.getAccountSummary = vi.fn(async () => ({
        ...SUMMARY,
        verifiedPlayers: [{ tag: TAG, name: "Alpha" }],
      }));
      client.listGroups = vi.fn(async () => {
        throw new PythonApiError(503, { error: "unavailable" });
      });
      await expect(
        accountLoader({
          request: new Request(`${ORIGIN}/account`),
        } as never),
      ).rejects.toSatisfy(expectRedirectTo("/users/nova88"));
    });

    it("passes only a valid linked player tag on to the profile", async () => {
      client.getAccountSummary = vi.fn(async () => SUMMARY);
      await expect(
        accountLoader({
          request: new Request(`${ORIGIN}/account?linked=${encodeURIComponent(TAG)}`),
        } as never),
      ).rejects.toSatisfy(
        expectRedirectTo(`/users/nova88?linked=${encodeURIComponent(TAG)}`),
      );
      await expect(
        accountLoader({
          request: new Request(`${ORIGIN}/account?linked=not-a-tag`),
        } as never),
      ).rejects.toSatisfy(expectRedirectTo("/users/nova88"));
    });

    it.each([403, 404])(
      "redirects a missing account to setup after %s",
      async (status) => {
        client.getAccountSummary = vi.fn(async () => {
          throw new PythonApiError(status, { error: "account_not_found" });
        });
        await expect(
          accountLoader({ request: new Request(`${ORIGIN}/account`) } as never),
        ).rejects.toSatisfy(expectRedirectTo("/account/setup"));
      },
    );

    it("shows a safe error when the account service is unavailable", async () => {
      client.getAccountSummary = vi.fn(async () => {
        throw new PythonApiError(503, { error: "unavailable" });
      });
      const result = await accountLoader({
        request: new Request(`${ORIGIN}/account`),
      } as never);
      expect(result.summary).toBeNull();
      expect(result.error?.error.code).toBe("unavailable");
      assertNoProviderData(result);
    });

    it("exports a no-store headers policy", async () => {
      const { headers } = await import("../../app/routes/account");
      expect(headers()).toEqual({ "Cache-Control": "no-store" });
    });
  });
});
