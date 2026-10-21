/**
 * Server-only auth guard for account loaders and actions.
 *
 * The guard reads the signed login cookie and yields the validated Google
 * identity for the private Python client. A page opened without a login
 * answers 401 with the current page as a validated same-origin return path,
 * and the site shows that page's "needs an account" box in place. A form
 * sent without one, or with login disabled or unconfigured, redirects to
 * /login with the same return path. Callers receive the identity for
 * server-side signing only; route loader data must never include the
 * provider or provider subject.
 */

import { data, redirect } from "react-router";

import type { NeedsAccountData } from "../components/NeedsAccount";
import type { LoginIdentity } from "./auth-cookies.server";
import { readLoginIdentity } from "./actions.server";
import type { WebsiteConfig } from "./config.server";
import { getWebsiteConfig } from "./config.server";
import {
  ACCOUNT_SETUP_PATH,
  DEFAULT_RETURN_PATH,
  safeReturnPath,
  setupReturnPath,
} from "./return-path.server";

/**
 * Require a valid browser login. A page (GET) whose login cookie is missing,
 * malformed, tampered, expired, or logged out throws a 401 carrying
 * NeedsAccountData; a form throws a redirect Response to /login, as does any
 * request while login is disabled or configuration is missing. Throws a 503
 * Response when the private API cannot say whether the login logged out.
 */
export async function requireLogin(request: Request): Promise<LoginIdentity> {
  let config: WebsiteConfig;
  try {
    config = getWebsiteConfig();
  } catch {
    throw redirect("/login");
  }
  if (!config.loginEnabled) throw redirect("/login");
  let identity: LoginIdentity | null;
  try {
    identity = await readLoginIdentity(request, config);
  } catch {
    throw new Response(null, { status: 503, headers: { "Cache-Control": "no-store" } });
  }
  if (identity === null) {
    const returnPath = loginReturnPath(request, config);
    if (request.method === "GET" || request.method === "HEAD") {
      throw data<NeedsAccountData>(
        { needsAccount: true, returnPath },
        { status: 401, headers: { "Cache-Control": "no-store" } },
      );
    }
    if (returnPath === null) throw redirect("/login");
    throw redirect(`/login?returnPath=${encodeURIComponent(returnPath)}`);
  }
  return identity;
}

function loginReturnPath(request: Request, config: WebsiteConfig): string | null {
  const { pathname, searchParams } = new URL(request.url);
  let returnPath = safeReturnPath(pathname, config.publicOrigin);
  if (returnPath === ACCOUNT_SETUP_PATH) {
    const destination = setupReturnPath(
      searchParams.get("returnPath"),
      config.publicOrigin,
    );
    if (destination !== DEFAULT_RETURN_PATH) returnPath = destination;
  }
  return returnPath;
}
