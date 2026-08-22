#!/usr/bin/env node
/**
 * Deterministic local Discord OAuth2 fixture for browser tests.
 *
 * This server is test-only. It serves the OAuth2 Authorization Code + PKCE
 * endpoints and the `identify` user endpoint that the website's Discord
 * service uses: /oauth2/authorize, /api/oauth2/token, and /api/users/@me.
 * Access tokens are opaque, single-user, in-memory values; the immutable
 * numeric subject is the only identity ever returned. Codes are one-time and
 * expire quickly. No secret or token is ever logged.
 */

import { createHash, randomBytes, timingSafeEqual } from "node:crypto";
import { createServer } from "node:http";
import type { IncomingMessage, ServerResponse } from "node:http";

const HOST = "127.0.0.1";
const DEFAULT_PORT = 8012;
const AUTH_CODE_TTL_SECONDS = 60;
const MAX_CODE_ENTRIES = 5_000;
const MAX_QUERY_BYTES = 8_192;
const MAX_BODY_BYTES = 65_536;

const CLIENT_ID = envOr("CLASHLENS_FIXTURE_DISCORD_CLIENT_ID", "1234567890123456789");
const CLIENT_SECRET = envOr(
  "CLASHLENS_FIXTURE_DISCORD_CLIENT_SECRET",
  "discord-browser-test-secret",
);
const SUBJECT = envOr("CLASHLENS_FIXTURE_DISCORD_SUBJECT", "110022003300440055");
const REDIRECT_URI = envOr(
  "CLASHLENS_FIXTURE_DISCORD_REDIRECT_URI",
  "http://127.0.0.1:5173/auth/discord/callback",
);
const PORT = Number(process.env.CLASHLENS_FIXTURE_DISCORD_PORT ?? DEFAULT_PORT);

interface AuthorizationCode {
  codeChallenge: string;
  createdAtMs: number;
}

const codes = new Map<string, AuthorizationCode>();
const accessTokens = new Set<string>();

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

function jsonResponse(response: ServerResponse, status: number, payload: unknown): void {
  const body = JSON.stringify(payload);
  response.writeHead(status, {
    "Content-Type": "application/json",
    "Content-Length": String(Buffer.byteLength(body)),
    "Cache-Control": "no-store",
  });
  response.end(body);
}

