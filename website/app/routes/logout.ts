import { redirect } from "react-router";

import type { Route } from "./+types/logout";

/**
 * POST /logout — end the current browser login. The private API records the
 * login as logged out, so a copy of the cookie stops working too, and the
 * browser cookie is cleared. If the API cannot record it, the cookie is still
 * cleared and home says the logout was not recorded; otherwise it says
 * "You're logged out.".
 * Logout is a same-origin cookie-authenticated mutation, so it follows the
 * same origin rule as every other account action.
 * "Not you?" on account setup sends `signUp` with its return path, and goes
 * on to /signup with that path instead of home.
 * A plain GET to this route just returns home.
 */
export async function loader(): Promise<Response> {
  throw redirect("/");
}

export async function action({ request }: Route.ActionArgs): Promise<Response> {
  if (request.method !== "POST") throw redirect("/");
  const { getWebsiteConfig } = await import("../server/config.server");
  const cookies = await import("../server/auth-cookies.server");
  const actions = await import("../server/actions.server");
  const { DEFAULT_RETURN_PATH, setupReturnPath } =
    await import("../server/return-path.server");

  let config;
  try {
    config = getWebsiteConfig();
  } catch {
    throw redirect("/");
  }
  if (!actions.isSameOrigin(request, config.publicOrigin)) {
    return new Response(null, { status: 403, headers: NO_STORE });
  }
  const form = await actions.parseBoundedFormData(request);
  if (form === null || !actions.isIdempotencyKey(form.idempotencyKey)) {
    return new Response(null, { status: 400, headers: NO_STORE });
  }
  let next = "/?saved=logout";
  if (form.signUp !== undefined) {
    const returnPath = setupReturnPath(form.signUp, config.publicOrigin);
    next =
      returnPath === DEFAULT_RETURN_PATH
        ? "/signup"
        : `/signup?returnPath=${encodeURIComponent(returnPath)}`;
  }
  const loginCookie = actions
    .parseCookieHeader(request.headers.get("cookie"))
    .get(cookies.LOGIN_COOKIE_NAME);
  const identity =
    loginCookie === undefined || !config.loginEnabled || config.loginSecret.length !== 32
      ? null
      : cookies.parseLoginCookieValue(
          loginCookie,
          config.loginSecret,
          Math.floor(Date.now() / 1000),
        );
  let recorded = true;
  if (identity !== null && loginCookie !== undefined) {
    const { revokeLogin } = await import("../server/login-session.server");
    recorded = await revokeLogin(identity, loginCookie).then(
      () => true,
      () => false,
    );
  }
  return new Response(null, {
    status: 302,
    headers: {
      Location: recorded ? next : "/?logout=unrecorded",
      "Set-Cookie": cookies.buildClearCookieHeader(
        cookies.LOGIN_COOKIE_NAME,
        config.cookieSecure,
      ),
      ...NO_STORE,
    },
  });
}

const NO_STORE = { "Cache-Control": "no-store" };

export function headers() {
  return NO_STORE;
}
