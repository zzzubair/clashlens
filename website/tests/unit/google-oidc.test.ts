import { createHash } from "node:crypto";

import { describe, expect, it, vi } from "vitest";

import { loadWebsiteConfig } from "../../app/server/config.server";
import type { WebsiteConfig } from "../../app/server/config.server";
import {
  CALLBACK_PATH,
  GOOGLE_ISSUER,
  MAX_PROVIDER_SUBJECT_LENGTH,
  OAUTH_TRANSACTION_LIFETIME_SECONDS,
  OAuthCallbackError,
  constantTimeEqual,
  createGoogleOidcService,
  createOAuthTransaction,
  isProviderSubject,
  isPlausibleTransaction,
  redirectUriFor,
} from "../../app/server/google-oidc.server";
import type {
  OidcServiceDeps,
  OAuthTransaction,
  ValidatedGoogleIdentity,
} from "../../app/server/google-oidc.server";

const TEST_SECRET = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8";
const ISSUER = "http://localhost:9999/";
const CLIENT_ID = "test-client.apps.googleusercontent.com";
const SUBJECT = "11223344556677889900";

function testConfig(): WebsiteConfig {
  return loadWebsiteConfig({
    NODE_ENV: "test",
    CLASHLENS_LOGIN_ENABLED: "true",
    CLASHLENS_PUBLIC_ORIGIN: "https://clashlens.example",
    CLASHLENS_GOOGLE_CLIENT_ID: CLIENT_ID,
    CLASHLENS_GOOGLE_CLIENT_SECRET: "test-client-secret",
    CLASHLENS_DISCORD_CLIENT_ID: "1234567890123456789",
    CLASHLENS_DISCORD_CLIENT_SECRET: "discord-test-secret",
    CLASHLENS_LOGIN_SECRET_B64: TEST_SECRET,
    CLASHLENS_GOOGLE_ISSUER_URL: ISSUER,
  });
}

function googleConfig(): WebsiteConfig {
  return loadWebsiteConfig({
    NODE_ENV: "test",
    CLASHLENS_LOGIN_ENABLED: "true",
    CLASHLENS_PUBLIC_ORIGIN: "https://clashlens.example",
    CLASHLENS_GOOGLE_CLIENT_ID: CLIENT_ID,
    CLASHLENS_GOOGLE_CLIENT_SECRET: "test-client-secret",
    CLASHLENS_DISCORD_CLIENT_ID: "1234567890123456789",
    CLASHLENS_DISCORD_CLIENT_SECRET: "discord-test-secret",
    CLASHLENS_LOGIN_SECRET_B64: TEST_SECRET,
  });
}

function fixedRandom(size: number): Buffer {
  return Buffer.alloc(size, 0x2a);
}

function transaction(now = 1_000_000): OAuthTransaction {
  return createOAuthTransaction("/account", now, fixedRandom, "login");
}

function validClaims(now = 1_000_000, overrides: Record<string, unknown> = {}): unknown {
  return {
    iss: ISSUER,
    aud: CLIENT_ID,
    exp: now + 300,
    nonce: transaction(now).nonce,
    sub: SUBJECT,
    ...overrides,
  };
}

interface FakeDeps {
  deps: OidcServiceDeps;
  discovered: unknown;
  buildAuthorizationUrl: ReturnType<typeof vi.fn>;
  authorizationCodeGrant: ReturnType<typeof vi.fn>;
  discovery: ReturnType<typeof vi.fn>;
}

function fakeDeps(claims: () => unknown = () => validClaims()): FakeDeps {
  const discovered = { fake: "configuration" };
  const discovery = vi.fn(async () => discovered);
  const buildAuthorizationUrl = vi.fn(
    (_config: unknown, parameters: Record<string, string>) => {
      const url = new URL("https://accounts.google.com/o/oauth2/v2/auth");
      for (const [key, value] of Object.entries(parameters)) {
        url.searchParams.append(key, value);
      }
      return url;
    },
  );
  const authorizationCodeGrant = vi.fn(async () => ({ claims }));
  return {
    deps: { discovery, buildAuthorizationUrl, authorizationCodeGrant },
    discovered,
    buildAuthorizationUrl,
    authorizationCodeGrant,
    discovery,
  };
}

