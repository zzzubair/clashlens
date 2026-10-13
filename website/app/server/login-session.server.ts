/**
 * Server-side end of a browser login. The signed login cookie says who signed
 * in and when; logout stores the login's fingerprint, the SHA-256 of the
 * cookie that started it, which renewed cookies carry, with the private API,
 * and every request that relies on the cookie asks the API first, so a copied
 * or renewed cookie stops working once its login has logged out. Other logins
 * of the same user, on other browsers, are not affected by logout. Removing a
 * sign-in connection ends every login signed in through it before the removal,
 * including every later renewal of that login's cookie.
 */

import { createLoginSessionBinding } from "./auth-cookies.server";
import type { LoginIdentity, LoginSession } from "./auth-cookies.server";
import { PythonApiError, requestJson } from "../services/python.server";

/**
 * True when this login has logged out or its sign-in connection was removed
 * after it signed in. Throws PythonApiError when the API cannot answer, so
 * no caller treats an unchecked cookie as signed in.
 */
export async function isLoginRevoked(
  session: LoginSession,
  loginCookieValue: string,
  timeoutMs?: number,
): Promise<boolean> {
  const payload = await requestJson<{ revoked?: unknown }>(
    "/v1/account/session/check",
    "POST",
    sessionBody(loginCookieValue, session.signedInAtMs),
    undefined,
    undefined,
    session,
    undefined,
    timeoutMs,
  );
  if (typeof payload?.revoked !== "boolean") {
    throw new PythonApiError(502, { error: "malformed" });
  }
  return payload.revoked;
}

/** Record that this login logged out. Throws PythonApiError on failure. */
export async function revokeLogin(
  identity: LoginIdentity,
  loginCookieValue: string,
): Promise<void> {
  await requestJson<unknown>(
    "/v1/account/session/revoke",
    "POST",
    sessionBody(loginCookieValue),
    undefined,
    undefined,
    identity,
  );
}

function sessionBody(loginCookieValue: string, issuedAtMs?: number): Buffer {
  return Buffer.from(
    JSON.stringify({
      session: createLoginSessionBinding(loginCookieValue),
      ...(issuedAtMs === undefined ? {} : { issued_at_ms: issuedAtMs }),
    }),
    "utf8",
  );
}
