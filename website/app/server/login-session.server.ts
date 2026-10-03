/**
 * Server-side end of a browser login. The signed login cookie says who signed
 * in; logout stores the SHA-256 of that exact cookie value with the private
 * API, and every request that relies on the cookie asks the API first, so a
 * copied cookie stops working once its login has logged out. Other logins of
 * the same user, on other browsers, are not affected.
 */

import { createLoginSessionBinding } from "./auth-cookies.server";
import type { LoginIdentity } from "./auth-cookies.server";
import { PythonApiError, requestJson } from "../services/python.server";

/**
 * True when this login has logged out. Throws PythonApiError when the API
 * cannot answer, so no caller treats an unchecked cookie as signed in.
 */
export async function isLoginRevoked(
  identity: LoginIdentity,
  loginCookieValue: string,
): Promise<boolean> {
  const payload = await requestJson<{ revoked?: unknown }>(
    "/v1/account/session/check",
    "POST",
    sessionBody(loginCookieValue),
    undefined,
    undefined,
    identity,
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

function sessionBody(loginCookieValue: string): Buffer {
  return Buffer.from(
    JSON.stringify({ session: createLoginSessionBinding(loginCookieValue) }),
    "utf8",
  );
}
