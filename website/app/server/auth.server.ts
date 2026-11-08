import { createHash, randomBytes, timingSafeEqual } from "node:crypto";

export interface LoginIdentity {
  provider: "google" | "discord";
  providerSubject: string;
}

export const OAUTH_TRANSACTION_LIFETIME_SECONDS = 600;

export const MAX_PROVIDER_SUBJECT_LENGTH = 128;
export const MAX_CALLBACK_PARAMS = 32;
export const MAX_CALLBACK_PARAM_BYTES = 8192;

/**
 * What the authorization completes: a browser login, or a link/unlink
 * started from an authenticated account. The provider is always the one
 * whose authorize route created the transaction.
 */
/** Sign in, sign up (a missing account may be made), or change sign-in connections. */
export type OAuthIntent = "login" | "signup" | "link" | "unlink";

export interface OAuthTransaction {
  /** The only provider whose callback may consume this transaction. */
  provider: "google" | "discord";
  state: string;
  nonce: string;
  codeVerifier: string;
  codeChallenge: string;
  returnPath: string;
  intent: OAuthIntent;
  /** Fingerprint of the initiating login (createLoginSessionBinding), for link/unlink only. */
  sessionBinding?: string;
  issuedAt: number;
  expiresAt: number;
}

export type OAuthCallbackErrorCode =
  "expired" | "provider_error" | "invalid_state" | "invalid_callback" | "invalid_claims";

export class OAuthCallbackError extends Error {
  readonly code: OAuthCallbackErrorCode;

  constructor(code: OAuthCallbackErrorCode) {
    super("Google sign-in could not be completed");
    this.name = "OAuthCallbackError";
    this.code = code;
  }
}

/** Google subject bound: non-empty, bounded, no whitespace or control chars. */
export function isProviderSubject(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= MAX_PROVIDER_SUBJECT_LENGTH &&
    ![...value].some((character) => {
      const code = character.charCodeAt(0);
      return /\s/u.test(character) || code <= 0x1f || code === 0x7f;
    })
  );
}

/** Constant-time string comparison for state and nonce values. */
export function constantTimeEqual(left: string, right: string): boolean {
  const leftBytes = Buffer.from(left, "utf8");
  const rightBytes = Buffer.from(right, "utf8");
  if (leftBytes.length !== rightBytes.length) return false;
  return timingSafeEqual(leftBytes, rightBytes);
}

/** The two Phase 1 login providers; transactions are provider-bound. */
export function isProvider(value: unknown): value is "google" | "discord" {
  return value === "google" || value === "discord";
}

/**
 * Fresh one-time OAuth transaction for exactly one provider and intent:
 * state, nonce, and PKCE S256 verifier with challenge, plus the validated
 * same-origin return path. The transaction lives for
 * OAUTH_TRANSACTION_LIFETIME_SECONDS and is consumed once.
 * `now` is the current epoch time in seconds.
 */
export function createOAuthTransaction(
  returnPath: string,
  now: number,
  random: (size: number) => Buffer = randomBytes,
  intent: OAuthIntent = "login",
  provider: "google" | "discord" = "google",
  sessionBinding?: string,
): OAuthTransaction {
  const issuedAt = Math.floor(now);
  const state = random(24).toString("base64url");
  const nonce = random(24).toString("base64url");
  const codeVerifier = random(32).toString("base64url");
  const codeChallenge = createHash("sha256")
    .update(codeVerifier, "utf8")
    .digest()
    .toString("base64url");
  return {
    provider,
    state,
    nonce,
    codeVerifier,
    codeChallenge,
    returnPath,
    intent,
    ...(sessionBinding === undefined ? {} : { sessionBinding }),
    issuedAt,
    expiresAt: issuedAt + OAUTH_TRANSACTION_LIFETIME_SECONDS,
  };
}

export function isPlausibleTransaction(transaction: OAuthTransaction): boolean {
  return (
    /^[A-Za-z0-9_-]{16,128}$/.test(transaction.state) &&
    /^[A-Za-z0-9_-]{16,128}$/.test(transaction.nonce) &&
    /^[A-Za-z0-9_-]{43,128}$/.test(transaction.codeVerifier) &&
    /^[A-Za-z0-9_-]{43,128}$/.test(transaction.codeChallenge) &&
    (transaction.intent === "login" ||
      transaction.intent === "signup" ||
      transaction.intent === "link" ||
      transaction.intent === "unlink") &&
    (transaction.intent === "login" || transaction.intent === "signup"
      ? transaction.sessionBinding === undefined
      : typeof transaction.sessionBinding === "string" &&
        /^[A-Za-z0-9_-]{43}$/.test(transaction.sessionBinding)) &&
    isProvider(transaction.provider) &&
    Number.isSafeInteger(transaction.issuedAt) &&
    Number.isSafeInteger(transaction.expiresAt) &&
    transaction.expiresAt - transaction.issuedAt === OAUTH_TRANSACTION_LIFETIME_SECONDS
  );
}

export const MAX_RETURN_PATH_LENGTH = 200;
// %23 is the # in a player tag, such as /players/%232PP.
const RETURN_PATH_CHARS = /^(?:[A-Za-z0-9._~/-]|%23)+$/;

export function isSafeReturnPath(value: string): boolean {
  return (
    value.length > 0 &&
    value.length <= MAX_RETURN_PATH_LENGTH &&
    value.startsWith("/") &&
    !value.startsWith("//") &&
    RETURN_PATH_CHARS.test(value) &&
    !value.split("/").some((segment) => segment === "." || segment === "..")
  );
}

export function isBoundedCallbackParams(params: Record<string, string>): boolean {
  if (Object.keys(params).length > MAX_CALLBACK_PARAMS) return false;
  let total = 0;
  for (const [key, value] of Object.entries(params)) {
    total += key.length + value.length;
    if (total > MAX_CALLBACK_PARAM_BYTES) return false;
  }
  return true;
}
