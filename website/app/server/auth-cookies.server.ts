import {
  isSafeReturnPath,
  MAX_RETURN_PATH_LENGTH,
  type LoginIdentity,
} from "./auth.server";

import { createHash, createHmac, randomBytes, timingSafeEqual } from "node:crypto";

import type { OAuthIntent, OAuthTransaction } from "./auth.server";
import { isPlausibleTransaction, isProvider, isProviderSubject } from "./auth.server";

export const LOGIN_COOKIE_NAME = "clashlens_login";
export const OAUTH_COOKIE_NAME = "clashlens_oauth";
export const LOGIN_COOKIE_LIFETIME_SECONDS = 30 * 24 * 60 * 60;
export const LOGIN_COOKIE_RENEW_AFTER_SECONDS = 60 * 60;
const LEGACY_LOGIN_COOKIE_LIFETIME_SECONDS = 24 * 60 * 60;
const SESSION_BINDING = /^[A-Za-z0-9_-]{43}$/;

const COOKIE_VERSION = 1;
const MAX_COOKIE_VALUE_BYTES = 4096;

export interface LoginSession extends LoginIdentity {
  issuedAtMs: number;
  signedInAtMs: number;
}

interface ContinuedLogin {
  sessionBinding: string;
  signedInAtMs: number;
}

