import { beforeEach, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getWebsiteConfig: vi.fn(),
  isLoginRevoked: vi.fn(),
  getAccount: vi.fn(),
}));

vi.mock("../../app/server/login-session.server", () => ({
  isLoginRevoked: mocks.isLoginRevoked,
}));

vi.mock("../../app/server/config.server", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/server/config.server")>();
  return { ...actual, getWebsiteConfig: mocks.getWebsiteConfig };
});

vi.mock("../../app/services/python.server", () => ({
  createPythonClient: () => ({ getAccount: mocks.getAccount }),
}));

import {
  LOGIN_COOKIE_NAME,
  createLoginCookieValue,
  createLoginSessionBinding,
} from "../../app/server/auth-cookies.server";
import { loadWebsiteConfig } from "../../app/server/config.server";
import { loadRootNavigation } from "../../app/server/root-navigation.server";
import { PythonApiError } from "../../app/services/python-response.server";

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

function signedInVisit(
  cookie = createLoginCookieValue(IDENTITY, CONFIG.loginSecret, Date.now()),
): Request {
  return new Request("https://clashlens.example/leaderboards/tracked", {
    headers: { cookie: `${LOGIN_COOKIE_NAME}=${cookie}` },
  });
}

beforeEach(() => {
  mocks.getWebsiteConfig.mockReturnValue(CONFIG);
  mocks.isLoginRevoked.mockReset();
  mocks.isLoginRevoked.mockResolvedValue(false);
  mocks.getAccount.mockReset();
  mocks.getAccount.mockResolvedValue({ displayName: "Nav Scout", username: "navscout" });
});

it("names the signed-in account", async () => {
  expect(await loadRootNavigation(signedInVisit())).toMatchObject({
    loggedIn: true,
    accountLabel: "Nav Scout",
    accountUsername: "navscout",
  });
});

it("keeps a signed-in browser signed in when the logout check does not answer", async () => {
  for (const status of [503, 502, 500]) {
    mocks.isLoginRevoked.mockRejectedValueOnce(
      new PythonApiError(status, { error: "timeout" }),
    );
    const navigation = await loadRootNavigation(signedInVisit());
    expect(navigation).toMatchObject({
      loggedIn: true,
      accountLabel: null,
      accountUsername: null,
    });
    expect(navigation.logoutIdempotencyKey).toEqual(expect.any(String));
  }
});

it("signs out only on an answer: logged out, refused, no account, or no cookie", async () => {
  mocks.isLoginRevoked.mockResolvedValueOnce(true);
  expect((await loadRootNavigation(signedInVisit())).loggedIn).toBe(false);
  mocks.isLoginRevoked.mockRejectedValueOnce(
    new PythonApiError(403, { error: "caller_operation_not_authorized" }),
  );
  expect((await loadRootNavigation(signedInVisit())).loggedIn).toBe(false);
  mocks.getAccount.mockRejectedValueOnce(
    new PythonApiError(404, { error: "account_not_found" }),
  );
  expect((await loadRootNavigation(signedInVisit())).loggedIn).toBe(false);
  const noCookie = new Request("https://clashlens.example/");
  expect((await loadRootNavigation(noCookie)).loggedIn).toBe(false);
});

it("keys each login so a busy moment never shows another login's name", async () => {
  const first = createLoginCookieValue(IDENTITY, CONFIG.loginSecret, Date.now() - 2000);
  const named = await loadRootNavigation(signedInVisit(first));
  expect(named.loginKey).toEqual(expect.any(String));
  expect(JSON.stringify(named)).not.toContain(IDENTITY.providerSubject);
  expect(JSON.stringify(named)).not.toContain(createLoginSessionBinding(first));

  const renewed = createLoginCookieValue(IDENTITY, CONFIG.loginSecret, Date.now(), {
    sessionBinding: createLoginSessionBinding(first),
    signedInAtMs: Date.now() - 2000,
  });
  mocks.isLoginRevoked.mockRejectedValueOnce(
    new PythonApiError(503, { error: "timeout" }),
  );
  const sameLogin = await loadRootNavigation(signedInVisit(renewed));
  expect(sameLogin).toMatchObject({ loggedIn: true, accountLabel: null });
  expect(sameLogin.loginKey).toBe(named.loginKey);

  const other = createLoginCookieValue(
    { provider: "discord", providerSubject: "998877665544332211" },
    CONFIG.loginSecret,
    Date.now() - 1000,
  );
  mocks.isLoginRevoked.mockRejectedValueOnce(
    new PythonApiError(503, { error: "timeout" }),
  );
  const otherLogin = await loadRootNavigation(signedInVisit(other));
  expect(otherLogin).toMatchObject({ loggedIn: true, accountLabel: null });
  expect(otherLogin.loginKey).not.toBe(named.loginKey);
  expect(
    (await loadRootNavigation(new Request("https://clashlens.example/"))).loginKey,
  ).toBeNull();
});
