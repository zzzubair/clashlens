import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getWebsiteConfig: vi.fn(),
  createGoogleOidcService: vi.fn(),
  createPythonClient: vi.fn(),
}));

vi.mock("../../app/server/config.server", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/server/config.server")>();
  return { ...actual, getWebsiteConfig: mocks.getWebsiteConfig };
});

vi.mock("../../app/server/google-oidc.server", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../app/server/google-oidc.server")>();
  return { ...actual, createGoogleOidcService: mocks.createGoogleOidcService };
});

vi.mock("../../app/services/python.server", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../app/services/python.server")>();
  return { ...actual, createPythonClient: mocks.createPythonClient };
});

import {
  LOGIN_COOKIE_LIFETIME_SECONDS,
  LOGIN_COOKIE_NAME,
  OAUTH_COOKIE_NAME,
  OAUTH_TRANSACTION_LIFETIME_SECONDS,
  createLoginCookieValue,
  createOAuthTransactionCookieValue,
  parseLoginCookieValue,
  parseOAuthTransactionCookieValue,
} from "../../app/server/auth-cookies.server";
import { loadWebsiteConfig, type WebsiteConfig } from "../../app/server/config.server";
import {
  OAuthCallbackError,
  createOAuthTransaction,
  type OAuthTransaction,
} from "../../app/server/google-oidc.server";
import { PythonApiError } from "../../app/services/python.server";
import { loader as callbackLoader } from "../../app/routes/auth.google.callback";
import { loader as googleStartLoader } from "../../app/routes/auth.google";
import { loader as loginLoader } from "../../app/routes/login";
import { action as logoutAction, loader as logoutLoader } from "../../app/routes/logout";

const TEST_SECRET = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8";
const IDENTITY = { provider: "google", providerSubject: "11223344556677889900" } as const;
const ORIGIN = "https://clashlens.example";
const CALLBACK_ORIGIN = "https://accounts.google.com/o/oauth2/v2/auth";

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

function isResponse(value: unknown): value is Response {
  return value instanceof Response;
}

function testConfig(): WebsiteConfig {
  return loadWebsiteConfig({
    NODE_ENV: "test",
    CLASHLENS_LOGIN_ENABLED: "true",
    CLASHLENS_PUBLIC_ORIGIN: ORIGIN,
    CLASHLENS_GOOGLE_CLIENT_ID: "test-client.apps.googleusercontent.com",
    CLASHLENS_GOOGLE_CLIENT_SECRET: "test-client-secret",
    CLASHLENS_LOGIN_SECRET_B64: TEST_SECRET,
  });
}

function makeService(): {
  service: {
    authorizationUrl: ReturnType<typeof vi.fn>;
    validateCallback: ReturnType<typeof vi.fn>;
  };
  authorizationUrl: ReturnType<typeof vi.fn>;
  validateCallback: ReturnType<typeof vi.fn>;
} {
  const authorizationUrl = vi.fn((transaction: OAuthTransaction) => {
    const url = new URL(CALLBACK_ORIGIN);
    url.searchParams.set("scope", "openid");
    url.searchParams.set("response_type", "code");
    url.searchParams.set("redirect_uri", `${ORIGIN}/auth/google/callback`);
    url.searchParams.set("state", transaction.state);
    url.searchParams.set("nonce", transaction.nonce);
    url.searchParams.set("code_challenge", transaction.codeChallenge);
    url.searchParams.set("code_challenge_method", "S256");
    return url;
  });
  const validateCallback = vi.fn();
  return {
    service: { authorizationUrl, validateCallback },
    authorizationUrl,
    validateCallback,
  };
}

function oauthCookie(
  config: WebsiteConfig,
  returnPath = "/account",
  issuedAt = Math.floor(Date.now() / 1000),
): { transaction: OAuthTransaction; header: string } {
  const transaction = createOAuthTransaction(returnPath, issuedAt);
  return {
    transaction,
    header: `${OAUTH_COOKIE_NAME}=${createOAuthTransactionCookieValue(
      transaction,
      config.loginSecret,
    )}`,
  };
}

