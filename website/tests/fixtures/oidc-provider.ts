#!/usr/bin/env node
/**
 * Deterministic local OpenID Connect provider for browser tests.
 *
 * This server is test-only. It serves the OIDC endpoints that the website
 * discovers through openid-client: discovery, JWKS, authorization, and token.
 * It generates an ephemeral RS256 key at startup and never writes a secret to
 * disk. The issuer, client ID, client secret, redirect URI, and default
 * subject are fixed test values; every value can be overridden with
 * CLASHLENS_FIXTURE_OIDC_* environment variables.
 *
 * Security rules, in order:
 * - The authorization endpoint validates client ID, exact redirect URI,
 *   response_type=code, exact scope "openid", state, nonce, and PKCE S256.
 * - The token endpoint validates client credentials, grant type, exact
 *   redirect URI, one-time short-lived code, and the PKCE verifier.
 * - The ID token carries only iss, aud, exp, iat, nonce, and the stable test
 *   subject. It never carries email, profile, or refresh-token claims.
 * - Codes are single-use, expire quickly, and are stored in a bounded map.
 * - Every response has Cache-Control: no-store.
 * - The reset endpoint works only for loopback clients.
 * - Request sizes are bounded. Codes, verifiers, tokens, and secrets are
 *   never logged.
 */

import { createHash, randomBytes, timingSafeEqual } from "node:crypto";
import { createServer } from "node:http";
import type { IncomingMessage, ServerResponse } from "node:http";
import { exportJWK, generateKeyPair, SignJWT } from "jose";
import type { CryptoKey } from "jose";

const HOST = "127.0.0.1";
const DEFAULT_PORT = 8011;
const KEY_ID = "fixture-rs256-1";
const AUTH_CODE_TTL_SECONDS = 60;
const ID_TOKEN_TTL_SECONDS = 60;
const ACCESS_TOKEN_TTL_SECONDS = 300;
const MAX_CODE_ENTRIES = 5_000;
const MAX_QUERY_BYTES = 8_192;
const MAX_BODY_BYTES = 65_536;
const MAX_HEADER_BYTES = 16_384;

const CLIENT_ID = envOr("CLASHLENS_FIXTURE_OIDC_CLIENT_ID", "clashlens-browser-test");
const CLIENT_SECRET = envOr(
  "CLASHLENS_FIXTURE_OIDC_CLIENT_SECRET",
  "clashlens-browser-test-secret",
);
const DEFAULT_SUBJECT = envOr(
  "CLASHLENS_FIXTURE_OIDC_SUBJECT",
  "fixture-google-subject-1001",
);
const REDIRECT_URI = envOr(
  "CLASHLENS_FIXTURE_OIDC_REDIRECT_URI",
  "http://127.0.0.1:5173/auth/google/callback",
);
const PORT = Number(process.env.CLASHLENS_FIXTURE_OIDC_PORT ?? DEFAULT_PORT);

/** Exact issuer string; the website compares it with trailing slash. */
const ISSUER = `http://${HOST}:${PORT}/`;

const CLIENT_ID_PATTERN = /^[A-Za-z0-9._~-]{1,256}$/;
const STATE_PATTERN = /^[A-Za-z0-9_-]{16,128}$/;
const NONCE_PATTERN = /^[A-Za-z0-9_-]{16,128}$/;
const CODE_CHALLENGE_PATTERN = /^[A-Za-z0-9_-]{43,128}$/;
const SUBJECT_PATTERN = /^fixture-google-subject-[0-9]{4}$/;

interface AuthorizationCode {
  clientId: string;
  redirectUri: string;
  codeChallenge: string;
  nonce: string;
  subject: string;
  state: string;
  createdAtMs: number;
}

const codes = new Map<string, AuthorizationCode>();

function envOr(name: string, fallback: string): string {
  const value = process.env[name];
  return value === undefined || value === "" ? fallback : value;
}

function safeEqual(left: string, right: string): boolean {
  const leftBytes = Buffer.from(left, "utf8");
  const rightBytes = Buffer.from(right, "utf8");
  if (leftBytes.length !== rightBytes.length) return false;
  return timingSafeEqual(leftBytes, rightBytes);
}

function isLoopbackClient(address: string | undefined): boolean {
  return address === "127.0.0.1" || address === "::1" || address === "::ffff:127.0.0.1";
}

function pruneCodes(nowMs: number): void {
  for (const [code, entry] of codes) {
    if (nowMs - entry.createdAtMs > AUTH_CODE_TTL_SECONDS * 1000) {
      codes.delete(code);
    }
  }
  while (codes.size > MAX_CODE_ENTRIES) {
    const oldest = [...codes.entries()].sort(
      (left, right) => left[1].createdAtMs - right[1].createdAtMs,
    )[0];
    if (oldest === undefined) break;
    codes.delete(oldest[0]);
  }
}

function jsonResponse(response: ServerResponse, status: number, payload: unknown): void {
  const body = JSON.stringify(payload);
  response.writeHead(status, {
    "Content-Type": "application/json",
    "Content-Length": String(Buffer.byteLength(body)),
    "Cache-Control": "no-store",
  });
  response.end(body);
}

