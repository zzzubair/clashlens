import { redirect } from "react-router";
import type { OAuthIntent } from "./auth.server";
import {
  createOAuthTransaction,
  OAUTH_TRANSACTION_LIFETIME_SECONDS,
} from "./auth.server";
import type { WebsiteConfig } from "./config.server";

/**
 * Start one provider transaction after the route has authorized its intent.
 * Its stages and how it finishes are on `ReturnedTransaction` in
 * provider-callback.server.
 */
export async function startProviderAuthorization(
  config: WebsiteConfig,
  provider: "google" | "discord",
  intent: OAuthIntent,
  returnPath: string,
  sessionBinding?: string,
): Promise<Response> {
  const cookies = await import("./auth-cookies.server");
  const nowSeconds = Math.floor(Date.now() / 1000);
  const transaction = createOAuthTransaction(
    returnPath,
    nowSeconds,
    undefined,
    intent,
    provider,
    sessionBinding,
  );
  const service =
    provider === "google"
      ? await (await import("./google-oidc.server")).createGoogleOidcService(config)
      : await (await import("./discord-oauth.server")).createDiscordOAuthService(config);

  return new Response(null, {
    status: 302,
    headers: {
      Location: service.authorizationUrl(transaction).toString(),
      "Set-Cookie": cookies.buildSetCookieHeader(
        cookies.OAUTH_COOKIE_NAME,
        cookies.createOAuthTransactionCookieValue(transaction, config.loginSecret),
        OAUTH_TRANSACTION_LIFETIME_SECONDS,
        config.cookieSecure,
      ),
      "Cache-Control": "no-store",
    },
  });
}

/** Public GETs can only sign in or sign up; account POSTs authorize connection changes. */
export async function startProviderLogin(
  request: Request,
  provider: "google" | "discord",
): Promise<Response> {
  const { getWebsiteConfig } = await import("./config.server");
  const { safeReturnPath, DEFAULT_RETURN_PATH } = await import("./return-path.server");
  let config;
  try {
    config = getWebsiteConfig();
  } catch {
    throw redirect("/login");
  }
  if (!config.loginEnabled) throw redirect("/login");
  const url = new URL(request.url);
  const returnPath =
    safeReturnPath(url.searchParams.get("returnPath"), config.publicOrigin) ??
    DEFAULT_RETURN_PATH;
  const intent = url.searchParams.get("signup") === "1" ? "signup" : "login";
  try {
    return await startProviderAuthorization(config, provider, intent, returnPath);
  } catch {
    throw redirect("/login");
  }
}