describe("login loader", () => {
  beforeEach(() => {
    mocks.getWebsiteConfig.mockReturnValue(testConfig());
  });

  it("reports login unavailable and a safe default path when login is disabled", async () => {
    mocks.getWebsiteConfig.mockReturnValue(
      loadWebsiteConfig({
        NODE_ENV: "test",
        CLASHLENS_LOGIN_ENABLED: "false",
        CLASHLENS_PUBLIC_ORIGIN: ORIGIN,
      }),
    );
    const result = await loginLoader({
      request: new Request(`${ORIGIN}/login`),
    } as never);
    expect(result).toEqual({ loginAvailable: false, returnPath: "/account" });
  });

  it("reports login unavailable when configuration fails", async () => {
    mocks.getWebsiteConfig.mockImplementation(() => {
      throw new Error("missing configuration");
    });
    const result = await loginLoader({
      request: new Request(`${ORIGIN}/login`),
    } as never);
    expect(result).toEqual({ loginAvailable: false, returnPath: "/account" });
  });

  it("returns the validated return path and login availability for an anonymous request", async () => {
    const result = await loginLoader({
      request: new Request(`${ORIGIN}/login?returnPath=%2Faccount%2Fprofile`),
    } as never);
    expect(result).toEqual({ loginAvailable: true, returnPath: "/account/profile" });
  });

  it("falls back to the default path for unsafe return paths", async () => {
    const result = await loginLoader({
      request: new Request(
        `${ORIGIN}/login?returnPath=${encodeURIComponent("https://evil.example/")}`,
      ),
    } as never);
    expect(result.returnPath).toBe("/account");
  });

  it("redirects an already-signed-in browser to the validated return path", async () => {
    const config = testConfig();
    const cookie = createLoginCookieValue(
      IDENTITY,
      config.loginSecret,
      Math.floor(Date.now() / 1000),
    );
    await expect(
      loginLoader({
        request: new Request(`${ORIGIN}/login?returnPath=%2Faccount%2Fgroups`, {
          headers: { cookie: `${LOGIN_COOKIE_NAME}=${cookie}` },
        }),
      } as never),
    ).rejects.toSatisfy((thrown: unknown) => {
      expect(isResponse(thrown)).toBe(true);
      expect((thrown as Response).status).toBe(302);
      expect((thrown as Response).headers.get("Location")).toBe("/account/groups");
      return true;
    });
  });

  it("exports a no-store headers policy", () => {
    expect(loginLoaderHeaders()).toEqual({ "Cache-Control": "no-store" });
  });
});

// Local reference keeps the headers export assertion readable.
import { headers as loginLoaderHeaders } from "../../app/routes/login";

