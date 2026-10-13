import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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
  LOGIN_COOKIE_NAME,
  createLoginCookieValue,
  createLoginSessionBinding,
  parseLoginCookieValue,
} from "../../app/server/auth-cookies.server";
import { loadWebsiteConfig } from "../../app/server/config.server";
import { renewLoginCookie } from "../../app/server/login-renewal.server";

const CONFIG = loadWebsiteConfig({
  NODE_ENV: "test",
  CLASHLENS_LOGIN_ENABLED: "true",
  CLASHLENS_PUBLIC_ORIGIN: "https://clashlens.example",
  CLASHLENS_GOOGLE_CLIENT_ID: "test-client.apps.googleusercontent.com",
  CLASHLENS_GOOGLE_CLIENT_SECRET: "test-client-secret",
  CLASHLENS_DISCORD_CLIENT_ID: "1234567890123456789",
  CLASHLENS_DISCORD_CLIENT_SECRET: "discord-test-secret",
  CLASHLENS_LOGIN_SECRET_B64: "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8",
});
const IDENTITY = { provider: "google", providerSubject: "11223344556677889900" } as const;
const NOW_MS = 1_790_000_000_000;
const MINUTE_MS = 60_000;

function cookieIssuedMinutesAgo(minutes: number): string {
  return createLoginCookieValue(
    IDENTITY,
    CONFIG.loginSecret,
    NOW_MS - minutes * MINUTE_MS,
  );
}

async function visit(
  cookie: string | undefined,
  response = new Response("page", { headers: { "Cache-Control": "no-store" } }),
): Promise<string[]> {
  const request = new Request("https://clashlens.example/dashboard", {
    headers: cookie === undefined ? {} : { cookie: `${LOGIN_COOKIE_NAME}=${cookie}` },
  });
  const result = await renewLoginCookie(request, async () => response);
  expect(await result.text()).toBe("page");
  return result.headers.getSetCookie();
}

describe("login cookie renewal", () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"], now: NOW_MS });
    mocks.getWebsiteConfig.mockReturnValue(CONFIG);
    mocks.isLoginRevoked.mockReset();
    mocks.isLoginRevoked.mockResolvedValue(false);
  });
  afterEach(() => vi.useRealTimers());

  it("gives a visit with an hour-old login a fresh 30-day cookie for the same login", async () => {
    const old = cookieIssuedMinutesAgo(60);
    const [header, ...rest] = await visit(old);
    expect(rest).toEqual([]);
    expect(header).toMatch(
      new RegExp(
        `^${LOGIN_COOKIE_NAME}=[^;]+; Max-Age=2592000; Path=/; HttpOnly; SameSite=Lax; Secure$`,
      ),
    );
    const renewed = header.slice(LOGIN_COOKIE_NAME.length + 1, header.indexOf(";"));
    const signedInAtMs = NOW_MS - 60 * MINUTE_MS;
    expect(parseLoginCookieValue(renewed, CONFIG.loginSecret, NOW_MS / 1000)).toEqual({
      ...IDENTITY,
      issuedAtMs: NOW_MS,
      signedInAtMs,
    });
    expect(
      parseLoginCookieValue(renewed, CONFIG.loginSecret, NOW_MS / 1000 + 30 * 86_400),
    ).toBeNull();
    expect(createLoginSessionBinding(renewed)).toBe(createLoginSessionBinding(old));
    expect(mocks.isLoginRevoked).toHaveBeenCalledOnce();

    // Renewing the renewed cookie keeps when the login signed in.
    vi.setSystemTime(NOW_MS + 60 * MINUTE_MS);
    const [again] = await visit(renewed);
    expect(
      parseLoginCookieValue(
        again.slice(LOGIN_COOKIE_NAME.length + 1, again.indexOf(";")),
        CONFIG.loginSecret,
        NOW_MS / 1000 + 3_600,
      ),
    ).toEqual({ ...IDENTITY, issuedAtMs: NOW_MS + 60 * MINUTE_MS, signedInAtMs });
  });

  it("leaves a cookie under an hour old alone without asking the API", async () => {
    expect(await visit(cookieIssuedMinutesAgo(59))).toEqual([]);
    expect(mocks.isLoginRevoked).not.toHaveBeenCalled();
  });

  it("never renews a missing, expired, logged-out, or unconfirmed login", async () => {
    expect(await visit(undefined)).toEqual([]);
    expect(await visit(cookieIssuedMinutesAgo(30 * 24 * 60))).toEqual([]);
    mocks.isLoginRevoked.mockResolvedValueOnce(true);
    expect(await visit(cookieIssuedMinutesAgo(120))).toEqual([]);
    mocks.isLoginRevoked.mockRejectedValueOnce(new Error("API down"));
    expect(await visit(cookieIssuedMinutesAgo(120))).toEqual([]);
  });

  it("keeps a response's own login cookie and never adds one to a shared-cache response", async () => {
    const cleared = `${LOGIN_COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax`;
    const logout = new Response("page", {
      status: 302,
      headers: { "Set-Cookie": cleared },
    });
    expect(await visit(cookieIssuedMinutesAgo(120), logout)).toEqual([cleared]);
    const feed = new Response("page", {
      headers: { "Cache-Control": "public, max-age=300" },
    });
    expect(await visit(cookieIssuedMinutesAgo(120), feed)).toEqual([]);
  });
});
