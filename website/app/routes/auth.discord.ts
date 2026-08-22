import { redirect } from "react-router";

import type { Route } from "./+types/auth.discord";

/**
 * GET /auth/discord — start a fresh Discord authorization transaction.
 *
 * Discord uses OAuth2 Authorization Code with PKCE S256 and the `identify`
 * scope only. The return path is validated against the exact public origin,
 * a new one-time transaction is created for this provider with its intent
 * (login, or a link/unlink started from an authenticated account), and its
 * signed transaction cookie is set before the browser is redirected to
 * Discord.
 */
export async function loader({ request }: Route.LoaderArgs): Promise<Response> {
  const { getWebsiteConfig } = await import("../server/config.server");
  const { safeReturnPath, DEFAULT_RETURN_PATH } =
    await import("../server/return-path.server");
  const discord = await import("../server/discord-oauth.server");
  const oidc = await import("../server/google-oidc.server");
  const cookies = await import("../server/auth-cookies.server");
  const { parseTransactionIntent } = await import("../server/provider-callback.server");

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
  const intent = parseTransactionIntent(url.searchParams.get("intent"));
  if (intent !== "login") {
    // Link and unlink start from an authenticated account only.
    const actions = await import("../server/actions.server");
    if (actions.readLoginIdentity(request, config) === null) {
      throw redirect("/login");
    }
  }
  const nowSeconds = Math.floor(Date.now() / 1000);
  const transaction = oidc.createOAuthTransaction(
    returnPath,
    nowSeconds,
    undefined,
    intent,
    "discord",
  );
  let service;
  try {
    service = await discord.createDiscordOAuthService(config);
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