describe("logout route", () => {
  beforeEach(() => {
    mocks.getWebsiteConfig.mockReturnValue(testConfig());
  });

  it("redirects GETs to home from the loader", async () => {
    await expect(logoutLoader()).rejects.toSatisfy((thrown: unknown) => {
      expect(isResponse(thrown)).toBe(true);
      expect((thrown as Response).headers.get("Location")).toBe("/");
      return true;
    });
  });

  it("redirects non-POST actions to home", async () => {
    await expect(
      logoutAction({
        request: new Request(`${ORIGIN}/logout`, { method: "GET" }),
      } as never),
    ).rejects.toSatisfy((thrown: unknown) => {
      expect(isResponse(thrown)).toBe(true);
      expect((thrown as Response).headers.get("Location")).toBe("/");
      return true;
    });
  });

  it("rejects cross-origin POSTs with 403 and never sets a cookie", async () => {
    const hostileHeaders: Array<Record<string, string>> = [
      { Origin: "https://evil.example" },
      { Referer: "https://evil.example/logout" },
      {},
    ];
    for (const headers of hostileHeaders) {
      const response = await logoutAction({
        request: new Request(`${ORIGIN}/logout`, { method: "POST", headers }),
      } as never);
      expect(isResponse(response)).toBe(true);
      expect((response as Response).status).toBe(403);
      expect((response as Response).headers.get("Cache-Control")).toBe("no-store");
      expect((response as Response).headers.getSetCookie()).toEqual([]);
    }
  });

  it("clears the login cookie on a same-origin POST and redirects home", async () => {
    const response = await logoutAction({
      request: new Request(`${ORIGIN}/logout`, {
        method: "POST",
        headers: { Origin: ORIGIN },
      }),
    } as never);
    expect(isResponse(response)).toBe(true);
    expect((response as Response).status).toBe(302);
    expect((response as Response).headers.get("Location")).toBe("/");
    expect((response as Response).headers.get("Cache-Control")).toBe("no-store");
    const setCookies = (response as Response).headers.getSetCookie();
    expect(setCookies).toHaveLength(1);
    expect(setCookies[0]).toBe(
      `${LOGIN_COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax; Secure`,
    );
  });

  it("accepts a same-origin Referer when Origin is absent", async () => {
    const response = await logoutAction({
      request: new Request(`${ORIGIN}/logout`, {
        method: "POST",
        headers: { Referer: `${ORIGIN}/account` },
      }),
    } as never);
    expect((response as Response).status).toBe(302);
  });

  it("redirects home when configuration fails", async () => {
    mocks.getWebsiteConfig.mockImplementation(() => {
      throw new Error("missing configuration");
    });
    await expect(
      logoutAction({
        request: new Request(`${ORIGIN}/logout`, {
          method: "POST",
          headers: { Origin: ORIGIN },
        }),
      } as never),
    ).rejects.toSatisfy((thrown: unknown) => {
      expect(isResponse(thrown)).toBe(true);
      expect((thrown as Response).headers.get("Location")).toBe("/");
      return true;
    });
  });

  it("exports a no-store headers policy", async () => {
    const { headers } = await import("../../app/routes/logout");
    expect(headers()).toEqual({ "Cache-Control": "no-store" });
  });
});

