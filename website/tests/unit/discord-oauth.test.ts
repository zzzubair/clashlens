import { beforeEach, describe, expect, it } from "vitest";

import { createDiscordOAuthService } from "../../app/server/discord-oauth.server";
import { loadWebsiteConfig } from "../../app/server/config.server";
import { OAuthCallbackError } from "../../app/server/google-oidc.server";

const TEST_SECRET = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8";

function config() {
  return loadWebsiteConfig({
    NODE_ENV: "test",
    CLASHLENS_LOGIN_ENABLED: "true",
    CLASHLENS_PUBLIC_ORIGIN: "https://clashlens.example",
    CLASHLENS_GOOGLE_CLIENT_ID: "test-client.apps.googleusercontent.com",
    CLASHLENS_GOOGLE_CLIENT_SECRET: "google-test-secret",
    CLASHLENS_DISCORD_CLIENT_ID: "1234567890123456789",
    CLASHLENS_DISCORD_CLIENT_SECRET: "discord-test-secret",
    CLASHLENS_DISCORD_API_BASE_URL: "https://discord.example",
    CLASHLENS_LOGIN_SECRET_B64: TEST_SECRET,
  });
}

function transaction(now = 1_000_000) {
  return {
    state: "A".repeat(24),
    nonce: "B".repeat(24),
    codeVerifier: "C".repeat(48),
    codeChallenge: "D".repeat(43),
    returnPath: "/account",
    intent: "login" as const,
    issuedAt: now,
    expiresAt: now + 600,
  };
}

describe("Discord OAuth service", () => {
  let fetchCalls: Array<{ url: string; init: RequestInit }>;

  beforeEach(() => {
    fetchCalls = [];
  });

  function fakeFetch(respond: (url: string) => { status: number; json: unknown }) {
    return async (url: string, init: RequestInit) => {
      fetchCalls.push({ url, init });
      return respond(url);
    };
  }

  function validParams(): Record<string, string> {
    return { code: "one-time-code", state: transaction().state };
  }

  it("builds the exact authorization URL with identify-only scope and PKCE S256", async () => {
    const service = await createDiscordOAuthService(config());
    const url = service.authorizationUrl(transaction());
    expect(url.origin).toBe("https://discord.example");
    expect(url.pathname).toBe("/oauth2/authorize");
    expect(url.searchParams.get("client_id")).toBe("1234567890123456789");
    expect(url.searchParams.get("redirect_uri")).toBe(
      "https://clashlens.example/auth/discord/callback",
    );
    expect(url.searchParams.get("response_type")).toBe("code");
    expect(url.searchParams.get("scope")).toBe("identify");
    expect(url.searchParams.get("code_challenge_method")).toBe("S256");
    expect(url.searchParams.get("state")).toBe(transaction().state);
  });

  it("exchanges the code and returns only the immutable numeric user ID", async () => {
    const service = await createDiscordOAuthService(config(), {
      fetchJson: fakeFetch((url) =>
        url.endsWith("/api/users/@me")
          ? { status: 200, json: { id: "110022003300440055", username: "someone" } }
          : { status: 200, json: { access_token: "opaque-token", expires_in: 300 } },
      ),
    });
    const identity = await service.validateCallback(validParams(), transaction());
    expect(identity).toEqual({
      provider: "discord",
      providerSubject: "110022003300440055",
    });

    expect(fetchCalls).toHaveLength(2);
    const [tokenCall, userCall] = fetchCalls;
    expect(tokenCall.url).toBe("https://discord.example/api/oauth2/token");
    expect(String(tokenCall.init.body)).toContain("grant_type=authorization_code");
    expect(String(tokenCall.init.body)).toContain("client_secret=discord-test-secret");
    expect(userCall.url).toBe("https://discord.example/api/users/@me");
    expect((userCall.init.headers as Record<string, string>).authorization).toBe(
      "Bearer opaque-token",
    );
  });

  it("rejects a mismatched state before any provider call", async () => {
    const service = await createDiscordOAuthService(config(), {
      fetchJson: fakeFetch(() => ({ status: 500, json: null })),
    });
    await expect(
      service.validateCallback({ code: "c", state: "Z".repeat(24) }, transaction()),
    ).rejects.toMatchObject({ code: "invalid_state" });
    expect(fetchCalls).toHaveLength(0);
  });

  it("maps a provider error response to provider_error", async () => {
    const service = await createDiscordOAuthService(config(), {
      fetchJson: fakeFetch(() => ({ status: 500, json: null })),
    });
    await expect(
      service.validateCallback(
        { error: "access_denied", state: transaction().state },
        transaction(),
      ),
    ).rejects.toMatchObject({ code: "provider_error" });
    expect(fetchCalls).toHaveLength(0);
  });

  it("rejects a failed token exchange without exposing the response", async () => {
    const service = await createDiscordOAuthService(config(), {
      fetchJson: fakeFetch(() => ({ status: 400, json: { error: "invalid_grant" } })),
    });
    await expect(
      service.validateCallback(validParams(), transaction()),
    ).rejects.toMatchObject({ code: "invalid_callback" });
    expect(fetchCalls).toHaveLength(1);
  });

  it("rejects non-numeric or wrong-length Discord IDs as invalid claims", async () => {
    for (const id of ["not-numeric", "12345", `${"9".repeat(21)}`]) {
      const service = await createDiscordOAuthService(config(), {
        fetchJson: fakeFetch((url) =>
          url.endsWith("/api/users/@me")
            ? { status: 200, json: { id } }
            : { status: 200, json: { access_token: "t" } },
        ),
      });
      await expect(
        service.validateCallback(validParams(), transaction()),
      ).rejects.toMatchObject({ code: "invalid_claims" });
    }
  });

  it("refuses construction when login is disabled or Discord is unconfigured", async () => {
    const disabled = loadWebsiteConfig({
      NODE_ENV: "test",
      CLASHLENS_LOGIN_ENABLED: "false",
      CLASHLENS_PUBLIC_ORIGIN: "http://127.0.0.1:3000",
    });
    await expect(createDiscordOAuthService(disabled)).rejects.toThrow(/enabled login/);
  });

  it("keeps every error an OAuthCallbackError without provider details", async () => {
    const service = await createDiscordOAuthService(config(), {
      fetchJson: async () => {
        throw new Error("secret network detail");
      },
    });
    try {
      await service.validateCallback(validParams(), transaction());
      expect.unreachable("validateCallback should have thrown");
    } catch (error) {
      expect(error).toBeInstanceOf(OAuthCallbackError);
      expect((error as Error).message).not.toContain("secret network detail");
    }
  });
});