function isBoundedIntent(value: unknown): value is OAuthIntent {
  return (
    value === "login" || value === "signup" || value === "link" || value === "unlink"
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
  issuedMillisecond: number,
  expiresAt: number,
  login: ContinuedLogin | undefined,
): Buffer {
  const payload = Buffer.from(
    JSON.stringify({
      v: COOKIE_VERSION,
      p: identity.provider,
      s: identity.providerSubject,
      i: issuedAt,
      m: issuedMillisecond,
      e: expiresAt,
      n: randomBytes(16).toString("base64url"),
      ...(login === undefined ? {} : { l: login.sessionBinding, o: login.signedInAtMs }),
    }),
    "utf8",
  );
  if (payload.length > MAX_COOKIE_VALUE_BYTES) {
    throw new Error("login cookie payload exceeds the value bound");
  }
  return payload;
}

export function createLoginCookieValue(
  identity: LoginIdentity,
  key: Buffer,
  nowMs: number,
  login?: ContinuedLogin,
): string {
  assertKey(key);
  if (!isProvider(identity.provider) || !isProviderSubject(identity.providerSubject)) {
    throw new Error("login identity must be a bounded provider subject");
  }
  if (!Number.isSafeInteger(nowMs) || nowMs < 0) {
    throw new Error("login cookie timestamps must be safe integers");
  }
  if (
    login !== undefined &&
    (!SESSION_BINDING.test(login.sessionBinding) ||
      !Number.isSafeInteger(login.signedInAtMs) ||
      login.signedInAtMs < 0 ||
      login.signedInAtMs > nowMs)
  ) {
    throw new Error(
      "a renewed login needs a SHA-256 fingerprint and a sign-in time before now",
    );
  }
  const issuedAt = Math.floor(nowMs / 1000);
  return encodeValue(
    canonicalLoginPayload(
      identity,
      issuedAt,
      nowMs % 1000,
      issuedAt + LOGIN_COOKIE_LIFETIME_SECONDS,
      login,
    ),
    key,
  );
}

export function parseLoginCookieValue(
  value: string | undefined | null,
  key: Buffer,
  nowSeconds: number,
): LoginSession | null {
  assertKey(key);
  if (!Number.isSafeInteger(nowSeconds)) return null;
  const parsed = parseValue(value, key, 320);
  if (!isRecord(parsed)) return null;
  if (
    parsed.v !== COOKIE_VERSION ||
    typeof parsed.p !== "string" ||
    !isProvider(parsed.p) ||
    !isProviderSubject(parsed.s) ||
    !isSafeInteger(parsed.i) ||
    !isSafeInteger(parsed.e) ||
    (parsed.l !== undefined &&
      (typeof parsed.l !== "string" || !SESSION_BINDING.test(parsed.l)))
  ) {
    return null;
  }
  const issuedMillisecond = parsed.m === undefined ? 0 : parsed.m;
  if (
    !isSafeInteger(issuedMillisecond) ||
    issuedMillisecond < 0 ||
    issuedMillisecond > 999
  ) {
    return null;
  }
  const issuedAt = parsed.i;
  const expiresAt = parsed.e;
  const issuedAtMs = issuedAt * 1000 + issuedMillisecond;
  const signedInAtMs = parsed.o === undefined ? issuedAtMs : parsed.o;
  if (
    (expiresAt - issuedAt !== LOGIN_COOKIE_LIFETIME_SECONDS &&
      expiresAt - issuedAt !== LEGACY_LOGIN_COOKIE_LIFETIME_SECONDS) ||
    issuedAt > nowSeconds ||
    expiresAt <= nowSeconds ||
    !isSafeInteger(signedInAtMs) ||
    signedInAtMs < 0 ||
    signedInAtMs > issuedAtMs
  ) {
    return null;
  }
  return {
    provider: parsed.p as LoginIdentity["provider"],
    providerSubject: parsed.s,
    issuedAtMs,
    signedInAtMs,
  };
}

function canonicalOAuthPayload(transaction: OAuthTransaction): Buffer {
  const payload = Buffer.from(
    JSON.stringify({
      v: COOKIE_VERSION,
      pr: transaction.provider,
      st: transaction.state,
      no: transaction.nonce,
      ve: transaction.codeVerifier,
      ch: transaction.codeChallenge,
      rp: transaction.returnPath,
      in: transaction.intent,
      ...(transaction.sessionBinding === undefined
        ? {}
        : { sb: transaction.sessionBinding }),
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

export function createOAuthTransactionCookieValue(
  transaction: OAuthTransaction,
  key: Buffer,
): string {
  assertKey(key);
  if (!isPlausibleTransaction(transaction) || !isSafeReturnPath(transaction.returnPath)) {
    throw new Error("OAuth transaction cookie requires a plausible bounded transaction");
  }
  return encodeValue(canonicalOAuthPayload(transaction), key);
}

export function parseOAuthTransactionCookieValue(
  value: string | undefined | null,
  key: Buffer,
  nowSeconds: number,
): OAuthTransaction | null {
  assertKey(key);
  if (!Number.isSafeInteger(nowSeconds)) return null;
  const parsed = parseValue(value, key, MAX_RETURN_PATH_LENGTH + 512);
  if (!isRecord(parsed)) return null;
  if (
    parsed.v !== COOKIE_VERSION ||
    !isSafeInteger(parsed.i) ||
    !isSafeInteger(parsed.e) ||
    typeof parsed.st !== "string" ||
    !isProvider(parsed.pr) ||
    typeof parsed.no !== "string" ||
    typeof parsed.ve !== "string" ||
    typeof parsed.ch !== "string" ||
    typeof parsed.rp !== "string" ||
    !isBoundedIntent(parsed.in) ||
    !isSafeReturnPath(parsed.rp)
  ) {
    return null;
  }
  const transaction: OAuthTransaction = {
    provider: parsed.pr,
    state: parsed.st,
    nonce: parsed.no,
    codeVerifier: parsed.ve,
    codeChallenge: parsed.ch,
    returnPath: parsed.rp,
    intent: parsed.in,
    ...(parsed.sb === undefined ? {} : { sessionBinding: parsed.sb as string }),
    issuedAt: parsed.i,
    expiresAt: parsed.e,
  };
  if (
    (parsed.sb !== undefined && typeof parsed.sb !== "string") ||
    !isPlausibleTransaction(transaction) ||
    transaction.issuedAt > nowSeconds
  ) {
    return null;
  }
  if (nowSeconds >= transaction.expiresAt) return null;
  return transaction;
}

export function createLoginSessionBinding(loginCookieValue: string): string {
  const payload = decodePart(loginCookieValue.split(".", 1)[0] ?? "", 320);
  try {
    const parsed: unknown =
      payload === null ? null : JSON.parse(payload.toString("utf8"));
    if (
      isRecord(parsed) &&
      typeof parsed.l === "string" &&
      SESSION_BINDING.test(parsed.l)
    ) {
      return parsed.l;
    }
  } catch {
    // Not a renewed cookie; fall through to its own fingerprint.
  }
  return createHash("sha256").update(loginCookieValue, "utf8").digest("base64url");
}

export function cookieAttributes(secure: boolean): string {
  return `Path=/; HttpOnly; SameSite=Lax${secure ? "; Secure" : ""}`;
}

export function buildSetCookieHeader(
  name: string,
  value: string,
  maxAgeSeconds: number,
  secure: boolean,
): string {
  return `${name}=${value}; Max-Age=${maxAgeSeconds}; ${cookieAttributes(secure)}`;
}

export function buildClearCookieHeader(name: string, secure: boolean): string {
  return `${name}=; Max-Age=0; ${cookieAttributes(secure)}`;
}