describe("auth.google start loader", () => {
  let service: ReturnType<typeof makeService>;

  beforeEach(() => {
    mocks.getWebsiteConfig.mockReturnValue(testConfig());
    service = makeService();
    mocks.createGoogleOidcService.mockResolvedValue(service.service);
  });

  it("sets only the OAuth transaction cookie and redirects to an openid-only provider URL", async () => {
    const response = await googleStartLoader({
      request: new Request(`${ORIGIN}/auth/google?returnPath=%2Faccount%2Fprofile`),
    } as never);
    expect(isResponse(response)).toBe(true);
    expect((response as Response).status).toBe(302);
    expect((response as Response).headers.get("Cache-Control")).toBe("no-store");

    const setCookies = (response as Response).headers.getSetCookie();
    expect(setCookies).toHaveLength(1);
    expect(setCookies[0]).toContain(`${OAUTH_COOKIE_NAME}=`);
    expect(setCookies[0]).toContain(`Max-Age=${OAUTH_TRANSACTION_LIFETIME_SECONDS}`);
    expect(setCookies[0]).toContain("Secure");
    expect(setCookies[0]).not.toContain(LOGIN_COOKIE_NAME);

    const cookieValue = setCookies[0].slice(
      setCookies[0].indexOf("=") + 1,
      setCookies[0].indexOf(";"),
    );
    const config = testConfig();
    const transaction = parseOAuthTransactionCookieValue(
      cookieValue,
      config.loginSecret,
      Math.floor(Date.now() / 1000),
    );
    expect(transaction).not.toBeNull();
    expect(transaction?.returnPath).toBe("/account/profile");
    expect(service.authorizationUrl).toHaveBeenCalledWith(transaction);

    const location = new URL((response as Response).headers.get("Location") ?? "");
    expect(location.origin + location.pathname).toBe(CALLBACK_ORIGIN);
    expect(location.searchParams.get("scope")).toBe("openid");
    expect(location.searchParams.get("response_type")).toBe("code");
    expect(location.searchParams.get("redirect_uri")).toBe(
      `${ORIGIN}/auth/google/callback`,
    );
    expect(location.searchParams.get("state")).toBe(transaction?.state);
    expect(location.searchParams.get("nonce")).toBe(transaction?.nonce);
    expect(location.searchParams.get("code_challenge")).toBe(transaction?.codeChallenge);
    expect(location.searchParams.get("code_challenge_method")).toBe("S256");
    expect(location.searchParams.size).toBe(7);
    expect(location.searchParams.get("access_type")).toBeNull();
    expect(location.searchParams.get("prompt")).toBeNull();
    expect(mocks.createGoogleOidcService).toHaveBeenCalledWith(config);
  });

  it("uses the safe default return path for missing or hostile returnPath values", async () => {
    for (const query of [
      "",
      "?returnPath=https%3A%2F%2Fevil.example%2F",
      "?returnPath=%2F%2Fevil.example",
    ]) {
      const response = await googleStartLoader({
        request: new Request(`${ORIGIN}/auth/google${query}`),
      } as never);
      const setCookies = (response as Response).headers.getSetCookie();
      const cookieValue = setCookies[0].slice(
        setCookies[0].indexOf("=") + 1,
        setCookies[0].indexOf(";"),
      );
      const transaction = parseOAuthTransactionCookieValue(
        cookieValue,
        testConfig().loginSecret,
        Math.floor(Date.now() / 1000),
      );
      expect(transaction?.returnPath).toBe("/account");
    }
  });

  it("redirects to /login when login is disabled or configuration fails", async () => {
    mocks.getWebsiteConfig.mockReturnValue(
      loadWebsiteConfig({
        NODE_ENV: "test",
        CLASHLENS_LOGIN_ENABLED: "false",
        CLASHLENS_PUBLIC_ORIGIN: ORIGIN,
      }),
    );
    await expect(
      googleStartLoader({ request: new Request(`${ORIGIN}/auth/google`) } as never),
    ).rejects.toSatisfy((thrown: unknown) => {
      expect(isResponse(thrown)).toBe(true);
      expect((thrown as Response).headers.get("Location")).toBe("/login");
      return true;
    });

    mocks.getWebsiteConfig.mockImplementation(() => {
      throw new Error("missing configuration");
    });
    await expect(
      googleStartLoader({ request: new Request(`${ORIGIN}/auth/google`) } as never),
    ).rejects.toSatisfy((thrown: unknown) => {
      expect(isResponse(thrown)).toBe(true);
      expect((thrown as Response).headers.get("Location")).toBe("/login");
      return true;
    });
  });

  it("redirects to /login when the OIDC service cannot be created", async () => {
    mocks.createGoogleOidcService.mockRejectedValue(new Error("discovery failed"));
    await expect(
      googleStartLoader({ request: new Request(`${ORIGIN}/auth/google`) } as never),
    ).rejects.toSatisfy((thrown: unknown) => {
      expect(isResponse(thrown)).toBe(true);
      expect((thrown as Response).headers.get("Location")).toBe("/login");
      return true;
    });
  });
});