function authorizationRedirect(
  response: ServerResponse,
  location: URL,
  parameters: Record<string, string>,
): void {
  for (const [key, value] of Object.entries(parameters)) {
    location.searchParams.set(key, value);
  }
  response.writeHead(302, { Location: location.toString(), "Cache-Control": "no-store" });
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

function prune(nowMs: number): void {
  for (const [code, entry] of codes) {
    if (nowMs - entry.createdAtMs > AUTH_CODE_TTL_SECONDS * 1000) codes.delete(code);
  }
  while (codes.size > MAX_CODE_ENTRIES) {
    const oldest = [...codes.entries()].sort(
      (left, right) => left[1].createdAtMs - right[1].createdAtMs,
    )[0];
    if (oldest === undefined) break;
    codes.delete(oldest[0]);
  }
}

function handleAuthorize(response: ServerResponse, rawUrl: string): void {
  if (Buffer.byteLength(rawUrl, "utf8") > MAX_QUERY_BYTES) {
    jsonResponse(response, 400, { error: "invalid_request" });
    return;
  }
  const url = new URL(rawUrl, `http://${HOST}:${PORT}`);
  const parameters = url.searchParams;
  if (parameters.get("client_id") !== CLIENT_ID) {
    jsonResponse(response, 400, { error: "unknown client" });
    return;
  }
  if (parameters.get("redirect_uri") !== REDIRECT_URI) {
    jsonResponse(response, 400, { error: "redirect URI is not registered" });
    return;
  }
  const callback = new URL(REDIRECT_URI);
  const state = parameters.get("state") ?? "";
  if (!/^[A-Za-z0-9_-]{16,128}$/.test(state)) {
    authorizationRedirect(response, callback, { error: "invalid_request" });
    return;
  }
  callback.searchParams.set("state", state);
  if (parameters.get("response_type") !== "code") {
    authorizationRedirect(response, callback, { error: "unsupported_response_type" });
    return;
  }
  if (parameters.get("scope") !== "identify") {
    authorizationRedirect(response, callback, { error: "invalid_scope" });
    return;
  }
  const challenge = parameters.get("code_challenge") ?? "";
  if (!/^[A-Za-z0-9_-]{43,128}$/.test(challenge)) {
    authorizationRedirect(response, callback, { error: "invalid_request" });
    return;
  }
  if (parameters.get("code_challenge_method") !== "S256") {
    authorizationRedirect(response, callback, { error: "invalid_request" });
    return;
  }
  prune(Date.now());
  const code = randomBytes(32).toString("base64url");
  codes.set(code, { codeChallenge: challenge, createdAtMs: Date.now() });
  authorizationRedirect(response, callback, { code });
}

async function handleToken(
  request: IncomingMessage,
  response: ServerResponse,
): Promise<void> {
  let body: Buffer;
  try {
    body = await readBoundedBody(request);
  } catch {
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
  if (
    form.get("client_id") !== CLIENT_ID ||
    !safeEqual(form.get("client_secret") ?? "", CLIENT_SECRET)
  ) {
    jsonResponse(response, 401, { error: "invalid_client" });
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
  if ((form.get("redirect_uri") ?? "") !== REDIRECT_URI) {
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
  // One short-lived opaque token per exchange, used once by /users/@me.
  const accessToken = randomBytes(32).toString("base64url");
  accessTokens.add(accessToken);
  jsonResponse(response, 200, {
    access_token: accessToken,
    token_type: "Bearer",
    expires_in: 300,
    scope: "identify",
  });
}

function handleUsersMe(request: IncomingMessage, response: ServerResponse): void {
  const authorization = request.headers.authorization ?? "";
  const token = authorization.startsWith("Bearer ") ? authorization.slice(7) : "";
  if (token === "" || !accessTokens.has(token)) {
    jsonResponse(response, 401, { message: "401: Unauthorized" });
    return;
  }
  accessTokens.delete(token);
  jsonResponse(response, 200, {
    id: SUBJECT,
    username: "fixture-clasher",
    discriminator: "0",
  });
}

function main(): void {
  if (!Number.isInteger(PORT) || PORT < 1 || PORT > 65_535) {
    throw new Error("Discord fixture port must be an integer between 1 and 65535");
  }
  if (!/^[0-9]{17,20}$/.test(CLIENT_ID)) {
    throw new Error("Discord fixture client ID must be a numeric snowflake");
  }
  const server = createServer((request, response) => {
    const rawUrl = request.url ?? "/";
    const pathname = new URL(rawUrl, `http://${HOST}:${PORT}`).pathname;
    if (request.method === "GET" && pathname === "/healthz") {
      jsonResponse(response, 200, { ok: true, fixture: "discord-provider-v1" });
      return;
    }
    if (request.method === "GET" && pathname === "/oauth2/authorize") {
      handleAuthorize(response, rawUrl);
      return;
    }
    if (request.method === "POST" && pathname === "/api/oauth2/token") {
      void handleToken(request, response);
      return;
    }
    if (request.method === "GET" && pathname === "/api/users/@me") {
      handleUsersMe(request, response);
      return;
    }
    if (request.method === "POST" && pathname === "/reset") {
      const address = request.socket.remoteAddress;
      if (
        address !== "127.0.0.1" &&
        address !== "::1" &&
        address !== "::ffff:127.0.0.1"
      ) {
        jsonResponse(response, 403, { error: "forbidden" });
        return;
      }
      codes.clear();
      accessTokens.clear();
      jsonResponse(response, 200, { ok: true });
      return;
    }
    jsonResponse(response, 404, { error: "not_found" });
  });
  server.listen(PORT, HOST, () => {
    console.log(`Discord fixture listening on http://${HOST}:${PORT}`);
  });
}

main();