function errorPage(response: ServerResponse, message: string): void {
  jsonResponse(response, 400, { error: "invalid_request", error_description: message });
}

function authorizationRedirect(
  response: ServerResponse,
  location: URL,
  parameters: Record<string, string>,
): void {
  for (const [key, value] of Object.entries(parameters)) {
    location.searchParams.set(key, value);
  }
  response.writeHead(302, {
    Location: location.toString(),
    "Cache-Control": "no-store",
  });
  response.end();
}

function readBoundedBody(request: IncomingMessage): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    let total = 0;
    request.on("data", (chunk: Buffer) => {
      total += chunk.length;
      if (total > MAX_BODY_BYTES) {
        reject(new Error("body too large"));
        request.destroy();
        return;
      }
      chunks.push(chunk);
    });
    request.on("end", () => resolve(Buffer.concat(chunks)));
    request.on("error", reject);
  });
}

function discoveryDocument(): Record<string, unknown> {
  return {
    issuer: ISSUER,
    authorization_endpoint: `${ISSUER}authorize`,
    token_endpoint: `${ISSUER}token`,
    jwks_uri: `${ISSUER}jwks`,
    response_types_supported: ["code"],
    subject_types_supported: ["public"],
    id_token_signing_alg_values_supported: ["RS256"],
    code_challenge_methods_supported: ["S256"],
    token_endpoint_auth_methods_supported: ["client_secret_basic", "client_secret_post"],
    scopes_supported: ["openid"],
    claims_supported: ["iss", "aud", "exp", "iat", "nonce", "sub"],
  };
}

function handleAuthorize(response: ServerResponse, rawUrl: string): void {
  if (Buffer.byteLength(rawUrl, "utf8") > MAX_QUERY_BYTES) {
    errorPage(response, "request too large");
    return;
  }
  const url = new URL(rawUrl, `http://${HOST}:${PORT}`);
  const parameters = url.searchParams;

  const clientId = parameters.get("client_id") ?? "";
  if (clientId !== CLIENT_ID) {
    errorPage(response, "unknown client");
    return;
  }
  const redirectUri = parameters.get("redirect_uri") ?? "";
  if (redirectUri !== REDIRECT_URI) {
    errorPage(response, "redirect URI is not registered");
    return;
  }
  const callback = new URL(REDIRECT_URI);
  const state = parameters.get("state") ?? "";
  if (STATE_PATTERN.test(state)) callback.searchParams.set("state", state);

  const responseType = parameters.get("response_type") ?? "";
  if (responseType !== "code") {
    authorizationRedirect(response, callback, {
      error: "unsupported_response_type",
    });
    return;
  }
  const scope = parameters.get("scope") ?? "";
  if (scope !== "openid") {
    authorizationRedirect(response, callback, { error: "invalid_scope" });
    return;
  }
  if (!STATE_PATTERN.test(state)) {
    authorizationRedirect(response, callback, { error: "invalid_request" });
    return;
  }
  const nonce = parameters.get("nonce") ?? "";
  if (!NONCE_PATTERN.test(nonce)) {
    authorizationRedirect(response, callback, { error: "invalid_request" });
    return;
  }
  const codeChallenge = parameters.get("code_challenge") ?? "";
  const codeChallengeMethod = parameters.get("code_challenge_method") ?? "";
  if (!CODE_CHALLENGE_PATTERN.test(codeChallenge) || codeChallengeMethod !== "S256") {
    authorizationRedirect(response, callback, { error: "invalid_request" });
    return;
  }
  pruneCodes(Date.now());
  const code = randomBytes(32).toString("base64url");
  codes.set(code, {
    clientId,
    redirectUri,
    codeChallenge,
    nonce,
    subject: DEFAULT_SUBJECT,
    state,
    createdAtMs: Date.now(),
  });
  authorizationRedirect(response, callback, { code });
}

function clientCredentials(request: IncomingMessage, body: URLSearchParams) {
  const authorization = request.headers.authorization ?? "";
  if (authorization.startsWith("Basic ")) {
    const decoded = Buffer.from(authorization.slice(6), "base64url").toString("utf8");
    const separator = decoded.indexOf(":");
    if (separator < 0) return null;
    return {
      clientId: decoded.slice(0, separator),
      clientSecret: decoded.slice(separator + 1),
    };
  }
  const clientId = body.get("client_id") ?? "";
  const clientSecret = body.get("client_secret") ?? "";
  if (clientId === "" || clientSecret === "") return null;
  return { clientId, clientSecret };
}

