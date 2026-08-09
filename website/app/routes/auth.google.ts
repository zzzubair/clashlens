import { redirect } from "react-router";

import type { Route } from "./+types/auth.google";

/**
 * GET /auth/google — start a fresh Google authorization transaction.
 *
 * The return path is validated against the exact public origin, a new
 * one-time OAuth transaction (state, nonce, PKCE) is created, and its signed
 * transaction cookie is set with the fixed ten-minute lifetime before the
 * browser is redirected to the provider. The provider receives only the
 * openid scope.
 */
export async function loader({ request }: Route.LoaderArgs): Promise<Response> {
  const { getWebsiteConfig } = await import("../server/config.server");
  const { safeReturnPath, DEFAULT_RETURN_PATH } =
    await import("../server/return-path.server");
  const oidc = await import("../server/google-oidc.server");
  const cookies = await import("../server/auth-cookies.server");

  let config;
  try {
    config = getWebsiteConfig();
  } catch {
    throw redirect("/login");
  }
  if (!config.loginEnabled) throw redirect("/login");

  const rawReturnPath = new URL(request.url).searchParams.get("returnPath");
  const returnPath =
    safeReturnPath(rawReturnPath, config.publicOrigin) ?? DEFAULT_RETURN_PATH;
  const nowSeconds = Math.floor(Date.now() / 1000);
  const transaction = oidc.createOAuthTransaction(returnPath, nowSeconds);
  let service;
  try {
    service = await oidc.createGoogleOidcService(config);
  } catch {
    throw redirect("/login");
  }

  return new Response(null, {
    status: 302,
    headers: {
      Location: service.authorizationUrl(transaction).toString(),
      "Set-Cookie": cookies.buildSetCookieHeader(
        cookies.OAUTH_COOKIE_NAME,
        cookies.createOAuthTransactionCookieValue(transaction, config.loginSecret),
        cookies.OAUTH_TRANSACTION_LIFETIME_SECONDS,
        config.cookieSecure,
      ),
      "Cache-Control": "no-store",
    },
  });
}
