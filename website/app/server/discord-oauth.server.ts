/**
 * Server-only Discord OAuth2 service (Authorization Code + PKCE S256).
 *
 * Discord login uses the `identify` scope only. The access token is used once
 * in memory to read `/users/@me` and is then dropped: it is never persisted,
 * logged, or placed in a cookie. Only the immutable numeric Discord user ID
 * survives validation. Errors are safe, typed OAuthCallbackError values that
 * never carry provider responses, tokens, or secret values.
 *
 * The fetch boundary is injectable so deterministic tests can substitute a
 * fake Discord server; production uses the global fetch against the fixed
 * Discord origins.
 */

import { timingSafeEqual } from "node:crypto";

import type { WebsiteConfig } from "./config.server";
import { OAuthCallbackError } from "./google-oidc.server";
import { isProviderSubject } from "./google-oidc.server";

export const DISCORD_CALLBACK_PATH = "/auth/discord/callback";

/** Fixed Discord path layout under the configured API base origin. */
export function discordEndpoints(apiBase: URL): {
  authorization: string;
  token: string;
  usersMe: string;
} {
  return {
    authorization: new URL("/oauth2/authorize", apiBase).toString(),
    token: new URL("/api/oauth2/token", apiBase).toString(),
    usersMe: new URL("/api/users/@me", apiBase).toString(),
  };
}
/** Discord snowflake IDs are 17–20 digit numeric strings. */
const DISCORD_ID_PATTERN = /^[0-9]{17,20}$/;

export interface DiscordServiceDeps {
  fetchJson?: (
    url: string,
    init: RequestInit,
  ) => Promise<{ status: number; json: unknown }>;
}

export interface DiscordOidcService {
  authorizationUrl(transaction: { state: string; codeChallenge: string }): URL;
  validateCallback(
    callbackParams: Record<string, string>,
    transaction: { state: string; codeVerifier: string },
  ): Promise<{ provider: "discord"; providerSubject: string }>;
}

type JsonFetcher = (
  url: string,
  init: RequestInit,
) => Promise<{ status: number; json: unknown }>;

const defaultFetchJson: JsonFetcher = async (url, init) => {
  const response = await fetch(url, init);
  const json: unknown = await response.json().catch(() => null);
  return { status: response.status, json };
};

/** Constant-time string comparison shared with the Google flow. */
function safeEqual(left: string, right: string): boolean {
  const leftBytes = Buffer.from(left, "utf8");
  const rightBytes = Buffer.from(right, "utf8");
  if (leftBytes.length !== rightBytes.length) return false;
  return timingSafeEqual(leftBytes, rightBytes);
}

export function redirectUriFor(publicOrigin: URL): string {
  return new URL(DISCORD_CALLBACK_PATH, publicOrigin).toString();
}

export async function createDiscordOAuthService(
  config: WebsiteConfig,
  deps: DiscordServiceDeps = {},
): Promise<DiscordOidcService> {
  if (!config.loginEnabled || config.discordClientId === "") {
    throw new Error("Discord OAuth service requires an enabled login configuration");
  }
  const fetchJson = deps.fetchJson ?? defaultFetchJson;
  const clientId = config.discordClientId;
  const redirectUri = redirectUriFor(config.publicOrigin);
  const endpoints = discordEndpoints(config.discordApiBaseUrl);

  return {
    authorizationUrl(transaction) {
      const url = new URL(endpoints.authorization);
      url.searchParams.set("client_id", clientId);
      url.searchParams.set("redirect_uri", redirectUri);
      url.searchParams.set("response_type", "code");
      url.searchParams.set("scope", "identify");
      url.searchParams.set("state", transaction.state);
      url.searchParams.set("code_challenge", transaction.codeChallenge);
      url.searchParams.set("code_challenge_method", "S256");
      // Discord shows the authorization prompt on every attempt, which keeps
      // each link or unlink a fresh explicit authorization.
      url.searchParams.set("prompt", "consent");
      return url;
    },

    async validateCallback(callbackParams, transaction) {
      const state = callbackParams["state"];
      if (state === undefined || !safeEqual(state, transaction.state)) {
        throw new OAuthCallbackError("invalid_state");
      }
      if (callbackParams["error"] !== undefined) {
        throw new OAuthCallbackError("provider_error");
      }
      const code = callbackParams["code"];
      if (code === undefined || code.length === 0 || code.length > 512) {
        throw new OAuthCallbackError("invalid_callback");
      }

      let tokenResponse: { status: number; json: unknown };
      try {
        tokenResponse = await fetchJson(endpoints.token, {
          method: "POST",
          headers: { "content-type": "application/x-www-form-urlencoded" },
          body: new URLSearchParams({
            client_id: clientId,
            client_secret: config.discordClientSecret,
            grant_type: "authorization_code",
            code,
            redirect_uri: redirectUri,
            code_verifier: transaction.codeVerifier,
          }).toString(),
        });
      } catch {
        throw new OAuthCallbackError("invalid_callback");
      }
      if (tokenResponse.status !== 200 || !isRecord(tokenResponse.json)) {
        throw new OAuthCallbackError("invalid_callback");
      }
      const accessToken = tokenResponse.json["access_token"];
      if (
        typeof accessToken !== "string" ||
        accessToken.length === 0 ||
        accessToken.length > 512
      ) {
        throw new OAuthCallbackError("invalid_callback");
      }

      let userResponse: { status: number; json: unknown };
      try {
        userResponse = await fetchJson(endpoints.usersMe, {
          headers: { authorization: `Bearer ${accessToken}` },
        });
      } catch {
        throw new OAuthCallbackError("invalid_callback");
      }
      // The one-time access token never leaves this scope again.
      if (userResponse.status !== 200 || !isRecord(userResponse.json)) {
        throw new OAuthCallbackError("invalid_claims");
      }
      const id = userResponse.json["id"];
      if (typeof id !== "string" || !DISCORD_ID_PATTERN.test(id)) {
        throw new OAuthCallbackError("invalid_claims");
      }
      if (!isProviderSubject(id)) {
        throw new OAuthCallbackError("invalid_claims");
      }
      return { provider: "discord", providerSubject: id };
    },
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
