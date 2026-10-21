import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getWebsiteConfig: vi.fn(),
  isLoginRevoked: vi.fn(),
}));

vi.mock("../../app/server/login-session.server", () => ({
  isLoginRevoked: mocks.isLoginRevoked,
}));

vi.mock("../../app/server/config.server", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/server/config.server")>();
  return { ...actual, getWebsiteConfig: mocks.getWebsiteConfig };
});

import {
  LOGIN_COOKIE_LIFETIME_SECONDS,
  LOGIN_COOKIE_NAME,
  createLoginCookieValue,
} from "../../app/server/auth-cookies.server";
import { requireLogin } from "../../app/server/auth-guard.server";
import { loadWebsiteConfig, type WebsiteConfig } from "../../app/server/config.server";

const TEST_SECRET = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8";
const IDENTITY = { provider: "google", providerSubject: "11223344556677889900" } as const;
const NOW_SECONDS = Math.floor(Date.now() / 1000);

function testConfig(): WebsiteConfig {
  return loadWebsiteConfig({
    NODE_ENV: "test",
    CLASHLENS_LOGIN_ENABLED: "true",
    CLASHLENS_PUBLIC_ORIGIN: "https://clashlens.example",
    CLASHLENS_GOOGLE_CLIENT_ID: "test-client.apps.googleusercontent.com",
    CLASHLENS_GOOGLE_CLIENT_SECRET: "test-client-secret",
    CLASHLENS_DISCORD_CLIENT_ID: "1234567890123456789",
    CLASHLENS_DISCORD_CLIENT_SECRET: "discord-test-secret",
    CLASHLENS_LOGIN_SECRET_B64: TEST_SECRET,
  });
}

function loginRequest(path = "/account", cookie?: string): Request {
  return new Request(`https://clashlens.example${path}`, {
    headers: cookie === undefined ? {} : { cookie },
  });
}

// A page opened signed out answers 401 and shows its "needs an account" box.
function expectNeedsAccount(thrown: unknown, returnPath: string | null): void {
  const answer = thrown as { data: unknown; init: ResponseInit | null };
  expect(answer.init?.status).toBe(401);
  expect(answer.data).toEqual({ needsAccount: true, returnPath });
  const text = JSON.stringify(answer.data);
  expect(text).not.toContain(IDENTITY.providerSubject);
  expect(text.toLowerCase()).not.toContain("provider");
}

function expectLoginRedirect(thrown: unknown, expectedLocation: string): void {
  expect(thrown).toBeInstanceOf(Response);
  const response = thrown as Response;
  expect(response.status).toBe(302);
  expect(response.headers.get("Location")).toBe(expectedLocation);
  const decoded = decodeURIComponent(response.headers.get("Location") ?? "");
  expect(decoded).not.toContain(IDENTITY.providerSubject);
  expect(decoded.toLowerCase()).not.toContain("provider");
}

