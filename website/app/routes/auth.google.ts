import { redirect } from "react-router";

import type { Route } from "./+types/auth.google";

/**
 * GET /auth/google — start a fresh Google authorization transaction.
 *
 * The return path is validated against the exact public origin, a new
 * one-time OAuth transaction (state, nonce, PKCE) is created with its intent
 * (login, or a link/unlink started from an authenticated account), and its
 * signed transaction cookie is set with the fixed ten-minute lifetime before
 * the browser is redirected to the provider. The provider receives only the
 * openid scope.
 */
export async function loader({ request }: Route.LoaderArgs): Promise<Response> {
  const { getWebsiteConfig } = await import("../server/config.server");
  const { safeReturnPath, DEFAULT_RETURN_PATH } =
    await import("../server/return-path.server");
  const { startProviderAuthorization } = await import("../server/provider-start.server");

  let config;
  try {
    config = getWebsiteConfig();
  } catch {
    throw redirect("/login");
  }
  if (!config.loginEnabled) throw redirect("/login");

  const url = new URL(request.url);
  const rawReturnPath = url.searchParams.get("returnPath");
  const returnPath =
    safeReturnPath(rawReturnPath, config.publicOrigin) ?? DEFAULT_RETURN_PATH;
  // Privileged intents are started only by the protected account POST action;
  // query parameters on this public GET route can start login only.
  try {
    return await startProviderAuthorization(config, "google", "login", returnPath);
  } catch {
    throw redirect("/login");
  }
}
