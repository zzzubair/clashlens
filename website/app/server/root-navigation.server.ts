import { createHmac } from "node:crypto";

import {
  freshIdempotencyKey,
  isAccountNotFoundError,
  parseCookieHeader,
  readLoginIdentity,
} from "./actions.server";
import {
  LOGIN_COOKIE_NAME,
  createLoginSessionBinding,
  type LoginIdentity,
} from "./auth-cookies.server";
import { getWebsiteConfig } from "./config.server";
import { createPythonClient } from "../services/python.server";
import { PythonApiError } from "../services/python-response.server";

export const NAVIGATION_ACCOUNT_TIMEOUT_MS = 250;

export interface RootNavigationData {
  loggedIn: boolean;
  accountLabel: string | null;
  accountUsername: string | null;
  logoutIdempotencyKey: string | null;
  /** Opaque and the same for one login, so the header never shows another login's name. */
  loginKey: string | null;
}

export async function loadRootNavigation(request: Request): Promise<RootNavigationData> {
  try {
    const config = getWebsiteConfig();
    if (!config.loginEnabled) return loggedOutNavigation();
    let identity: LoginIdentity | null;
    try {
      identity = await readLoginIdentity(request, config, NAVIGATION_ACCOUNT_TIMEOUT_MS);
    } catch (cause) {
      // The cookie is ours and unexpired, but the logout check did not answer
      // in time: a busy moment, not a sign-out. The menu stays, without the
      // account's name; every account page still checks for itself.
      if (cause instanceof PythonApiError && cause.status >= 500)
        return signedInNavigation(request, config.loginSecret, null, null);
      throw cause;
    }
    if (identity === null) return loggedOutNavigation();

    let accountLabel: string | null = null;
    let accountUsername: string | null = null;
    try {
      const account = await createPythonClient(identity, {
        accountReadTimeoutMs: NAVIGATION_ACCOUNT_TIMEOUT_MS,
      }).getAccount();
      accountLabel = account.displayName;
      accountUsername = account.username;
    } catch (cause) {
      // Signed in with Google or Discord before making an account: no account
      // menu, and no Log out, until one exists.
      if (isAccountNotFoundError(cause)) return loggedOutNavigation();
      accountLabel = null;
    }
    return signedInNavigation(request, config.loginSecret, accountLabel, accountUsername);
  } catch {
    return loggedOutNavigation();
  }
}

function signedInNavigation(
  request: Request,
  loginSecret: Buffer,
  accountLabel: string | null,
  accountUsername: string | null,
): RootNavigationData {
  const cookie = parseCookieHeader(request.headers.get("cookie")).get(LOGIN_COOKIE_NAME);
  return {
    loggedIn: true,
    accountLabel,
    accountUsername,
    logoutIdempotencyKey: freshIdempotencyKey(),
    loginKey: createHmac("sha256", loginSecret)
      .update(`navigation:${createLoginSessionBinding(cookie ?? "")}`)
      .digest("base64url")
      .slice(0, 22),
  };
}

function loggedOutNavigation(): RootNavigationData {
  return {
    loggedIn: false,
    accountLabel: null,
    accountUsername: null,
    logoutIdempotencyKey: null,
    loginKey: null,
  };
}
