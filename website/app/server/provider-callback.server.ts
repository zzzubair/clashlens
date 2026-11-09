/**
 * Shared completion logic for both provider callback routes.
 *
 * One signed OAuth transaction cookie carries the intent: a browser login, or
 * a link/unlink started from an authenticated account. Link and unlink always
 * require the fresh provider authorization that just completed; collisions
 * are refused without merging, moving, or exposing either account, and an
 * unlink never deletes the Clash Lens account. Provider subjects, tokens,
 * and secrets stay out of browser-visible errors and logs.
 */

import { randomUUID } from "node:crypto";

import type { UNSAFE_DataWithResponseInit as DataWithResponseInit } from "react-router";
import { data } from "react-router";
import {
  isAccountNotFoundError,
  parseCallbackParams,
  parseCookieHeader,
} from "./actions.server";
import type { LoginSession } from "./auth-cookies.server";
import {
  buildClearCookieHeader,
  buildSetCookieHeader,
  createLoginCookieValue,
  createLoginSessionBinding,
  LOGIN_COOKIE_LIFETIME_SECONDS,
  LOGIN_COOKIE_NAME,
  OAUTH_COOKIE_NAME,
  parseLoginCookieValue,
  parseOAuthTransactionCookieValue,
} from "./auth-cookies.server";
import type { LoginIdentity, OAuthTransaction } from "./auth.server";
import { constantTimeEqual, OAuthCallbackError } from "./auth.server";
import type { WebsiteConfig } from "./config.server";
import { getWebsiteConfig } from "./config.server";
import { isLoginRevoked } from "./login-session.server";
import {
  accountSetupPath,
  DEFAULT_RETURN_PATH,
  safeReturnPath,
  setupReturnPath,
} from "./return-path.server";

const NO_STORE = { "Cache-Control": "no-store" };

export interface CallbackLoaderData {
  error: { code: string; message: string } | null;
}

export type CallbackErrorCode =
  | "unavailable"
  | "invalid_callback"
  | "login_required"
  | "provider_conflict"
  | "final_provider"
  | "provider_not_linked";

export interface CallbackErrorView {
  kind: "error";
  status: number;
  code: CallbackErrorCode;
  message: string;
  clearTransactionCookie?: string;
}

interface SuccessRedirect {
  kind: "redirect";
  location: string;
  setCookies: string[];
}

export type CallbackOutcome = CallbackErrorView | SuccessRedirect;

/**
 * One provider sign-in transaction, from the authorize route to its callback.
 *
 * `startProviderAuthorization` begins it: a signed transaction cookie
 * (ten minutes, bound to one provider, with the intent and, for link or
 * unlink, the fingerprint of the login that started it) and a redirect to
 * Google or Discord. When the provider sends the browser back,
 * `readReturnedTransaction` decides which of these it is:
 *
 * - `rejected`: bad callback parameters; a missing, expired, forged or
 *   other-provider transaction; or a link or unlink whose starting login is
 *   gone, different, logged out, or could not be checked.
 * - `signingIn`: a login or signup.
 * - `changingConnection`: a link or unlink by the same still-valid login.
 *
 * The provider's identity is then validated and the callback finishes with
 * exactly one `CallbackOutcome`: a login cookie, an account setup or "no
 * account" redirect, a linked or unlinked connection, or a refusal. Every
 * outcome clears the transaction cookie, so a transaction is used once and
 * any failure needs a fresh start. Nothing else stores the transaction.
 */
type ReturnedTransaction =
  | { kind: "rejected"; outcome: CallbackErrorView }
  | { kind: "signingIn"; transaction: OAuthTransaction }
  | {
      kind: "changingConnection";
      transaction: OAuthTransaction;
      session: LoginSession;
      rawLoginCookie: string;
    };

async function readReturnedTransaction(
  cookiesMap: Map<string, string>,
  provider: "google" | "discord",
  config: WebsiteConfig,
  clearTransactionCookie: string,
): Promise<ReturnedTransaction> {
  const rejected = (outcome: CallbackErrorView): ReturnedTransaction => ({
    kind: "rejected",
    outcome,
  });
  const rawTransaction = cookiesMap.get(OAUTH_COOKIE_NAME);
  if (rawTransaction === undefined) {
    return rejected(errorView(400, "invalid_callback", clearTransactionCookie));
  }
  const transaction = parseOAuthTransactionCookieValue(
    rawTransaction,
    config.loginSecret,
    Math.floor(Date.now() / 1000),
  );
  if (transaction === null || transaction.provider !== provider) {
    return rejected(errorView(400, "invalid_callback", clearTransactionCookie));
  }
  if (transaction.intent === "login" || transaction.intent === "signup") {
    return { kind: "signingIn", transaction };
  }

  const loginRequired = rejected({
    kind: "error",
    status: 400,
    code: "login_required",
    message: "Sign in to your Clash Lens account before changing sign-in connections.",
    clearTransactionCookie,
  });
  const rawLoginCookie = cookiesMap.get(LOGIN_COOKIE_NAME);
  const session = parseLoginCookieValue(
    rawLoginCookie,
    config.loginSecret,
    Math.floor(Date.now() / 1000),
  );
  if (
    session === null ||
    rawLoginCookie === undefined ||
    transaction.sessionBinding === undefined ||
    !constantTimeEqual(
      transaction.sessionBinding,
      createLoginSessionBinding(rawLoginCookie),
    )
  ) {
    return loginRequired;
  }
  // The same login started this change, but it may have logged out since.
  try {
    if (await isLoginRevoked(session, rawLoginCookie)) return loginRequired;
  } catch {
    return rejected(unavailable(clearTransactionCookie));
  }
  return { kind: "changingConnection", transaction, session, rawLoginCookie };
}

