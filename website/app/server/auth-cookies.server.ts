/**
 * Server-only signed browser cookies for Google login sessions and one-time
 * OAuth transactions.
 *
 * Values are canonical JSON bound by an HMAC-SHA256 signature using the
 * exactly-32-byte login secret, so any tamper is detected by a constant-time
 * comparison and the value is rejected as null. Login cookies carry only the
 * immutable provider and provider subject with a fixed non-sliding 24-hour
 * lifetime. OAuth transaction cookies carry only the state, nonce, PKCE
 * verifier and challenge, and same-origin return path with a ten-minute
 * lifetime. No Google tokens, email, or other personal data ever appears in a
 * cookie value, and every value stays far below the 4 KB cookie limit.
 *
 * The route layer composes Set-Cookie headers from these values; this module
 * stays pure (no request, response, or storage access) so tests are
 * deterministic and the callback route can consume the transaction cookie
 * later in the same login flow.
 */

import { createHmac, timingSafeEqual } from "node:crypto";

import {
  isPlausibleTransaction,
  isProviderSubject,
  MAX_PROVIDER_SUBJECT_LENGTH,
  OAUTH_TRANSACTION_LIFETIME_SECONDS,
} from "./google-oidc.server";
import type { OAuthTransaction } from "./google-oidc.server";

export const LOGIN_COOKIE_NAME = "clashlens_login";
export const OAUTH_COOKIE_NAME = "clashlens_oauth";
export const LOGIN_COOKIE_LIFETIME_SECONDS = 24 * 60 * 60;

const COOKIE_VERSION = 1;
/** Hard cap for any cookie value; real values are a few hundred bytes. */
const MAX_COOKIE_VALUE_BYTES = 4096;
const MAX_RETURN_PATH_LENGTH = 200;
const RETURN_PATH_CHARS = /^[A-Za-z0-9._~/-]+$/;

export interface LoginIdentity {
  provider: "google";
  providerSubject: string;
}

function isBoundedReturnPath(value: string): boolean {
  return (
    value.length > 0 &&
    value.length <= MAX_RETURN_PATH_LENGTH &&
    value.startsWith("/") &&
    !value.startsWith("//") &&
    RETURN_PATH_CHARS.test(value) &&
    !value.split("/").some((segment) => segment === "." || segment === "..")
  );
}

function assertKey(key: Buffer): void {
  if (key.length !== 32) {
    throw new Error("login cookie key must be exactly 32 bytes");
  }
}

function sign(payload: Buffer, key: Buffer): Buffer {
  return createHmac("sha256", key).update(payload).digest();
}

function encodeValue(payload: Buffer, key: Buffer): string {
  return `${payload.toString("base64url")}.${sign(payload, key).toString("base64url")}`;
}

/** Decode a strict single base64url part, or null for junk or oversized bytes. */
function decodePart(value: string, maxLength: number): Buffer | null {
  if (value.length === 0 || !/^[A-Za-z0-9_-]+$/.test(value)) return null;
  const decoded = Buffer.from(value, "base64url");
  if (decoded.length > maxLength) return null;
  if (decoded.toString("base64url") !== value) return null;
  return decoded;
}

function parseValue(
  value: string | undefined | null,
  key: Buffer,
  maxPayloadBytes: number,
): unknown | null {
  if (typeof value !== "string" || value.length === 0) return null;
  if (Buffer.byteLength(value, "utf8") > MAX_COOKIE_VALUE_BYTES) return null;
  const separator = value.lastIndexOf(".");
  if (separator <= 0 || separator === value.length - 1) return null;
  const payloadPart = value.slice(0, separator);
  const signaturePart = value.slice(separator + 1);
  if (payloadPart.length === 0 || signaturePart.length === 0) return null;
  const payload = decodePart(payloadPart, maxPayloadBytes);
  if (payload === null) return null;
  const providedSignature = decodePart(signaturePart, 32);
  if (providedSignature === null) return null;
  const expectedSignature = sign(payload, key);
  if (
    providedSignature.length !== expectedSignature.length ||
    !timingSafeEqual(providedSignature, expectedSignature)
  ) {
    return null;
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(payload.toString("utf8"));
  } catch {
    return null;
  }
  return parsed;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isSafeInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value);
}

function canonicalLoginPayload(
  identity: LoginIdentity,
  issuedAt: number,
  expiresAt: number,
): Buffer {
  const payload = Buffer.from(
    JSON.stringify({
      v: COOKIE_VERSION,
      p: identity.provider,
      s: identity.providerSubject,
      i: issuedAt,
      e: expiresAt,
    }),
    "utf8",
  );
  if (payload.length > MAX_COOKIE_VALUE_BYTES) {
    throw new Error("login cookie payload exceeds the value bound");
  }
  return payload;
}

/**
 * Build a signed login cookie value. `nowSeconds` is the current epoch time
 * in seconds; the cookie never slides and expires exactly 24 hours later.
 */
