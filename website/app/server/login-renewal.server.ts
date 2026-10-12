/**
 * Keeps a browser signed in while it is used. A request with a valid login
 * cookie issued at least an hour ago gets a fresh 30-day cookie for the same
 * login, so the 30 days restart from that visit. The hour limit keeps the
 * extra logout check to one an hour per browser. A logged-out, removed, or
 * unconfirmed login is never renewed, and a response that already sets or
 * clears the login cookie (login, logout, removing a sign-in connection)
 * keeps its own. A response that shared caches may keep never carries one.
 */

import {
  buildSetCookieHeader,
  createLoginCookieValue,
  createLoginSessionBinding,
  LOGIN_COOKIE_LIFETIME_SECONDS,
  LOGIN_COOKIE_NAME,
  LOGIN_COOKIE_RENEW_AFTER_SECONDS,
  parseLoginCookieValue,
} from "./auth-cookies.server";
import { parseCookieHeader } from "./actions.server";
import { getWebsiteConfig } from "./config.server";
import { isLoginRevoked } from "./login-session.server";

const RENEWAL_CHECK_TIMEOUT_MS = 1_000;

export async function renewLoginCookie(
  request: Request,
  next: () => Promise<Response>,
): Promise<Response> {
  // The logout check runs alongside the page's own loaders.
  const renewal = renewedLoginCookie(request);
  const response = await next();
  const cookie = await renewal;
  if (
    cookie === null ||
    /\bpublic\b/i.test(response.headers.get("Cache-Control") ?? "") ||
    response.headers
      .getSetCookie()
      .some((header) => header.startsWith(`${LOGIN_COOKIE_NAME}=`))
  ) {
    return response;
  }
  const headers = new Headers(response.headers);
  headers.append("Set-Cookie", cookie);
  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

async function renewedLoginCookie(request: Request): Promise<string | null> {
  try {
    const config = getWebsiteConfig();
    if (!config.loginEnabled || config.loginSecret.length !== 32) return null;
    const value = parseCookieHeader(request.headers.get("cookie")).get(LOGIN_COOKIE_NAME);
    if (value === undefined) return null;
    // Issued before the check, so a sign-in connection removed while it runs
    // still ends the renewed cookie.
    const nowMs = Date.now();
    const session = parseLoginCookieValue(
      value,
      config.loginSecret,
      Math.floor(nowMs / 1000),
    );
    if (
      session === null ||
      nowMs - session.issuedAtMs < LOGIN_COOKIE_RENEW_AFTER_SECONDS * 1000 ||
      (await isLoginRevoked(session, value, RENEWAL_CHECK_TIMEOUT_MS))
    ) {
      return null;
    }
    return buildSetCookieHeader(
      LOGIN_COOKIE_NAME,
      createLoginCookieValue(
        { provider: session.provider, providerSubject: session.providerSubject },
        config.loginSecret,
        nowMs,
        createLoginSessionBinding(value),
      ),
      LOGIN_COOKIE_LIFETIME_SECONDS,
      config.cookieSecure,
    );
  } catch {
    // Without a confirmed login the cookie stays as it is.
    return null;
  }
}