/**
 * Complete one provider callback. `validate` runs the provider-specific token
 * exchange and identity validation; everything else (transaction cookie
 * handling, intents, account dispatch) is shared. See `ReturnedTransaction`.
 */
export async function completeProviderCallback(
  request: Request,
  provider: "google" | "discord",
  validate: (
    params: Record<string, string>,
    transaction: OAuthTransaction,
  ) => Promise<LoginIdentity>,
): Promise<CallbackOutcome> {
  let config: WebsiteConfig;
  try {
    config = getWebsiteConfig();
  } catch {
    return unavailable();
  }
  const clearTransactionCookie = buildClearCookieHeader(
    OAUTH_COOKIE_NAME,
    config.cookieSecure,
  );

  const params = parseCallbackParams(new URL(request.url).search);
  if (params === null) {
    return errorView(400, "invalid_callback", clearTransactionCookie);
  }
  const returned = await readReturnedTransaction(
    parseCookieHeader(request.headers.get("cookie")),
    provider,
    config,
    clearTransactionCookie,
  );
  if (returned.kind === "rejected") return returned.outcome;
  const { transaction } = returned;

  let validated: LoginIdentity;
  try {
    validated = await validate(params, transaction);
  } catch (error) {
    return errorView(
      error instanceof OAuthCallbackError ? 400 : 503,
      error instanceof OAuthCallbackError ? "invalid_callback" : "unavailable",
      clearTransactionCookie,
    );
  }
  if (validated.provider !== provider) {
    return errorView(400, "invalid_callback", clearTransactionCookie);
  }

  if (returned.kind === "signingIn") {
    const loginCookie = buildSetCookieHeader(
      LOGIN_COOKIE_NAME,
      createLoginCookieValue(validated, config.loginSecret, Date.now()),
      LOGIN_COOKIE_LIFETIME_SECONDS,
      config.cookieSecure,
    );
    const python = await import("../services/python.server");
    try {
      await python.createPythonClient(validated).getAccount();
    } catch (error) {
      // Sign-up makes the account; a sign-in asks first, signed out, so a
      // Clasher who pressed the other provider doesn't make a second account
      // by mistake.
      if (isAccountNotFoundError(error)) {
        return transaction.intent === "signup"
          ? {
              kind: "redirect",
              location: accountSetupPath(transaction.returnPath, config.publicOrigin),
              setCookies: [clearTransactionCookie, loginCookie],
            }
          : {
              kind: "redirect",
              location: noAccountPath(
                provider,
                transaction.returnPath,
                config.publicOrigin,
              ),
              setCookies: [clearTransactionCookie],
            };
      }
      return unavailable(clearTransactionCookie);
    }
    return {
      kind: "redirect",
      location:
        safeReturnPath(transaction.returnPath, config.publicOrigin) ??
        DEFAULT_RETURN_PATH,
      setCookies: [clearTransactionCookie, loginCookie],
    };
  }

  const { session, rawLoginCookie } = returned;
  const python = await import("../services/python.server");
  const client = python.createPythonClient(session);
  let currentProviders: string[];
  try {
    currentProviders = (await client.getAccount()).providers;
  } catch (error) {
    if (isAccountNotFoundError(error)) {
      // Every outcome clears the transaction cookie; a thrown redirect would
      // leave it alive.
      return {
        kind: "redirect",
        location: "/account/setup",
        setCookies: [clearTransactionCookie],
      };
    }
    return unavailable(clearTransactionCookie);
  }

  if (transaction.intent === "link") {
    try {
      await client.linkProvider(provider, validated.providerSubject, randomUUID());
    } catch (error) {
      const code = pythonErrorCode(error);
      if (code === "provider_identity_conflict") {
        return {
          kind: "error",
          status: 409,
          code: "provider_conflict",
          message:
            "That sign-in connection already belongs to another Clash Lens " +
            "account. No accounts were changed.",
          clearTransactionCookie,
        };
      }
      void code;
      return unavailable(clearTransactionCookie);
    }
    return {
      kind: "redirect",
      // Connecting from the Discord alerts page goes back there.
      location: `${
        transaction.returnPath === "/account/discord-alerts"
          ? transaction.returnPath
          : "/account/settings"
      }?saved=${provider}-connected`,
      setCookies: [clearTransactionCookie],
    };
  }

  // Unlink: the fresh authorization must match the identity currently linked
  // to this account for this provider; nothing else may be removed.
  if (!currentProviders.includes(provider)) {
    return {
      kind: "error",
      status: 409,
      code: "provider_not_linked",
      message: "That sign-in connection was not found on your account.",
      clearTransactionCookie,
    };
  }
  // Removing the provider that created this login also ends the login in the
  // same API transaction, so a copied cookie cannot come back if that
  // provider is linked again later.
  const endsLogin = session.provider === provider;
  try {
    await client.unlinkProvider(
      provider,
      validated.providerSubject,
      randomUUID(),
      endsLogin ? createLoginSessionBinding(rawLoginCookie) : undefined,
    );
  } catch (error) {
    const code = pythonErrorCode(error);
    if (code === "final_provider") {
      return {
        kind: "error",
        status: 409,
        code: "final_provider",
        message: "Your last remaining sign-in connection cannot be removed.",
        clearTransactionCookie,
      };
    }
    if (code === "provider_identity_conflict") {
      return {
        kind: "error",
        status: 409,
        code: "provider_conflict",
        message:
          "That sign-in connection already belongs to another Clash Lens " +
          "account. No accounts were changed.",
        clearTransactionCookie,
      };
    }
    if (code === "provider_not_linked") {
      return {
        kind: "error",
        status: 409,
        code: "provider_not_linked",
        message: "That sign-in connection was not found on your account.",
        clearTransactionCookie,
      };
    }
    return unavailable(clearTransactionCookie);
  }

  // The login has ended; ask for login through the remaining provider.
  if (endsLogin) {
    return {
      kind: "redirect",
      location: "/login",
      setCookies: [
        clearTransactionCookie,
        buildClearCookieHeader(LOGIN_COOKIE_NAME, config.cookieSecure),
      ],
    };
  }
  return {
    kind: "redirect",
    location: `/account/settings?saved=${provider}-disconnected`,
    setCookies: [clearTransactionCookie],
  };
}

