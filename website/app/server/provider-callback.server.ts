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

import { data } from "react-router";

import { parseCallbackParams, parseCookieHeader } from "./actions.server";
import { isAccountNotFoundError } from "./actions.server";
import {
  buildClearCookieHeader,
  buildSetCookieHeader,
  createLoginCookieValue,
  LOGIN_COOKIE_LIFETIME_SECONDS,
  LOGIN_COOKIE_NAME,
  OAUTH_COOKIE_NAME,
  parseLoginCookieValue,
  parseOAuthTransactionCookieValue,
} from "./auth-cookies.server";
import { getWebsiteConfig } from "./config.server";
import type { WebsiteConfig } from "./config.server";
import { OAuthCallbackError } from "./google-oidc.server";
import type { OAuthTransaction } from "./google-oidc.server";
import type { LoginProviderIdentity } from "../services/python.server";
import type { UNSAFE_DataWithResponseInit as DataWithResponseInit } from "react-router";

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
 * Complete one provider callback. `validate` runs the provider-specific token
 * exchange and identity validation; everything else (transaction cookie
 * handling, intents, account dispatch) is shared. The signed transaction is
 * bound to this route's provider: a transaction minted by the other
 * authorize route is rejected, and every outcome clears the transaction
 * cookie.
 */
export async function completeProviderCallback(
  request: Request,
  provider: "google" | "discord",
  validate: (
    params: Record<string, string>,
    transaction: OAuthTransaction,
  ) => Promise<LoginProviderIdentity>,
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
  const cookiesMap = parseCookieHeader(request.headers.get("cookie"));
  const rawTransaction = cookiesMap.get(OAUTH_COOKIE_NAME);
  if (rawTransaction === undefined) {
    return errorView(400, "invalid_callback", clearTransactionCookie);
  }
  const transaction = parseOAuthTransactionCookieValue(
    rawTransaction,
    config.loginSecret,
    Math.floor(Date.now() / 1000),
  );
  if (transaction === null || transaction.provider !== provider) {
    return errorView(400, "invalid_callback", clearTransactionCookie);
  }

  let validated: LoginProviderIdentity;
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

  const nowSeconds = Math.floor(Date.now() / 1000);

  if (transaction.intent === "login") {
    const loginCookie = buildSetCookieHeader(
      LOGIN_COOKIE_NAME,
      createLoginCookieValue(validated, config.loginSecret, nowSeconds),
      LOGIN_COOKIE_LIFETIME_SECONDS,
      config.cookieSecure,
    );
    const python = await import("../services/python.server");
    try {
      await python.createPythonClient(validated).getAccount();
    } catch (error) {
      if (isAccountNotFoundError(error)) {
        return {
          kind: "redirect",
          location: "/account/setup",
          setCookies: [clearTransactionCookie, loginCookie],
        };
      }
      return unavailable(clearTransactionCookie);
    }
    return {
      kind: "redirect",
      location: safeLocalPath(transaction.returnPath),
      setCookies: [clearTransactionCookie, loginCookie],
    };
  }

  // Link and unlink start from an authenticated account: the signed login
  // cookie must still be present and valid at callback time.
  const session = parseLoginCookieValue(
    cookiesMap.get(LOGIN_COOKIE_NAME),
    config.loginSecret,
    nowSeconds,
  );
  if (session === null) {
    return {
      kind: "error",
      status: 400,
      code: "login_required",
      message: "Sign in to your Clash Lens account before changing sign-in connections.",
      clearTransactionCookie,
    };
  }

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
      location: "/account/providers",
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
  try {
    await client.unlinkProvider(provider, validated.providerSubject, randomUUID());
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

  // If the unlinked provider created the current session, clear it and ask
  // for login through the remaining provider.
  if (session.provider === provider) {
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
    location: "/account/providers",
    setCookies: [clearTransactionCookie],
  };
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

/** Only same-origin absolute paths survive the round trip. */
function safeLocalPath(value: string): string {
  return value.startsWith("/") && !value.startsWith("//") ? value : "/";
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

/** Strict intent allowlist for authorize-route query parameters. */
export function parseTransactionIntent(
  value: string | null,
): "login" | "link" | "unlink" {
  return value === "link" || value === "unlink" ? value : "login";
}