describe("requireLogin auth guard", () => {
  beforeEach(() => {
    mocks.getWebsiteConfig.mockReturnValue(testConfig());
    mocks.isLoginRevoked.mockReset();
    mocks.isLoginRevoked.mockResolvedValue(false);
  });

  it("returns the server identity for a valid signed login cookie", async () => {
    const config = testConfig();
    const cookie = createLoginCookieValue(
      IDENTITY,
      config.loginSecret,
      NOW_SECONDS * 1000,
    );
    await expect(
      requireLogin(loginRequest("/account", `${LOGIN_COOKIE_NAME}=${cookie}`)),
    ).resolves.toEqual(IDENTITY);
  });

  it("sends a form without a login to /login, coming back to its page", async () => {
    const request = new Request("https://clashlens.example/account/watchlists", {
      method: "POST",
    });
    await expect(requireLogin(request)).rejects.toSatisfy((thrown: unknown) => {
      expectLoginRedirect(thrown, "/login?returnPath=%2Faccount%2Fwatchlists");
      return true;
    });
  });

  it("keeps a player tag's # in the return path", async () => {
    await expect(
      requireLogin(loginRequest("/account/watchlists/add/%232PP")),
    ).rejects.toSatisfy((thrown: unknown) => {
      expectNeedsAccount(thrown, "/account/watchlists/add/%232PP");
      return true;
    });
  });

  it("answers a page without a login with its needs-an-account box, coming back to it", async () => {
    await expect(requireLogin(loginRequest("/account"))).rejects.toSatisfy(
      (thrown: unknown) => {
        expectNeedsAccount(thrown, "/account");
        return true;
      },
    );
  });

  it("comes back to the exact pathname for nested account routes", async () => {
    await expect(requireLogin(loginRequest("/account/saved-players"))).rejects.toSatisfy(
      (thrown: unknown) => {
        expectNeedsAccount(thrown, "/account/saved-players");
        return true;
      },
    );
  });

  it("returns to the page, not its data, when an in-app link needs sign-in", async () => {
    for (const path of [
      "/account/groups.data",
      "/account/groups.data?_routes=root%2Croutes%2Faccount.groups",
    ]) {
      await expect(requireLogin(loginRequest(path))).rejects.toSatisfy(
        (thrown: unknown) => {
          expectNeedsAccount(thrown, "/account/groups");
          return true;
        },
      );
    }
    await expect(
      requireLogin(loginRequest("/account/setup.data?returnPath=%2Faccount%2Fgroups")),
    ).rejects.toSatisfy((thrown: unknown) => {
      expectNeedsAccount(thrown, "/account/groups");
      return true;
    });
  });

  it("keeps account setup's destination when its login has expired", async () => {
    const config = testConfig();
    const expired = createLoginCookieValue(
      IDENTITY,
      config.loginSecret,
      (NOW_SECONDS - LOGIN_COOKIE_LIFETIME_SECONDS - 1) * 1000,
    );
    await expect(
      requireLogin(
        loginRequest(
          "/account/setup?returnPath=%2Faccount%2Fgroups",
          `${LOGIN_COOKIE_NAME}=${expired}`,
        ),
      ),
    ).rejects.toSatisfy((thrown: unknown) => {
      expectNeedsAccount(thrown, "/account/groups");
      return true;
    });
  });

  it("falls back to account setup when its destination is unsafe", async () => {
    for (const destination of [
      "https%3A%2F%2Fevil.example",
      "%2F%2Fevil.example",
      "%2Faccount%2Fgroups%3Fdays%3D7",
      "%2Faccount%2Fsetup",
    ]) {
      await expect(
        requireLogin(loginRequest(`/account/setup?returnPath=${destination}`)),
      ).rejects.toSatisfy((thrown: unknown) => {
        expectNeedsAccount(thrown, "/account/setup");
        return true;
      });
    }
  });

  it("treats tampered, malformed, and expired cookies as signed out", async () => {
    const config = testConfig();
    const cookie = createLoginCookieValue(
      IDENTITY,
      config.loginSecret,
      NOW_SECONDS * 1000,
    );
    const [, signaturePart] = cookie.split(".");
    const forged = `${Buffer.from(
      JSON.stringify({
        v: 1,
        p: "google",
        s: "attacker-subject",
        i: NOW_SECONDS,
        e: NOW_SECONDS + 86_400,
      }),
    ).toString("base64url")}.${signaturePart}`;
    const expired = createLoginCookieValue(
      IDENTITY,
      config.loginSecret,
      (NOW_SECONDS - LOGIN_COOKIE_LIFETIME_SECONDS - 1) * 1000,
    );
    for (const value of [forged, expired, "junk"]) {
      await expect(
        requireLogin(loginRequest("/account", `${LOGIN_COOKIE_NAME}=${value}`)),
      ).rejects.toSatisfy((thrown: unknown) => {
        expectNeedsAccount(thrown, "/account");
        return true;
      });
    }
  });

  it("treats a logged-out login as signed out and answers 503 when the API cannot check it", async () => {
    const config = testConfig();
    const cookie = createLoginCookieValue(
      IDENTITY,
      config.loginSecret,
      NOW_SECONDS * 1000,
    );
    const request = loginRequest("/account", `${LOGIN_COOKIE_NAME}=${cookie}`);
    mocks.isLoginRevoked.mockResolvedValueOnce(true);
    await expect(requireLogin(request)).rejects.toSatisfy((thrown: unknown) => {
      expectNeedsAccount(thrown, "/account");
      return true;
    });
    mocks.isLoginRevoked.mockRejectedValueOnce(new Error("unavailable"));
    await expect(requireLogin(request)).rejects.toSatisfy((thrown: unknown) => {
      expect(thrown).toBeInstanceOf(Response);
      expect((thrown as Response).status).toBe(503);
      expect((thrown as Response).headers.get("Cache-Control")).toBe("no-store");
      return true;
    });
  });

  it("redirects to plain /login when login is disabled", async () => {
    mocks.getWebsiteConfig.mockReturnValue(
      loadWebsiteConfig({
        NODE_ENV: "test",
        CLASHLENS_LOGIN_ENABLED: "false",
        CLASHLENS_PUBLIC_ORIGIN: "https://clashlens.example",
      }),
    );
    await expect(requireLogin(loginRequest("/account"))).rejects.toSatisfy(
      (thrown: unknown) => {
        expectLoginRedirect(thrown, "/login");
        return true;
      },
    );
  });

  it("redirects to plain /login when configuration fails", async () => {
    mocks.getWebsiteConfig.mockImplementation(() => {
      throw new Error("missing configuration");
    });
    await expect(requireLogin(loginRequest("/account"))).rejects.toSatisfy(
      (thrown: unknown) => {
        expectLoginRedirect(thrown, "/login");
        return true;
      },
    );
  });
});
