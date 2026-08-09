/**
 * Deterministic test values for the local browser-test fixtures.
 *
 * These values are test-only. They are shared by the Playwright web-server
 * configuration, the local OIDC provider defaults, and the fixture-infrastructure
 * smoke test. No real credential ever matches these values.
 */

export const fixtureApiUrl = "http://127.0.0.1:8010";
export const oidcIssuerUrl = "http://127.0.0.1:8011";
export const websiteOrigin = "http://127.0.0.1:5173";

/** Golden-vector HMAC key: bytes 0..31, unpadded base64url. */
export const fixtureKey = Buffer.from(
  Array.from({ length: 32 }, (_, index) => index),
).toString("base64url");

/** Login cookie HMAC key: bytes 64..95, unpadded base64url. */
export const loginSecretB64 = Buffer.from(
  Array.from({ length: 32 }, (_, index) => index + 64),
).toString("base64url");

export const fixtureHmacCaller = "typescript-website";
export const fixtureHmacKeyId = "2026-08-a";

export const oidcClientId = "clashlens-browser-test";
export const oidcClientSecret = "clashlens-browser-test-secret";
export const oidcRedirectUri = `${websiteOrigin}/auth/google/callback`;
export const oidcSubject = "fixture-google-subject-1001";

export const fixtureApiHealthUrl = `${fixtureApiUrl}/healthz`;
export const oidcProviderHealthUrl = `${oidcIssuerUrl}/healthz`;
export const websiteHealthUrl = `${websiteOrigin}/healthz`;
