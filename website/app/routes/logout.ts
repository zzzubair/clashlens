import { redirect } from "react-router";

import type { Route } from "./+types/logout";

/**
 * POST /logout — end the current browser login. The private API records the
 * login as logged out, so a copy of the cookie stops working too, and the
 * browser cookie is cleared. If the API cannot record it, the cookie is still
 * cleared but the response is 503. Logout is a same-origin cookie-authenticated
 * mutation, so it follows the same origin rule as every other account action.
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
    status: recorded ? 302 : 503,
    headers: {
      ...(recorded ? { Location: "/" } : {}),
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