export function createLoginCookieValue(
  identity: LoginIdentity,
  key: Buffer,
  nowSeconds: number,
): string {
  assertKey(key);
  if (identity.provider !== "google" || !isProviderSubject(identity.providerSubject)) {
    throw new Error("login identity must be a bounded Google subject");
  }
  if (!Number.isSafeInteger(nowSeconds) || nowSeconds < 0) {
    throw new Error("login cookie timestamps must be safe integers");
  }
  const issuedAt = nowSeconds;
  return encodeValue(
    canonicalLoginPayload(identity, issuedAt, issuedAt + LOGIN_COOKIE_LIFETIME_SECONDS),
    key,
  );
}

/**
 * Parse and verify a login cookie value. Returns the identity or null for any
 * missing, malformed, tampered, or expired value. Never throws for input.
 */
export function parseLoginCookieValue(
  value: string | undefined | null,
  key: Buffer,
  nowSeconds: number,
): LoginIdentity | null {
  assertKey(key);
  if (!Number.isSafeInteger(nowSeconds)) return null;
  const parsed = parseValue(value, key, 256);
  if (!isRecord(parsed)) return null;
  if (
    parsed.v !== COOKIE_VERSION ||
    parsed.p !== "google" ||
    !isProviderSubject(parsed.s) ||
    !isSafeInteger(parsed.i) ||
    !isSafeInteger(parsed.e)
  ) {
    return null;
  }
  const issuedAt = parsed.i;
  const expiresAt = parsed.e;
  if (
    expiresAt - issuedAt !== LOGIN_COOKIE_LIFETIME_SECONDS ||
    issuedAt > nowSeconds ||
    expiresAt <= nowSeconds
  ) {
    return null;
  }
  return { provider: "google", providerSubject: parsed.s };
}

function canonicalOAuthPayload(transaction: OAuthTransaction): Buffer {
  const payload = Buffer.from(
    JSON.stringify({
      v: COOKIE_VERSION,
      st: transaction.state,
      no: transaction.nonce,
      ve: transaction.codeVerifier,
      ch: transaction.codeChallenge,
      rp: transaction.returnPath,
      i: transaction.issuedAt,
      e: transaction.expiresAt,
    }),
    "utf8",
  );
  if (payload.length > MAX_COOKIE_VALUE_BYTES) {
    throw new Error("OAuth transaction cookie payload exceeds the value bound");
  }
  return payload;
}

/** Build a signed OAuth transaction cookie value with its fixed ten-minute lifetime. */
export function createOAuthTransactionCookieValue(
  transaction: OAuthTransaction,
  key: Buffer,
): string {
  assertKey(key);
  if (
    !isPlausibleTransaction(transaction) ||
    !isBoundedReturnPath(transaction.returnPath)
  ) {
    throw new Error("OAuth transaction cookie requires a plausible bounded transaction");
  }
  return encodeValue(canonicalOAuthPayload(transaction), key);
}

/**
 * Parse and verify an OAuth transaction cookie value. Returns the
 * transaction or null for any missing, malformed, tampered, or expired
 * value. The callback route consumes (clears) the cookie after a successful
 * parse. Never throws for input.
 */
export function parseOAuthTransactionCookieValue(
  value: string | undefined | null,
  key: Buffer,
  nowSeconds: number,
): OAuthTransaction | null {
  assertKey(key);
  if (!Number.isSafeInteger(nowSeconds)) return null;
  const parsed = parseValue(value, key, 512);
  if (!isRecord(parsed)) return null;
  if (
    parsed.v !== COOKIE_VERSION ||
    !isSafeInteger(parsed.i) ||
    !isSafeInteger(parsed.e) ||
    typeof parsed.st !== "string" ||
    typeof parsed.no !== "string" ||
    typeof parsed.ve !== "string" ||
    typeof parsed.ch !== "string" ||
    typeof parsed.rp !== "string" ||
    !isBoundedReturnPath(parsed.rp)
  ) {
    return null;
  }
  const transaction: OAuthTransaction = {
    state: parsed.st,
    nonce: parsed.no,
    codeVerifier: parsed.ve,
    codeChallenge: parsed.ch,
    returnPath: parsed.rp,
    issuedAt: parsed.i,
    expiresAt: parsed.e,
  };
  if (!isPlausibleTransaction(transaction) || transaction.issuedAt > nowSeconds) {
    return null;
  }
  if (nowSeconds >= transaction.expiresAt) return null;
  return transaction;
}

/**
 * The fixed cookie attribute suffix shared by all login cookies:
 * Path=/, HttpOnly, SameSite=Lax, and Secure for any https origin.
 */
export function cookieAttributes(secure: boolean): string {
  return `Path=/; HttpOnly; SameSite=Lax${secure ? "; Secure" : ""}`;
}

/** Build a full Set-Cookie header value for a signed cookie value. */
export function buildSetCookieHeader(
  name: string,
  value: string,
  maxAgeSeconds: number,
  secure: boolean,
): string {
  return `${name}=${value}; Max-Age=${maxAgeSeconds}; ${cookieAttributes(secure)}`;
}

/** Build a Set-Cookie header value that clears a login cookie immediately. */
export function buildClearCookieHeader(name: string, secure: boolean): string {
  return `${name}=; Max-Age=0; ${cookieAttributes(secure)}`;
}

export { MAX_PROVIDER_SUBJECT_LENGTH, OAUTH_TRANSACTION_LIFETIME_SECONDS };