function expectOAuthError(error: unknown, code: string): void {
  expect(error).toBeInstanceOf(OAuthCallbackError);
  expect(error).toMatchObject({ code });
  expect((error as Error).message).toBe("Google sign-in could not be completed");
  expect((error as Error).name).toBe("OAuthCallbackError");
}

async function expectCallbackError(
  service: Awaited<ReturnType<typeof createGoogleOidcService>>,
  callbackParams: Record<string, string>,
  oauthTransaction: OAuthTransaction,
  code: string,
  now?: number,
): Promise<void> {
  await expect(
    service.validateCallback(callbackParams, oauthTransaction, now),
  ).rejects.toSatisfy((error: unknown) => {
    expectOAuthError(error, code);
    return true;
  });
}

describe("OAuth transaction creation", () => {
  it("derives bounded state, nonce, and PKCE S256 challenge from injected randomness", () => {
    const oauthTransaction = transaction(1_750_000);
    expect(oauthTransaction.state).toBe(Buffer.alloc(24, 0x2a).toString("base64url"));
    expect(oauthTransaction.state).toBe(oauthTransaction.nonce);
    expect(oauthTransaction.codeVerifier).toBe(
      Buffer.alloc(32, 0x2a).toString("base64url"),
    );
    const expectedChallenge = createHash("sha256")
      .update(oauthTransaction.codeVerifier, "utf8")
      .digest()
      .toString("base64url");
    expect(oauthTransaction.codeChallenge).toBe(expectedChallenge);
    expect(oauthTransaction.returnPath).toBe("/account");
    expect(oauthTransaction.issuedAt).toBe(1_750_000);
    expect(oauthTransaction.expiresAt).toBe(
      1_750_000 + OAUTH_TRANSACTION_LIFETIME_SECONDS,
    );
    expect(isPlausibleTransaction(oauthTransaction)).toBe(true);
  });

  it("rejects implausible transaction shapes", () => {
    const oauthTransaction = transaction();
    expect(isPlausibleTransaction({ ...oauthTransaction, state: "short" })).toBe(false);
    expect(isPlausibleTransaction({ ...oauthTransaction, codeVerifier: "x" })).toBe(
      false,
    );
    expect(
      isPlausibleTransaction({
        ...oauthTransaction,
        expiresAt: oauthTransaction.issuedAt + 60,
      }),
    ).toBe(false);
    expect(isPlausibleTransaction({ ...oauthTransaction, issuedAt: 1.5 })).toBe(false);
  });
});

describe("provider subject bounds", () => {
  it("accepts a bounded plain Google subject", () => {
    expect(isProviderSubject("11223344556677889900")).toBe(true);
    expect(isProviderSubject("x".repeat(MAX_PROVIDER_SUBJECT_LENGTH))).toBe(true);
  });

  it.each(["", "a b", "a\nb", "a\u0000b", `x${"y".repeat(128)}`])(
    "rejects provider subject %j",
    (value) => {
      expect(isProviderSubject(value)).toBe(false);
    },
  );
});

describe("constant-time state and nonce comparison", () => {
  it("matches identical values", () => {
    expect(constantTimeEqual("abc123", "abc123")).toBe(true);
  });

  it("rejects length-mismatched and differing values", () => {
    expect(constantTimeEqual("abc", "abcd")).toBe(false);
    expect(constantTimeEqual("abc123", "abc124")).toBe(false);
    expect(constantTimeEqual("", "x")).toBe(false);
  });
});

