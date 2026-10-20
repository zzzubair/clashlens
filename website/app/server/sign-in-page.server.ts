import { redirect } from "react-router";

import { isAccountNotFoundError, readLoginIdentity } from "./actions.server";
import { getWebsiteConfig } from "./config.server";
import { NAVIGATION_ACCOUNT_TIMEOUT_MS } from "./root-navigation.server";
import {
  accountSetupPath,
  DEFAULT_RETURN_PATH,
  safeReturnPath,
} from "./return-path.server";

export interface SignInPageData {
  loginAvailable: boolean;
  /** Validated page to open once signed in or signed up. */
  returnPath: string;
  /** The crew whose invite link the Clasher came from, if any. */
  crewName: string | null;
  /** On /login, the provider a sign-in just found no Clash Lens account for. */
  noAccount: "google" | "discord" | null;
}

/**
 * Shared loader for /login and /signup. Someone already signed in to an
 * account goes straight on to the return path; on /signup, a sign-in with no
 * account goes on to choosing a username. Login that is disabled or
 * unconfigured keeps the page usable with a safe notice.
 */
export async function loadSignInPage(
  request: Request,
  page: "login" | "signup",
): Promise<SignInPageData> {
  let config;
  try {
    config = getWebsiteConfig();
  } catch {
    return {
      loginAvailable: false,
      returnPath: DEFAULT_RETURN_PATH,
      crewName: null,
      noAccount: null,
    };
  }
  const query = new URL(request.url).searchParams;
  const returnPath =
    safeReturnPath(query.get("returnPath"), config.publicOrigin) ?? DEFAULT_RETURN_PATH;
  const none = page === "login" && config.loginEnabled ? query.get("none") : null;
  // A login the API cannot check shows the page rather than redirecting.
  const identity = config.loginEnabled
    ? await readLoginIdentity(request, config, NAVIGATION_ACCOUNT_TIMEOUT_MS).catch(
        () => null,
      )
    : null;
  if (identity !== null) {
    const { createPythonClient } = await import("../services/python.server");
    try {
      await createPythonClient(identity, {
        accountReadTimeoutMs: NAVIGATION_ACCOUNT_TIMEOUT_MS,
      }).getAccount();
      throw redirect(returnPath);
    } catch (cause) {
      // A slow or failed account read leaves the return page to say so.
      if (cause instanceof Response || !isAccountNotFoundError(cause)) {
        throw cause instanceof Response ? cause : redirect(returnPath);
      }
      if (page === "signup") {
        throw redirect(accountSetupPath(returnPath, config.publicOrigin));
      }
    }
  }
  const { inviteCrewName } = await import("../services/crews.server");
  return {
    loginAvailable: config.loginEnabled,
    returnPath,
    crewName: await inviteCrewName(returnPath),
    noAccount: none === "google" || none === "discord" ? none : null,
  };
}
