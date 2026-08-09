/**
 * Server-only HTTP helpers for account and login mutations: bounded cookie
 * and form parsing, same-origin request checks, fresh canonical idempotency
 * keys, and bounded OAuth callback query parsing.
 *
 * The helpers are deliberately small and pure so route actions stay
 * deterministic and unit-testable. The Python account domain remains
 * authoritative for name, tag, and limit rules; this module never re-implements
 * those rules and never echoes secret or personal values.
 */

import { randomUUID } from "node:crypto";

import {
  RESERVED_USERNAMES,
  isInappropriateName,
  normalizeDisplayName,
  normalizeUsername,
} from "../lib/account-validation";
import type { WebsiteErrorResponse } from "../lib/contracts";
import { LOGIN_COOKIE_NAME, parseLoginCookieValue } from "./auth-cookies.server";
import type { LoginIdentity } from "./auth-cookies.server";
import type { WebsiteConfig } from "./config.server";
import { safeWebsiteError } from "./errors.server";

const MAX_COOKIE_HEADER_BYTES = 8192;
const MAX_COOKIE_PAIRS = 64;
const MAX_COOKIE_NAME_BYTES = 128;
const MAX_COOKIE_VALUE_BYTES = 4096;

export const MAX_CALLBACK_PARAMS = 32;
export const MAX_CALLBACK_PARAM_BYTES = 8192;

export interface FormLimits {
  maxFields: number;
  maxFieldNameBytes: number;
  maxFieldValueBytes: number;
  maxTotalBytes: number;
}

export const DEFAULT_FORM_LIMITS: FormLimits = {
  maxFields: 16,
  maxFieldNameBytes: 64,
  maxFieldValueBytes: 16 * 1024,
  maxTotalBytes: 64 * 1024,
};

const FORM_CONTENT_TYPE = "application/x-www-form-urlencoded";

/**
 * Parse a Cookie header into a name/value map. The header, pair count, and
 * every name and value are bounded; the first occurrence of a repeated name
 * wins. Malformed or oversized input yields an empty map, never an error.
 */
export function parseCookieHeader(
  header: string | null | undefined,
): Map<string, string> {
  const cookies = new Map<string, string>();
  if (typeof header !== "string" || header.length === 0) return cookies;
  if (Buffer.byteLength(header, "utf8") > MAX_COOKIE_HEADER_BYTES) return cookies;
  const parts = header.split(";");
  if (parts.length > MAX_COOKIE_PAIRS) return cookies;
  for (const part of parts) {
    const trimmed = part.trim();
    if (trimmed === "") continue;
    const separator = trimmed.indexOf("=");
    if (separator <= 0) continue;
    const name = trimmed.slice(0, separator).trim();
    const value = trimmed.slice(separator + 1).trim();
    if (name.length === 0 || name.length > MAX_COOKIE_NAME_BYTES) continue;
    if (Buffer.byteLength(value, "utf8") > MAX_COOKIE_VALUE_BYTES) continue;
    if (!cookies.has(name)) cookies.set(name, value);
  }
  return cookies;
}

/**
 * Read and verify the signed login cookie for a request. Returns the Google
 * identity or null when login is disabled, the cookie is missing, or the
 * value is malformed, tampered, or expired. Never throws for request input.
 */
export function readLoginIdentity(
  request: Request,
  config: WebsiteConfig,
): LoginIdentity | null {
  if (!config.loginEnabled || config.loginSecret.length !== 32) return null;
  const cookies = parseCookieHeader(request.headers.get("cookie"));
  const value = cookies.get(LOGIN_COOKIE_NAME);
  if (value === undefined) return null;
  return parseLoginCookieValue(value, config.loginSecret, Math.floor(Date.now() / 1000));
}

/**
 * Require a same-origin request context for cookie-authenticated mutations.
 * The Origin header must match the exact public origin; when the browser
 * sends no Origin, the Referer origin must match. Requests with neither are
 * rejected. The configured public origin is the only accepted base, so a
 * forwarded or hostile header can never widen the accepted set.
 */
export function isSameOrigin(request: Request, publicOrigin: URL): boolean {
  const expected = publicOrigin.origin;
  const origin = request.headers.get("Origin");
  if (origin !== null) return origin === expected;
  const referer = request.headers.get("Referer");
  if (referer === null) return false;
  try {
    return new URL(referer).origin === expected;
  } catch {
    return false;
  }
}

/**
 * Read a urlencoded form body with hard bounds on field count, per-field
 * size, and total bytes. Returns a string map with no duplicate names, or
 * null for any missing, oversized, malformed, or duplicate input. File
 * uploads are not supported by any account form.
 */
export async function parseBoundedFormData(
  request: Request,
  limits: FormLimits = DEFAULT_FORM_LIMITS,
): Promise<Record<string, string> | null> {
  const contentType = request.headers
    .get("Content-Type")
    ?.split(";", 1)[0]
    ?.trim()
    .toLowerCase();
  if (contentType !== FORM_CONTENT_TYPE) return null;

  const declaredLength = request.headers.get("Content-Length");
  if (
    declaredLength !== null &&
    (!/^\d+$/.test(declaredLength) || Number(declaredLength) > limits.maxTotalBytes)
  ) {
    return null;
  }

  const reader = request.body?.getReader();
  if (!reader) return null;
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const result = await reader.read();
      if (result.done) break;
      total += result.value.byteLength;
      if (total > limits.maxTotalBytes) return null;
      chunks.push(result.value);
    }
  } finally {
    reader.releaseLock();
  }

  const body = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    body.set(chunk, offset);
    offset += chunk.byteLength;
  }
  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(body);
  } catch {
    return null;
  }
  if (/%(?![0-9a-fA-F]{2})/.test(text)) return null;

  const form = new URLSearchParams(text);
  if (form.size > limits.maxFields) return null;
  const values: Record<string, string> = {};
  let totalBytes = 0;
  for (const [name, value] of form.entries()) {
    if (name.length === 0 || name.length > limits.maxFieldNameBytes) return null;
    if (Buffer.byteLength(value, "utf8") > limits.maxFieldValueBytes) return null;
    if (Object.prototype.hasOwnProperty.call(values, name)) return null;
    values[name] = value;
    totalBytes += Buffer.byteLength(name, "utf8") + Buffer.byteLength(value, "utf8");
    if (totalBytes > limits.maxTotalBytes) return null;
  }
  return values;
}