/** The sign-in page's "No account found" answer for this provider. */
function noAccountPath(
  provider: "google" | "discord",
  returnPath: string,
  origin: URL,
): string {
  const path = setupReturnPath(returnPath, origin);
  const back =
    path === DEFAULT_RETURN_PATH ? "" : `&returnPath=${encodeURIComponent(path)}`;
  return `/login?none=${provider}${back}`;
}

/** Safe documented Python error codes only; everything else stays opaque. */
function pythonErrorCode(error: unknown): string | null {
  const status = (error as { status?: unknown }).status;
  const payload = (error as { payload?: unknown }).payload;
  if (
    typeof status !== "number" ||
    status < 400 ||
    typeof payload !== "object" ||
    payload === null
  ) {
    return null;
  }
  const code = (payload as Record<string, unknown>)["error"];
  if (
    (code !== "provider_identity_conflict" &&
      code !== "final_provider" &&
      code !== "provider_not_linked") ||
    !["404", "409"].includes(String(status))
  ) {
    return null;
  }
  return code;
}

function unavailable(setCookie?: string): CallbackErrorView {
  return errorView(503, "unavailable", setCookie);
}

function errorView(
  status: number,
  code: CallbackErrorCode,
  setCookie?: string,
): CallbackErrorView {
  return {
    kind: "error",
    status,
    code,
    message: "Sign-in could not be completed. Try again.",
    ...(setCookie === undefined ? {} : { clearTransactionCookie: setCookie }),
  };
}

export function callbackErrorResponse(
  outcome: CallbackErrorView,
): DataWithResponseInit<CallbackLoaderData> {
  return data<CallbackLoaderData>(
    { error: { code: outcome.code, message: outcome.message } },
    {
      status: outcome.status,
      headers: {
        ...NO_STORE,
        ...(outcome.clearTransactionCookie === undefined
          ? {}
          : { "Set-Cookie": outcome.clearTransactionCookie }),
      },
    },
  );
}

export function redirectOutcomeResponse(outcome: SuccessRedirect): Response {
  const headers = new Headers({ Location: outcome.location, ...NO_STORE });
  for (const setCookie of outcome.setCookies) {
    headers.append("Set-Cookie", setCookie);
  }
  return new Response(null, { status: 302, headers });
}
