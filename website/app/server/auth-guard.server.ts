/**
 * Server-only auth guard for account loaders and actions.
 *
 * The guard reads the signed login cookie and yields the validated Google
 * identity for the private Python client. A missing, invalid, or expired
 * cookie (or a login that is disabled or unconfigured) redirects to /login
 * with the current page as a validated same-origin return path. Callers
 * receive the identity for server-side signing only; route loader data must
 * never include the provider or provider subject.
 */

import { redirect } from "react-router";

import type { LoginIdentity } from "./auth-cookies.server";
import { readLoginIdentity } from "./actions.server";
import type { WebsiteConfig } from "./config.server";
import { getWebsiteConfig } from "./config.server";
import { safeReturnPath } from "./return-path.server";

/**
 * Require a valid browser login. Throws a redirect Response to /login when
 * login is disabled, configuration is missing, or the login cookie is
 * missing, malformed, tampered, or expired.
 */
export async function requireLogin(request: Request): Promise<LoginIdentity> {
  let config: WebsiteConfig;
  try {
    config = getWebsiteConfig();
  } catch {
    throw redirect("/login");
  }
  if (!config.loginEnabled) throw redirect("/login");
  const identity = readLoginIdentity(request, config);
  if (identity === null) {
    throw redirect(loginRedirectUrl(request, config));
  }
  return identity;
}

function loginRedirectUrl(request: Request, config: WebsiteConfig): string {
  const pathname = new URL(request.url).pathname;
  const returnPath = safeReturnPath(pathname, config.publicOrigin);
  if (returnPath === null) return "/login";
  return `/login?returnPath=${encodeURIComponent(returnPath)}`;
}