/** Fresh canonical idempotency UUID for one form render or action target. */
export function freshIdempotencyKey(): string {
  return randomUUID();
}

/** Strict canonical lowercase UUID check for idempotency keys. */
export function isIdempotencyKey(value: unknown): value is string {
  return (
    typeof value === "string" &&
    /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value)
  );
}

/**
 * Parse an OAuth callback query string into a plain string map. The result
 * is bounded by entry count and total size, and any repeated parameter name
 * is rejected outright so a provider response can never be ambiguous.
 */
export function parseCallbackParams(search: string): Record<string, string> | null {
  if (search === "") return {};
  const query = search.startsWith("?") ? search.slice(1) : search;
  let params: URLSearchParams;
  try {
    params = new URLSearchParams(query);
  } catch {
    return null;
  }
  if (params.size > MAX_CALLBACK_PARAMS) return null;
  const result: Record<string, string> = {};
  let total = 0;
  for (const [key, value] of params.entries()) {
    if (key.length === 0 || key.length > MAX_CALLBACK_PARAM_BYTES) return null;
    if (Object.prototype.hasOwnProperty.call(result, key)) return null;
    result[key] = value;
    total += key.length + value.length;
    if (total > MAX_CALLBACK_PARAM_BYTES) return null;
  }
  return result;
}

export interface AccountNameFieldErrors {
  username?: string;
  displayName?: string;
}

export interface AccountNameValidation {
  username: string | null;
  displayName: string | null;
  fieldErrors: AccountNameFieldErrors;
}

/**
 * Early account-name validation matching the Python account domain. The
 * Python private API remains authoritative; these checks only give safe
 * field-level feedback before any account mutation. Rejected values are
 * never logged and map to generic field messages.
 */
export function validateAccountNames(values: {
  username: string;
  displayName: string;
}): AccountNameValidation {
  const fieldErrors: AccountNameFieldErrors = {};
  const trimmedUsername = values.username.trim().toLowerCase();
  if (RESERVED_USERNAMES.has(trimmedUsername)) {
    fieldErrors.username = "That username is reserved. Choose a different one.";
  }
  const username = normalizeUsername(values.username);
  if (username === null && fieldErrors.username === undefined) {
    fieldErrors.username =
      "Username must start with a letter and use 3–32 lowercase letters, numbers, or underscores.";
  } else if (username !== null && isInappropriateName(values.username)) {
    fieldErrors.username = "Choose a different username.";
  }
  const displayName = normalizeDisplayName(values.displayName);
  if (displayName === null) {
    fieldErrors.displayName =
      "Display name must be 1–80 characters and must not contain control characters.";
  } else if (isInappropriateName(values.displayName)) {
    fieldErrors.displayName = "Choose a different display name.";
  }
  return { username, displayName, fieldErrors };
}

export type AccountNameErrorOutcome =
  | { kind: "account_exists" }
  | { kind: "account_not_found" }
  | { kind: "field"; fieldErrors: AccountNameFieldErrors; status: number }
  | { kind: "general"; generalError: WebsiteErrorResponse };

/**
 * Map a private Python account error to a safe website outcome for create and
 * update name mutations. Only the documented Python codes are special-cased;
 * every other failure becomes a general safe website error.
 */
export function mapAccountNameError(error: unknown): AccountNameErrorOutcome {
  if (isPythonApiErrorLike(error)) {
    const payload = isRecord(error.payload) ? error.payload : {};
    if (error.status === 409 && payload.error === "account_exists") {
      return { kind: "account_exists" };
    }
    if (isAccountNotFoundError(error)) {
      return { kind: "account_not_found" };
    }
    if (error.status === 409 && payload.error === "username_unavailable") {
      return {
        kind: "field",
        fieldErrors: { username: "That username is already taken." },
        status: 409,
      };
    }
    if (error.status === 422) {
      return {
        kind: "field",
        fieldErrors: {
          username: "This name was not accepted. Choose a different one.",
          displayName: "This name was not accepted. Choose a different one.",
        },
        status: 422,
      };
    }
  }
  return { kind: "general", generalError: safeWebsiteError(error) };
}

/** True only for the documented unresolved Google-account response. */
export function isAccountNotFoundError(error: unknown): boolean {
  if (!isPythonApiErrorLike(error)) return false;
  if (error.status !== 403 && error.status !== 404) return false;
  return isRecord(error.payload) && error.payload.error === "account_not_found";
}

function isPythonApiErrorLike(
  error: unknown,
): error is { status: number; payload: unknown } {
  return (
    typeof error === "object" &&
    error !== null &&
    "status" in error &&
    "payload" in error &&
    typeof (error as { status?: unknown }).status === "number"
  );
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
