import { freshIdempotencyKey, readLoginIdentity } from "./actions.server";
import { getWebsiteConfig } from "./config.server";
import { createPythonClient } from "../services/python.server";

const NAVIGATION_ACCOUNT_TIMEOUT_MS = 250;

export interface RootNavigationData {
  loggedIn: boolean;
  accountLabel: string | null;
  logoutIdempotencyKey: string | null;
}

export async function loadRootNavigation(request: Request): Promise<RootNavigationData> {
  try {
    const config = getWebsiteConfig();
    if (!config.loginEnabled) return loggedOutNavigation();
    const identity = readLoginIdentity(request, config);
    if (identity === null) return loggedOutNavigation();

    let accountLabel: string | null = null;
    try {
      const account = await createPythonClient(identity, {
        accountReadTimeoutMs: NAVIGATION_ACCOUNT_TIMEOUT_MS,
      }).getAccount();
      accountLabel = account.displayName;
    } catch {
      accountLabel = null;
    }
    return {
      loggedIn: true,
      accountLabel,
      logoutIdempotencyKey: freshIdempotencyKey(),
    };
  } catch {
    return loggedOutNavigation();
  }
}

function loggedOutNavigation(): RootNavigationData {
  return {
    loggedIn: false,
    accountLabel: null,
    logoutIdempotencyKey: null,
  };
}
