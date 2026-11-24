import { constantTimeEqual } from "./auth.server";

import { readBoundedBody } from "./actions.server";
import { isProviderSubject, OAuthCallbackError, type OAuthIntent } from "./auth.server";

import type { WebsiteConfig } from "./config.server";

export const DISCORD_CALLBACK_PATH = "/auth/discord/callback";

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
const DISCORD_ID_PATTERN = /^[0-9]{17,20}$/;

export interface DiscordServiceDeps {
  fetchJson?: (
    url: string,
    init: RequestInit,
  ) => Promise<{ status: number; json: unknown }>;
}

export interface DiscordOidcService {
  authorizationUrl(transaction: {
    state: string;
    codeChallenge: string;
    intent: OAuthIntent;
  }): URL;
  validateCallback(
    callbackParams: Record<string, string>,
    transaction: { state: string; codeVerifier: string },
  ): Promise<{ provider: "discord"; providerSubject: string }>;
}

type JsonFetcher = (
  url: string,
  init: RequestInit,
) => Promise<{ status: number; json: unknown }>;

const DISCORD_TIMEOUT_MS = 10_000;
const MAX_DISCORD_RESPONSE_BYTES = 64 * 1024;

const defaultFetchJson: JsonFetcher = async (url, init) => {
  const response = await fetch(url, {
    ...init,
    redirect: "error",
    signal: AbortSignal.timeout(DISCORD_TIMEOUT_MS),
  });
  const body = await readBoundedBody(response.body, MAX_DISCORD_RESPONSE_BYTES);
  if (body === null) await response.body?.cancel().catch(() => undefined);
  let json: unknown = null;
  try {
    if (body !== null) json = JSON.parse(new TextDecoder().decode(body));
  } catch {
    json = null;
  }
  return { status: response.status, json };
};

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
      if (transaction.intent === "unlink") {
        url.searchParams.set("scope", "identify");
      } else {
        url.searchParams.set("scope", "identify applications.commands");
        url.searchParams.set("integration_type", "1");
      }
      url.searchParams.set("state", transaction.state);
      url.searchParams.set("code_challenge", transaction.codeChallenge);
      url.searchParams.set("code_challenge_method", "S256");
      url.searchParams.set("prompt", "consent");
      return url;
    },

    async validateCallback(callbackParams, transaction) {
      const state = callbackParams["state"];
      if (state === undefined || !constantTimeEqual(state, transaction.state)) {
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
