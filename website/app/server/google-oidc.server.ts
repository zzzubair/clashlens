import {
  constantTimeEqual,
  isBoundedCallbackParams,
  isPlausibleTransaction,
  isProviderSubject,
  OAuthCallbackError,
  type LoginIdentity,
  type OAuthTransaction,
} from "./auth.server";
import { GOOGLE_ISSUER } from "./config.server";
export const CALLBACK_PATH = "/auth/google/callback";
/**
 * Server-only Google OpenID Connect service (Authorization Code + PKCE S256).
 *
 * The service owns the exact public origin and redirect URI, fresh high-
 * entropy state/nonce/PKCE values per attempt, one-time transaction expiry,
 * and strict claim validation (issuer, audience, expiry, nonce, subject).
 * Only the immutable provider subject survives: access, refresh, and ID
 * tokens are never returned, stored, or logged. Errors are safe, typed
 * OAuthCallbackError values that never carry provider responses, tokens,
 * stack traces, or secret values.
 *
 * The openid-client dependency is injectable so deterministic tests can
 * substitute a fake standards client; production uses openid-client 6.8.4
 * wired to the configured issuer (Google by default, or the test-only
 * override outside production).
 */

import type { Configuration } from "openid-client";

import {
  allowInsecureRequests,
  authorizationCodeGrant as openidAuthorizationCodeGrant,
  buildAuthorizationUrl as openidBuildAuthorizationUrl,
  discovery as openidDiscovery,
} from "openid-client";

import type { WebsiteConfig } from "./config.server";

export interface OidcDiscovery {
  (
    issuerUrl: URL,
    clientId: string,
    metadata: { client_secret: string },
  ): Promise<unknown>;
}

export interface OidcAuthorizationUrlBuilder {
  (config: unknown, parameters: Record<string, string>): URL;
}

export interface OidcGrantChecks {
  expectedState: string;
  expectedNonce: string;
  pkceCodeVerifier: string;
}

export interface OidcTokenResult {
  claims(): unknown;
}

export interface OidcGrant {
  (config: unknown, currentUrl: URL, checks: OidcGrantChecks): Promise<OidcTokenResult>;
}

export interface OidcServiceDeps {
  discovery?: OidcDiscovery;
  buildAuthorizationUrl?: OidcAuthorizationUrlBuilder;
  authorizationCodeGrant?: OidcGrant;
}

export interface OidcService {
  authorizationUrl(transaction: OAuthTransaction): URL;
  validateCallback(
    callbackParams: Record<string, string>,
    transaction: OAuthTransaction,
    now?: number,
  ): Promise<LoginIdentity & { provider: "google" }>;
}

export function redirectUriFor(publicOrigin: URL): string {
  return new URL(CALLBACK_PATH, publicOrigin).toString();
}

const defaultDiscovery: OidcDiscovery = (issuerUrl, clientId, metadata) =>
  openidDiscovery(
    issuerUrl,
    clientId,
    metadata.client_secret,
    undefined,
    issuerUrl.protocol === "http:" ? { execute: [allowInsecureRequests] } : undefined,
  );

const defaultBuildAuthorizationUrl: OidcAuthorizationUrlBuilder = (config, parameters) =>
  openidBuildAuthorizationUrl(config as Configuration, parameters);

const defaultGrant: OidcGrant = (config, currentUrl, checks) =>
  openidAuthorizationCodeGrant(config as Configuration, currentUrl, checks);

