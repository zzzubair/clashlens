import { redirect } from "react-router";

import type { Route } from "./+types/logout";

/**
 * POST /logout — clear the current browser login. Logout is a same-origin
 * cookie-authenticated mutation, so it follows the same origin rule as every
 * other account action. A plain GET to this route just returns home.
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
  return new Response(null, {
    status: 302,
    headers: {
      Location: "/",
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