describe("auth.google.callback loader", () => {
  let service: ReturnType<typeof makeService>;
  let config: WebsiteConfig;
  const now = () => Math.floor(Date.now() / 1000);

  beforeEach(() => {
    config = testConfig();
    mocks.getWebsiteConfig.mockReturnValue(config);
    service = makeService();
    mocks.createGoogleOidcService.mockResolvedValue(service.service);
    mocks.createPythonClient.mockReturnValue({
      getAccount: vi.fn(async () => ({
        username: "nova88",
        displayName: "Nova",
        preferences: {},
        providers: ["google"],
      })),
    } as never);
    service.validateCallback.mockResolvedValue(IDENTITY);
  });

  function expectClearOnly(setCookies: string[]): void {
    expect(setCookies).toHaveLength(1);
    expect(setCookies[0]).toBe(
      `${OAUTH_COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax; Secure`,
    );
  }

  it("clears the transaction, sets a 24-hour login cookie, and redirects to the safe path for an existing account", async () => {
    const { transaction, header } = oauthCookie(config, "/account/saved-players");
    const request = new Request(
      `${ORIGIN}/auth/google/callback?code=provider-code&state=${transaction.state}`,
      { headers: { cookie: header } },
    );
    const response = await callbackLoader({ request } as never);
    expect(isResponse(response)).toBe(true);
    expect((response as Response).status).toBe(302);
    expect((response as Response).headers.get("Cache-Control")).toBe("no-store");
    expect((response as Response).headers.get("Location")).toBe("/account/saved-players");
    expect(service.validateCallback).toHaveBeenCalledWith(
      { code: "provider-code", state: transaction.state },
      transaction,
    );

    const setCookies = (response as Response).headers.getSetCookie();
    expect(setCookies).toHaveLength(2);
    expect(setCookies[0]).toBe(
      `${OAUTH_COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax; Secure`,
    );
    expect(setCookies[1]).toContain(`${LOGIN_COOKIE_NAME}=`);
    expect(setCookies[1]).toContain(`Max-Age=${LOGIN_COOKIE_LIFETIME_SECONDS}`);
    const loginValue = setCookies[1].slice(
      setCookies[1].indexOf("=") + 1,
      setCookies[1].indexOf(";"),
    );
    expect(parseLoginCookieValue(loginValue, config.loginSecret, now())).toEqual(
      IDENTITY,
    );
  });

  it("redirects a documented account_not_found (403 and defensive 404) to setup with the login cookie", async () => {
    for (const status of [403, 404]) {
      mocks.createPythonClient.mockReturnValue({
        getAccount: vi.fn(async () => {
          throw new PythonApiError(status, { error: "account_not_found" });
        }),
      } as never);
      const { transaction, header } = oauthCookie(config, "/account");
      const response = await callbackLoader({
        request: new Request(
          `${ORIGIN}/auth/google/callback?code=provider-code&state=${transaction.state}`,
          { headers: { cookie: header } },
        ),
      } as never);
      expect(isResponse(response)).toBe(true);
      expect((response as Response).status).toBe(302);
      expect((response as Response).headers.get("Location")).toBe("/account/setup");
      const setCookies = (response as Response).headers.getSetCookie();
      expect(setCookies).toHaveLength(2);
      expect(setCookies[1]).toContain(`${LOGIN_COOKIE_NAME}=`);
      expect(
        parseLoginCookieValue(
          setCookies[1].slice(setCookies[1].indexOf("=") + 1, setCookies[1].indexOf(";")),
          config.loginSecret,
          now(),
        ),
      ).toEqual(IDENTITY);
    }
  });

  it("clears the transaction and does NOT set a login cookie when Python is unavailable", async () => {
    mocks.createPythonClient.mockReturnValue({
      getAccount: vi.fn(async () => {
        throw new PythonApiError(503, { error: "unavailable" });
      }),
    } as never);
    const { transaction, header } = oauthCookie(config, "/account");
    const result = await callbackLoader({
      request: new Request(
        `${ORIGIN}/auth/google/callback?code=provider-code&state=${transaction.state}`,
        { headers: { cookie: header } },
      ),
    } as never);
    const { data, status, headers } = dataOf<{ error: { code: string } | null }>(result);
    expect(status).toBe(503);
    expect(data.error?.code).toBe("unavailable");
    expect(headers["Cache-Control"]).toBe("no-store");
    expectClearOnly(Object.values({ "Set-Cookie": headers["Set-Cookie"] ?? "" }));
    expect(JSON.stringify(data)).not.toContain(LOGIN_COOKIE_NAME);
    expect(JSON.stringify(data)).not.toContain(IDENTITY.providerSubject);
  });

  it("clears the transaction for missing, duplicate, and tampered state with a safe 400 error", async () => {
    service.validateCallback.mockImplementation(
      async (params: Record<string, string>, tx: OAuthTransaction) => {
        if (params["state"] !== tx.state) throw new OAuthCallbackError("invalid_state");
        return IDENTITY;
      },
    );
    const { transaction, header } = oauthCookie(config, "/account");
    const cases: Array<{ search: string; cookie?: string; label: string }> = [
      { search: "?code=provider-code", cookie: header, label: "missing state" },
      {
        search: `?code=provider-code&state=${transaction.state}&state=duplicate`,
        cookie: header,
        label: "duplicate state",
      },
      {
        search: "?code=provider-code&state=wrong-state",
        cookie: header,
        label: "tampered state",
      },
      {
        search: "?code=provider-code&state=stale",
        cookie: undefined,
        label: "missing transaction",
      },
      {
        search: "?code=provider-code&state=overflow",
        cookie: `${OAUTH_COOKIE_NAME}=junk`,
        label: "tampered transaction",
      },
      {
        search: "?code=provider-code&state=expired",
        cookie: oauthCookie(config, "/account", now() - 601).header,
        label: "expired transaction",
      },
    ];
    for (const testCase of cases) {
      service.validateCallback.mockClear();
      const request = new Request(`${ORIGIN}/auth/google/callback${testCase.search}`, {
        headers: testCase.cookie === undefined ? {} : { cookie: testCase.cookie },
      });
      const result = await callbackLoader({ request } as never);
      const { data, status, headers } = dataOf<{ error: { code: string } | null }>(
        result,
      );
      expect(status, testCase.label).toBe(400);
      expect(data.error?.code, testCase.label).toBe("invalid_callback");
      expect(headers["Cache-Control"]).toBe("no-store");
      const setCookie = headers["Set-Cookie"] ?? "";
      expect(setCookie, testCase.label).toBe(
        `${OAUTH_COOKIE_NAME}=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax; Secure`,
      );
      const serialized = JSON.stringify(data);
      expect(serialized, testCase.label).not.toContain(IDENTITY.providerSubject);
      expect(serialized, testCase.label).not.toContain("provider-code");
      expect(serialized, testCase.label).not.toContain(transaction.state);
      expect(serialized, testCase.label).not.toContain(transaction.nonce);
      expect(serialized, testCase.label).not.toContain(transaction.codeVerifier);
      expect(serialized, testCase.label).not.toContain(transaction.codeChallenge);
    }
  });

  it("never exposes provider data on a provider-error callback", async () => {
    service.validateCallback.mockRejectedValue(new OAuthCallbackError("invalid_state"));
    const { transaction, header } = oauthCookie(config, "/account");
    const result = await callbackLoader({
      request: new Request(
        `${ORIGIN}/auth/google/callback?code=provider-code&state=${transaction.state}`,
        { headers: { cookie: header } },
      ),
    } as never);
    const { data, status } = dataOf<{ error: { code: string } | null }>(result);
    expect(status).toBe(400);
    const serialized = JSON.stringify(data);
    expect(serialized).not.toContain(IDENTITY.providerSubject);
    expect(serialized).not.toContain("provider-code");
    expect(serialized).not.toContain(transaction.state);
    expect(serialized).not.toContain(transaction.nonce);
    expect(serialized).not.toContain(transaction.codeVerifier);
    expect(serialized).not.toContain(transaction.codeChallenge);
  });

  it("treats a non-OAuth service failure as a 503 and still clears the transaction", async () => {
    service.validateCallback.mockRejectedValue(new Error("unexpected provider failure"));
    const { transaction, header } = oauthCookie(config, "/account");
    const result = await callbackLoader({
      request: new Request(
        `${ORIGIN}/auth/google/callback?code=provider-code&state=${transaction.state}`,
        { headers: { cookie: header } },
      ),
    } as never);
    const { status, headers } = dataOf(result);
    expect(status).toBe(503);
    expect(headers["Set-Cookie"]).toContain(`${OAUTH_COOKIE_NAME}=; Max-Age=0`);
  });

  it("returns a safe 503 with no cookies when configuration fails", async () => {
    mocks.getWebsiteConfig.mockImplementation(() => {
      throw new Error("missing configuration");
    });
    const result = await callbackLoader({
      request: new Request(`${ORIGIN}/auth/google/callback?code=x&state=y`),
    } as never);
    const { data, status, headers } = dataOf<{ error: { code: string } | null }>(result);
    expect(status).toBe(503);
    expect(data.error?.code).toBe("unavailable");
    expect(headers["Set-Cookie"]).toBeUndefined();
    expect(headers["Cache-Control"]).toBe("no-store");
  });

  it("exports a no-store headers policy", async () => {
    const { headers } = await import("../../app/routes/auth.google.callback");
    expect(headers()).toEqual({ "Cache-Control": "no-store" });
  });
});