export async function createGoogleOidcService(
  config: WebsiteConfig,
  deps: OidcServiceDeps = {},
): Promise<OidcService> {
  if (!config.loginEnabled || config.googleClientId === "") {
    throw new Error("Google OIDC service requires an enabled login configuration");
  }
  const discovery = deps.discovery ?? defaultDiscovery;
  const buildAuthorizationUrl =
    deps.buildAuthorizationUrl ?? defaultBuildAuthorizationUrl;
  const grant = deps.authorizationCodeGrant ?? defaultGrant;
  const issuer =
    config.googleIssuerUrl.origin === GOOGLE_ISSUER &&
    config.googleIssuerUrl.pathname === "/"
      ? GOOGLE_ISSUER
      : config.googleIssuerUrl.toString();
  const clientId = config.googleClientId;
  const redirectUri = redirectUriFor(config.publicOrigin);
  const discovered = await discovery(config.googleIssuerUrl, clientId, {
    client_secret: config.googleClientSecret,
  });

  return {
    authorizationUrl(transaction) {
      return buildAuthorizationUrl(discovered, {
        scope: "openid",
        response_type: "code",
        redirect_uri: redirectUri,
        state: transaction.state,
        nonce: transaction.nonce,
        code_challenge: transaction.codeChallenge,
        code_challenge_method: "S256",
        ...(transaction.intent === "unlink" ? { prompt: "login", max_age: "0" } : {}),
      });
    },

    async validateCallback(
      callbackParams,
      transaction,
      now = Math.floor(Date.now() / 1000),
    ) {
      const nowSeconds = now;
      if (nowSeconds >= transaction.expiresAt) {
        throw new OAuthCallbackError("expired");
      }
      if (
        !isBoundedCallbackParams(callbackParams) ||
        !isPlausibleTransaction(transaction)
      ) {
        throw new OAuthCallbackError("invalid_callback");
      }
      const state = callbackParams["state"];
      if (state === undefined || !constantTimeEqual(state, transaction.state)) {
        throw new OAuthCallbackError("invalid_state");
      }
      if (callbackParams["error"] !== undefined) {
        throw new OAuthCallbackError("provider_error");
      }
      const callbackUrl = new URL(redirectUri);
      for (const [key, value] of Object.entries(callbackParams)) {
        callbackUrl.searchParams.append(key, value);
      }
      let result: OidcTokenResult;
      try {
        result = await grant(discovered, callbackUrl, {
          expectedState: transaction.state,
          expectedNonce: transaction.nonce,
          pkceCodeVerifier: transaction.codeVerifier,
        });
      } catch {
        throw new OAuthCallbackError("invalid_callback");
      }
      let claims: Record<string, unknown>;
      try {
        const rawClaims = result.claims();
        if (!isRecord(rawClaims)) throw new Error("claims are not a record");
        claims = rawClaims;
      } catch {
        throw new OAuthCallbackError("invalid_claims");
      }
      const providerSubject = validateClaims(
        claims,
        issuer,
        clientId,
        transaction.nonce,
        nowSeconds,
      );
      return { provider: "google", providerSubject };
    },
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/**
 * Explicit claim validation. The standards client already validates the ID
 * token signature and claims during the grant; these checks are the
 * deterministic, testable contract for issuer, audience, expiry, nonce, and
 * subject regardless of the client in use.
 */
function validateClaims(
  claims: Record<string, unknown>,
  issuer: string,
  clientId: string,
  expectedNonce: string,
  nowSeconds: number,
): string {
  if (claims["iss"] !== issuer) {
    throw new OAuthCallbackError("invalid_claims");
  }
  const audience = claims["aud"];
  const audienceMatches =
    audience === clientId ||
    (Array.isArray(audience) &&
      audience.length > 0 &&
      audience.every((entry) => typeof entry === "string") &&
      audience.includes(clientId));
  if (!audienceMatches) {
    throw new OAuthCallbackError("invalid_claims");
  }

  const expiry = claims["exp"];
  if (typeof expiry !== "number" || !Number.isFinite(expiry) || expiry <= nowSeconds) {
    throw new OAuthCallbackError("invalid_claims");
  }
  const nonce = claims["nonce"];
  if (typeof nonce !== "string" || !constantTimeEqual(nonce, expectedNonce)) {
    throw new OAuthCallbackError("invalid_claims");
  }
  const subject = claims["sub"];
  if (!isProviderSubject(subject)) {
    throw new OAuthCallbackError("invalid_claims");
  }
  return subject;
}
