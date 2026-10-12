import type { WebsiteConfig } from "./config.server";
import type { OAuthIntent } from "./google-oidc.server";
import { countSignInStep } from "./usage-events.server";

/** Start one provider transaction after the route has authorized its intent. */
export async function startProviderAuthorization(
  config: WebsiteConfig,
  provider: "google" | "discord",
  intent: OAuthIntent,
  returnPath: string,
  sessionBinding?: string,
): Promise<Response> {
  const oidc = await import("./google-oidc.server");
  const cookies = await import("./auth-cookies.server");
  const nowSeconds = Math.floor(Date.now() / 1000);
  const transaction = oidc.createOAuthTransaction(
    returnPath,
    nowSeconds,
    undefined,
    intent,
    provider,
    sessionBinding,
  );
  const service =
    provider === "google"
      ? await oidc.createGoogleOidcService(config)
      : await (await import("./discord-oauth.server")).createDiscordOAuthService(config);
  const location = service.authorizationUrl(transaction).toString();
  // Linking or removing a connection is not a sign-in.
  if (intent === "login") countSignInStep(provider, "started");

  return new Response(null, {
    status: 302,
    headers: {
      Location: location,
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