function handleToken(
  request: IncomingMessage,
  response: ServerResponse,
  body: Buffer,
): void {
  if (body.length > MAX_BODY_BYTES) {
    jsonResponse(response, 400, { error: "invalid_request" });
    return;
  }
  let form: URLSearchParams;
  try {
    form = new URLSearchParams(body.toString("utf8"));
  } catch {
    jsonResponse(response, 400, { error: "invalid_request" });
    return;
  }
  const credentials = clientCredentials(request, form);
  if (
    credentials === null ||
    credentials.clientId !== CLIENT_ID ||
    !safeEqual(credentials.clientSecret, CLIENT_SECRET)
  ) {
    response.writeHead(401, {
      "WWW-Authenticate": 'Basic realm="token"',
      "Cache-Control": "no-store",
    });
    response.end();
    return;
  }
  if (form.get("grant_type") !== "authorization_code") {
    jsonResponse(response, 400, { error: "unsupported_grant_type" });
    return;
  }
  const code = form.get("code") ?? "";
  const entry = codes.get(code);
  if (entry === undefined) {
    jsonResponse(response, 400, { error: "invalid_grant" });
    return;
  }
  codes.delete(code);
  if (Date.now() - entry.createdAtMs > AUTH_CODE_TTL_SECONDS * 1000) {
    jsonResponse(response, 400, { error: "invalid_grant" });
    return;
  }
  if ((form.get("redirect_uri") ?? "") !== entry.redirectUri) {
    jsonResponse(response, 400, { error: "invalid_grant" });
    return;
  }
  const verifier = form.get("code_verifier") ?? "";
  const challenge = createHash("sha256")
    .update(verifier, "utf8")
    .digest()
    .toString("base64url");
  if (!safeEqual(challenge, entry.codeChallenge)) {
    jsonResponse(response, 400, { error: "invalid_grant" });
    return;
  }
  void signIdToken(entry).then(
    (idToken) => {
      jsonResponse(response, 200, {
        access_token: randomBytes(32).toString("base64url"),
        token_type: "Bearer",
        expires_in: ACCESS_TOKEN_TTL_SECONDS,
        id_token: idToken,
      });
    },
    () => jsonResponse(response, 500, { error: "server_error" }),
  );
}

async function signIdToken(entry: AuthorizationCode): Promise<string> {
  const now = Math.floor(Date.now() / 1000);
  return new SignJWT({ nonce: entry.nonce })
    .setProtectedHeader({ alg: "RS256", kid: KEY_ID })
    .setIssuer(ISSUER)
    .setAudience(entry.clientId)
    .setSubject(entry.subject)
    .setIssuedAt(now)
    .setExpirationTime(now + ID_TOKEN_TTL_SECONDS)
    .sign(PRIVATE_KEY);
}

let PRIVATE_KEY: CryptoKey;
let PUBLIC_JWK: Record<string, unknown>;

async function main(): Promise<void> {
  if (!Number.isInteger(PORT) || PORT < 1 || PORT > 65_535) {
    throw new Error("OIDC fixture port must be an integer between 1 and 65535");
  }
  if (!CLIENT_ID_PATTERN.test(CLIENT_ID)) {
    throw new Error("OIDC fixture client ID is invalid");
  }
  if (CLIENT_SECRET.length === 0 || CLIENT_SECRET.length > 512) {
    throw new Error("OIDC fixture client secret is invalid");
  }
  if (!SUBJECT_PATTERN.test(DEFAULT_SUBJECT)) {
    throw new Error("OIDC fixture subject is invalid");
  }
  const { publicKey, privateKey } = await generateKeyPair("RS256");
  PRIVATE_KEY = privateKey;
  PUBLIC_JWK = { ...(await exportJWK(publicKey)), kid: KEY_ID, use: "sig", alg: "RS256" };

  const server = createServer(
    { maxHeaderSize: MAX_HEADER_BYTES },
    (request, response) => {
      const rawUrl = request.url ?? "/";
      const pathname = new URL(rawUrl, `http://${HOST}:${PORT}`).pathname;
      if (request.method === "GET" && pathname === "/healthz") {
        jsonResponse(response, 200, { ok: true, fixture: "oidc-provider-v1" });
        return;
      }
      if (request.method === "GET" && pathname === "/.well-known/openid-configuration") {
        jsonResponse(response, 200, discoveryDocument());
        return;
      }
      if (request.method === "GET" && pathname === "/jwks") {
        jsonResponse(response, 200, { keys: [PUBLIC_JWK] });
        return;
      }
      if (request.method === "GET" && pathname === "/authorize") {
        handleAuthorize(response, rawUrl);
        return;
      }
      if (request.method === "POST" && pathname === "/token") {
        void readBoundedBody(request).then(
          (body) => handleToken(request, response, body),
          () => jsonResponse(response, 400, { error: "invalid_request" }),
        );
        return;
      }
      if (request.method === "POST" && pathname === "/reset") {
        if (!isLoopbackClient(request.socket.remoteAddress)) {
          jsonResponse(response, 403, { error: "forbidden" });
          return;
        }
        codes.clear();
        jsonResponse(response, 200, { ok: true });
        return;
      }
      jsonResponse(response, 404, { error: "not_found" });
    },
  );

  server.maxHeadersCount = 64;
  server.headersTimeout = 5_000;
  server.requestTimeout = 5_000;
  server.listen(PORT, HOST, () => {
    console.log(`OIDC fixture listening on ${ISSUER}`);
  });
}

void main();
