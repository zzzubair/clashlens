import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getWebsiteConfig: vi.fn(),
}));

vi.mock("../../app/server/config.server", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/server/config.server")>();
  return { ...actual, getWebsiteConfig: mocks.getWebsiteConfig };
});

import {
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
  });

  it("returns the server identity for a valid signed login cookie", async () => {
    const config = testConfig();
    const cookie = createLoginCookieValue(IDENTITY, config.loginSecret, NOW_SECONDS);
    await expect(
      requireLogin(loginRequest("/account", `${LOGIN_COOKIE_NAME}=${cookie}`)),
    ).resolves.toEqual(IDENTITY);
  });

  it("redirects to /login with the current path as the return path when the cookie is missing", async () => {
    await expect(requireLogin(loginRequest("/account"))).rejects.toSatisfy(
      (thrown: unknown) => {
        expectLoginRedirect(thrown, "/login?returnPath=%2Faccount");
        return true;
      },
    );
  });

  it("redirects with the exact pathname for nested account routes", async () => {
    await expect(requireLogin(loginRequest("/account/saved-players"))).rejects.toSatisfy(
      (thrown: unknown) => {
        expectLoginRedirect(thrown, "/login?returnPath=%2Faccount%2Fsaved-players");
        return true;
      },
    );
  });

  it("redirects safely for tampered, malformed, and expired cookies", async () => {
    const config = testConfig();
    const cookie = createLoginCookieValue(IDENTITY, config.loginSecret, NOW_SECONDS);
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
      NOW_SECONDS - 86_401,
    );
    for (const value of [forged, expired, "junk"]) {
      await expect(
        requireLogin(loginRequest("/account", `${LOGIN_COOKIE_NAME}=${value}`)),
      ).rejects.toSatisfy((thrown: unknown) => {
        expectLoginRedirect(thrown, "/login?returnPath=%2Faccount");
        return true;
      });
    }
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