describe("Google OIDC service", () => {
  it("accepts the exact Google issuer without an added trailing slash", async () => {
    const { deps } = fakeDeps(() => validClaims(1_000_000, { iss: GOOGLE_ISSUER }));
    const service = await createGoogleOidcService(googleConfig(), deps);
    const oauthTransaction = transaction();

    await expect(
      service.validateCallback(
        { state: oauthTransaction.state, code: "authorization-code" },
        oauthTransaction,
        1_000_100,
      ),
    ).resolves.toEqual({ provider: "google", providerSubject: SUBJECT });
  });

  it("builds the exact authorization URL with openid-only scope and PKCE S256", async () => {
    const { deps, buildAuthorizationUrl, discovery } = fakeDeps();
    const service = await createGoogleOidcService(testConfig(), deps);
    const oauthTransaction = transaction();
    const url = service.authorizationUrl(oauthTransaction);

    expect(discovery).toHaveBeenCalledWith(new URL(ISSUER), CLIENT_ID, {
      client_secret: "test-client-secret",
    });
    expect(buildAuthorizationUrl).toHaveBeenCalledWith(
      { fake: "configuration" },
      expect.objectContaining({
        scope: "openid",
        response_type: "code",
        redirect_uri: redirectUriFor(new URL("https://clashlens.example")),
        state: oauthTransaction.state,
        nonce: oauthTransaction.nonce,
        code_challenge: oauthTransaction.codeChallenge,
        code_challenge_method: "S256",
      }),
    );
    expect(url.searchParams.get("scope")).toBe("openid");
    expect(url.searchParams.has("email")).toBe(false);
    expect(url.searchParams.has("profile")).toBe(false);
    expect(url.searchParams.get("response_type")).toBe("code");
    expect(url.searchParams.get("redirect_uri")).toBe(
      "https://clashlens.example/auth/google/callback",
    );
    expect(url.searchParams.get("state")).toBe(oauthTransaction.state);
    expect(url.searchParams.get("nonce")).toBe(oauthTransaction.nonce);
    expect(url.searchParams.get("code_challenge")).toBe(oauthTransaction.codeChallenge);
    expect(url.searchParams.get("code_challenge_method")).toBe("S256");
  });

  it("forces Google to reauthenticate before unlinking", async () => {
    const { deps } = fakeDeps();
    const service = await createGoogleOidcService(testConfig(), deps);
    const oauthTransaction = createOAuthTransaction(
      "/account/providers",
      1_000_000,
      fixedRandom,
      "unlink",
      "google",
      "A".repeat(43),
    );

    const url = service.authorizationUrl(oauthTransaction);
    expect(url.searchParams.get("prompt")).toBe("login");
    expect(url.searchParams.get("max_age")).toBe("0");
  });

  it("uses the exact configured callback path for the redirect URI", () => {
    expect(CALLBACK_PATH).toBe("/auth/google/callback");
    expect(redirectUriFor(new URL("https://clashlens.example"))).toBe(
      "https://clashlens.example/auth/google/callback",
    );
    expect(redirectUriFor(new URL("http://localhost:3000"))).toBe(
      "http://localhost:3000/auth/google/callback",
    );
  });

  it("returns only the immutable provider subject from a valid callback", async () => {
    const { deps, authorizationCodeGrant } = fakeDeps();
    const service = await createGoogleOidcService(testConfig(), deps);
    const oauthTransaction = transaction();
    const identity: ValidatedGoogleIdentity = await service.validateCallback(
      { state: oauthTransaction.state, code: "auth-code" },
      oauthTransaction,
      1_000_100,
    );

    expect(identity).toEqual({ provider: "google", providerSubject: SUBJECT });
    const callbackUrl = authorizationCodeGrant.mock.calls[0]?.[1] as URL;
    expect(callbackUrl.origin + callbackUrl.pathname).toBe(
      "https://clashlens.example/auth/google/callback",
    );
    expect(callbackUrl.searchParams.get("state")).toBe(oauthTransaction.state);
    expect(callbackUrl.searchParams.get("code")).toBe("auth-code");
    expect(authorizationCodeGrant.mock.calls[0]?.[2]).toEqual({
      expectedState: oauthTransaction.state,
      expectedNonce: oauthTransaction.nonce,
      pkceCodeVerifier: oauthTransaction.codeVerifier,
    });
  });

  it("accepts a multi-valued audience that contains the client ID", async () => {
    const { deps } = fakeDeps(() =>
      validClaims(undefined, { aud: ["other", CLIENT_ID] }),
    );
    const service = await createGoogleOidcService(testConfig(), deps);
    const oauthTransaction = transaction();
    await expect(
      service.validateCallback(
        { state: oauthTransaction.state, code: "code" },
        oauthTransaction,
        1_000_100,
      ),
    ).resolves.toEqual({ provider: "google", providerSubject: SUBJECT });
  });

  it("rejects an expired transaction before any grant", async () => {
    const { deps, authorizationCodeGrant } = fakeDeps();
    const service = await createGoogleOidcService(testConfig(), deps);
    const oauthTransaction = transaction();
    await expectCallbackError(
      service,
      { state: oauthTransaction.state, code: "code" },
      oauthTransaction,
      "expired",
      1_000_600,
    );
    expect(authorizationCodeGrant).not.toHaveBeenCalled();
  });

  it("treats the default clock as epoch seconds, not milliseconds", async () => {
    vi.useFakeTimers();
    try {
      vi.setSystemTime(new Date(1_000_100_000));
      const { deps } = fakeDeps();
      const service = await createGoogleOidcService(testConfig(), deps);
      const oauthTransaction = transaction(1_000_000);
      await expect(
        service.validateCallback(
          { state: oauthTransaction.state, code: "code" },
          oauthTransaction,
        ),
      ).resolves.toEqual({ provider: "google", providerSubject: SUBJECT });
      vi.setSystemTime(new Date(1_000_600_000));
      await expect(
        service.validateCallback(
          { state: oauthTransaction.state, code: "code" },
          oauthTransaction,
        ),
      ).rejects.toMatchObject({ code: "expired" });
    } finally {
      vi.useRealTimers();
    }
  });

  it.each(["", "wrong-state"])(
    "rejects a missing or mismatched state as invalid_state (%j)",
    async (state) => {
      const { deps, authorizationCodeGrant } = fakeDeps();
      const service = await createGoogleOidcService(testConfig(), deps);
      const oauthTransaction = transaction();
      await expectCallbackError(
        service,
        { state, code: "code" },
        oauthTransaction,
        "invalid_state",
        1_000_100,
      );
      expect(authorizationCodeGrant).not.toHaveBeenCalled();
    },
  );

  it("rejects a provider error response after the state matches", async () => {
    const { deps, authorizationCodeGrant } = fakeDeps();
    const service = await createGoogleOidcService(testConfig(), deps);
    const oauthTransaction = transaction();
    await expectCallbackError(
      service,
      { state: oauthTransaction.state, error: "access_denied" },
      oauthTransaction,
      "provider_error",
      1_000_100,
    );
    expect(authorizationCodeGrant).not.toHaveBeenCalled();
  });

  it("rejects callbacks with too many parameters", async () => {
    const { deps } = fakeDeps();
    const service = await createGoogleOidcService(testConfig(), deps);
    const oauthTransaction = transaction();
    const params: Record<string, string> = {
      state: oauthTransaction.state,
      code: "code",
    };
    for (let index = 0; index < 40; index += 1) {
      params[`extra-${index}`] = "x";
    }
    await expectCallbackError(
      service,
      params,
      oauthTransaction,
      "invalid_callback",
      1_000_100,
    );
  });

  it("rejects callbacks whose total parameter size exceeds the bound", async () => {
    const { deps } = fakeDeps();
    const service = await createGoogleOidcService(testConfig(), deps);
    const oauthTransaction = transaction();
    await expectCallbackError(
      service,
      { state: oauthTransaction.state, code: "code", padding: "x".repeat(9000) },
      oauthTransaction,
      "invalid_callback",
      1_000_100,
    );
  });

  it("rejects an implausible transaction as invalid_callback", async () => {
    const { deps } = fakeDeps();
    const service = await createGoogleOidcService(testConfig(), deps);
    const oauthTransaction = { ...transaction(), codeVerifier: "short" };
    await expectCallbackError(
      service,
      { state: oauthTransaction.state, code: "code" },
      oauthTransaction,
      "invalid_callback",
      1_000_100,
    );
  });

  it("maps a failed or missing-code grant to invalid_callback", async () => {
    const { deps } = fakeDeps();
    const authorizationCodeGrant = vi.fn(async () => {
      throw new Error("missing code");
    });
    const service = await createGoogleOidcService(testConfig(), {
      ...deps,
      authorizationCodeGrant,
    });
    const oauthTransaction = transaction();
    await expectCallbackError(
      service,
      { state: oauthTransaction.state },
      oauthTransaction,
      "invalid_callback",
      1_000_100,
    );
  });

  it("maps non-record or throwing claims to invalid_claims", async () => {
    for (const claims of [
      () => null,
      () => "not-an-object",
      () => {
        throw new Error("claims unavailable");
      },
    ]) {
      const { deps } = fakeDeps(claims as () => unknown);
      const service = await createGoogleOidcService(testConfig(), deps);
      const oauthTransaction = transaction();
      await expectCallbackError(
        service,
        { state: oauthTransaction.state, code: "code" },
        oauthTransaction,
        "invalid_claims",
        1_000_100,
      );
    }
  });

  it.each([
    ["wrong issuer", { iss: "https://evil.example" }],
    ["wrong audience", { aud: "other-client" }],
    ["audience array without the client ID", { aud: ["a", "b"] }],
    ["empty audience array", { aud: [] }],
    ["expired token", { exp: 1_000_000 }],
    ["non-numeric expiry", { exp: "soon" }],
    ["wrong nonce", { nonce: "wrong-nonce" }],
    ["missing nonce", { nonce: undefined }],
    ["empty subject", { sub: "" }],
    ["whitespace subject", { sub: "a b" }],
    ["control-character subject", { sub: "a\u0000b" }],
    ["oversized subject", { sub: "x".repeat(MAX_PROVIDER_SUBJECT_LENGTH + 1) }],
  ])("rejects %s as invalid_claims", async (_label, overrides) => {
    const { deps } = fakeDeps(() => validClaims(1_000_000, overrides));
    const service = await createGoogleOidcService(testConfig(), deps);
    const oauthTransaction = transaction();
    await expectCallbackError(
      service,
      { state: oauthTransaction.state, code: "code" },
      oauthTransaction,
      "invalid_claims",
      1_000_100,
    );
  });

  it("never exposes tokens, claims, or provider values in errors", async () => {
    const { deps } = fakeDeps(() => ({
      iss: ISSUER,
      aud: CLIENT_ID,
      exp: 1_000_100,
      nonce: "wrong-nonce",
      sub: SUBJECT,
      access_token: "secret-access-token",
      refresh_token: "secret-refresh-token",
      email: "user@example.com",
    }));
    const service = await createGoogleOidcService(testConfig(), deps);
    const oauthTransaction = transaction();
    try {
      await service.validateCallback(
        { state: oauthTransaction.state, code: "code" },
        oauthTransaction,
        1_000_100,
      );
      throw new Error("expected validation to fail");
    } catch (error) {
      expectOAuthError(error, "invalid_claims");
      const serialized = JSON.stringify(error);
      expect(serialized).not.toContain("secret-access-token");
      expect(serialized).not.toContain("secret-refresh-token");
      expect(serialized).not.toContain("user@example.com");
      expect(serialized).not.toContain("wrong-nonce");
    }
  });

  it("requires an enabled login configuration with a client ID", async () => {
    await expect(
      createGoogleOidcService(testConfig(), fakeDeps().deps),
    ).resolves.toBeDefined();
    const disabled = loadWebsiteConfig({
      NODE_ENV: "test",
      CLASHLENS_PUBLIC_ORIGIN: "https://clashlens.example",
    });
    await expect(createGoogleOidcService(disabled, {})).rejects.toThrow(
      "Google OIDC service requires an enabled login configuration",
    );
    expect(GOOGLE_ISSUER).toBe("https://accounts.google.com");
  });
});
