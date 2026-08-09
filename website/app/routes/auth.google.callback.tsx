import { Link, data, useLoaderData } from "react-router";

import type { Route } from "./+types/auth.google.callback";

const NO_STORE = { "Cache-Control": "no-store" };

export interface CallbackLoaderData {
  error: { code: string; message: string } | null;
}

/**
 * GET /auth/google/callback — validate the provider callback exactly once and
 * establish the 24-hour browser login.
 *
 * The transaction cookie is read and always cleared (success or failure), the
 * callback query is bounded and duplicate-free, and the OIDC service verifies
 * state, nonce, PKCE, issuer, audience, expiry, and subject. Provider
 * responses, tokens, and the provider subject are never exposed to the
 * browser. The identity is used only to sign private Python account calls:
 * an existing account redirects to the validated return path, a missing
 * account (account_not_found) means setup is still unresolved and redirects
 * to /account/setup, and every other failure renders a safe error.
 */
export async function loader({ request }: Route.LoaderArgs) {
  const actions = await import("../server/actions.server");
  const { getWebsiteConfig } = await import("../server/config.server");
  const cookies = await import("../server/auth-cookies.server");
  const oidc = await import("../server/google-oidc.server");

  let config;
  try {
    config = getWebsiteConfig();
  } catch {
    return callbackError(503, "unavailable");
  }
  const clearTransactionCookie = cookies.buildClearCookieHeader(
    cookies.OAUTH_COOKIE_NAME,
    config.cookieSecure,
  );

  const params = actions.parseCallbackParams(new URL(request.url).search);
  if (params === null) {
    return callbackError(400, "invalid_callback", clearTransactionCookie);
  }
  const cookiesMap = actions.parseCookieHeader(request.headers.get("cookie"));
  const rawTransaction = cookiesMap.get(cookies.OAUTH_COOKIE_NAME);
  if (rawTransaction === undefined) {
    return callbackError(400, "invalid_callback", clearTransactionCookie);
  }
  const transaction = cookies.parseOAuthTransactionCookieValue(
    rawTransaction,
    config.loginSecret,
    Math.floor(Date.now() / 1000),
  );
  if (transaction === null) {
    return callbackError(400, "invalid_callback", clearTransactionCookie);
  }

  let identity;
  try {
    const service = await oidc.createGoogleOidcService(config);
    identity = await service.validateCallback(params, transaction);
  } catch (error) {
    return callbackError(
      error instanceof oidc.OAuthCallbackError ? 400 : 503,
      "invalid_callback",
      clearTransactionCookie,
    );
  }

  const nowSeconds = Math.floor(Date.now() / 1000);
  const loginCookie = cookies.buildSetCookieHeader(
    cookies.LOGIN_COOKIE_NAME,
    cookies.createLoginCookieValue(identity, config.loginSecret, nowSeconds),
    cookies.LOGIN_COOKIE_LIFETIME_SECONDS,
    config.cookieSecure,
  );

  const python = await import("../services/python.server");
  try {
    await python.createPythonClient(identity).getAccount();
  } catch (error) {
    if (
      error instanceof python.PythonApiError &&
      (error.status === 403 || error.status === 404) &&
      isRecord(error.payload) &&
      error.payload.error === "account_not_found"
    ) {
      return redirectResponse("/account/setup", [clearTransactionCookie, loginCookie]);
    }
    return callbackError(503, "unavailable", clearTransactionCookie);
  }
  return redirectResponse(transaction.returnPath, [clearTransactionCookie, loginCookie]);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function redirectResponse(location: string, setCookies: readonly string[]): Response {
  const headers = new Headers({ Location: location, ...NO_STORE });
  for (const setCookie of setCookies) headers.append("Set-Cookie", setCookie);
  return new Response(null, {
    status: 302,
    headers,
  });
}

function callbackError(status: number, code: string, setCookie?: string) {
  return data<CallbackLoaderData>(
    {
      error: {
        code,
        message: "Google sign-in could not be completed. Try again.",
      },
    },
    {
      status,
      headers: {
        ...NO_STORE,
        ...(setCookie === undefined ? {} : { "Set-Cookie": setCookie }),
      },
    },
  );
}

export function headers() {
  return NO_STORE;
}

export default function CallbackErrorRoute() {
  const data = useLoaderData<CallbackLoaderData>();
  return (
    <main className="page-shell narrow-shell" role="alert">
      <p className="eyebrow">Clash Lens</p>
      <h1>Sign-in could not be completed</h1>
      <p>{data.error?.message ?? "Google sign-in could not be completed. Try again."}</p>
      <Link className="button button-primary" to="/login">
        Try signing in again
      </Link>
      <p className="back-link">
        <Link to="/">← Back to home</Link>
      </p>
    </main>
  );
}
