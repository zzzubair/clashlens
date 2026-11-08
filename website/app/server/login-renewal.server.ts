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
import type { LoginSession } from "./auth-cookies.server";
import { parseCookieHeader } from "./actions.server";
import { getWebsiteConfig } from "./config.server";
import type { WebsiteConfig } from "./config.server";
import { isLoginRevoked } from "./login-session.server";

const RENEWAL_CHECK_TIMEOUT_MS = 1_000;

/**
 * Where one browser's login cookie stands for renewal.
 *
 * - `absent`: no login cookie, or login is switched off.
 * - `unusable`: not signed by this site, malformed, or past its 30 days.
 * - `recent`: valid and issued under an hour ago; used as it is, and the
 *   API is not asked.
 * - `due`: valid and issued an hour or more ago; the API is asked whether
 *   the login still stands (`LoginStanding` in Python's login_sessions).
 *
 * A `due` login becomes a fresh 30-day cookie for the same login (same
 * fingerprint and signed-in time) only when the API answers that it still
 * stands. A login that logged out or whose sign-in connection was removed
 * keeps its old cookie, which every request that relies on it refuses
 * (login-session.server). An API that fails or takes over a second leaves
 * the cookie as it is, and the next visit asks again. `loginCookieStage` is
 * the only place the stage is decided; nothing is stored here.
 */
export type LoginCookieStage =
  | { kind: "absent" }
  | { kind: "unusable" }
  | { kind: "recent"; session: LoginSession }
  | { kind: "due"; session: LoginSession; value: string };

export function loginCookieStage(
  request: Request,
  config: WebsiteConfig,
  nowMs: number,
): LoginCookieStage {
  if (!config.loginEnabled || config.loginSecret.length !== 32) return { kind: "absent" };
  const value = parseCookieHeader(request.headers.get("cookie")).get(LOGIN_COOKIE_NAME);
  if (value === undefined) return { kind: "absent" };
  const session = parseLoginCookieValue(
    value,
    config.loginSecret,
    Math.floor(nowMs / 1000),
  );
  if (session === null) return { kind: "unusable" };
  if (nowMs - session.issuedAtMs < LOGIN_COOKIE_RENEW_AFTER_SECONDS * 1000) {
    return { kind: "recent", session };
  }
  return { kind: "due", session, value };
}

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
    const nowMs = Date.now();
    const stage = loginCookieStage(request, config, nowMs);
    if (
      stage.kind !== "due" ||
      (await isLoginRevoked(stage.session, stage.value, RENEWAL_CHECK_TIMEOUT_MS))
    ) {
      return null;
    }
    return buildSetCookieHeader(
      LOGIN_COOKIE_NAME,
      createLoginCookieValue(
        {
          provider: stage.session.provider,
          providerSubject: stage.session.providerSubject,
        },
        config.loginSecret,
        nowMs,
        {
          sessionBinding: createLoginSessionBinding(stage.value),
          signedInAtMs: stage.session.signedInAtMs,
        },
      ),
      LOGIN_COOKIE_LIFETIME_SECONDS,
      config.cookieSecure,
    );
  } catch {
    // Without a confirmed login the cookie stays as it is.
    return null;
  }
}
