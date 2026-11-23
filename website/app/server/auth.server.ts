import { createHash, randomBytes, timingSafeEqual } from "node:crypto";

export interface LoginIdentity {
  provider: "google" | "discord";
  providerSubject: string;
}

export const OAUTH_TRANSACTION_LIFETIME_SECONDS = 600;

export const MAX_PROVIDER_SUBJECT_LENGTH = 128;
export const MAX_CALLBACK_PARAMS = 32;
export const MAX_CALLBACK_PARAM_BYTES = 8192;

/** Sign in, sign up (a missing account may be made), or change sign-in connections. */
export type OAuthIntent = "login" | "signup" | "link" | "unlink";

export interface OAuthTransaction {
  provider: "google" | "discord";
  state: string;
  nonce: string;
  codeVerifier: string;
  codeChallenge: string;
  returnPath: string;
  intent: OAuthIntent;
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

export function constantTimeEqual(left: string, right: string): boolean {
  const leftBytes = Buffer.from(left, "utf8");
  const rightBytes = Buffer.from(right, "utf8");
  if (leftBytes.length !== rightBytes.length) return false;
  return timingSafeEqual(leftBytes, rightBytes);
}

export function isProvider(value: unknown): value is "google" | "discord" {
  return value === "google" || value === "discord";
}

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

export const MAX_RETURN_PATH_LENGTH = 2048;
// %23 is the # in a player tag, such as /players/%232PP.
const RETURN_PATH_CHARS = /^(?:[A-Za-z0-9._~/-]|%23)+$/;
const RETURN_QUERY_CHARS =
  /^(?:[A-Za-z0-9._~/?=&+,:@!$()*;[\]{}|^`\\-]|%[0-9A-Fa-f]{2})+$/;

export function isSafeReturnPath(value: string): boolean {
  const queryStart = value.indexOf("?");
  const path = queryStart === -1 ? value : value.slice(0, queryStart);
  const query = queryStart === -1 ? null : value.slice(queryStart + 1);
  return (
    value.length <= MAX_RETURN_PATH_LENGTH &&
    path.startsWith("/") &&
    !path.startsWith("//") &&
    RETURN_PATH_CHARS.test(path) &&
    !path.split("/").some((segment) => segment === "." || segment === "..") &&
    (query === null || RETURN_QUERY_CHARS.test(query))
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
