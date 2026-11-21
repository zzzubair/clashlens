import { isSafeReturnPath, MAX_RETURN_PATH_LENGTH } from "./auth.server";
/**
 * Same-origin return-path validation for login redirects.
 *
 * Only a plain same-origin absolute path is accepted, with an optional query
 * string: ASCII path characters, no fragment, no percent-encoding in the path
 * but the %23 of a player tag's #, no dot segments, no backslashes, and no protocol-relative or
 * external forms. The configured public origin is the only base that may be
 * used, so an attacker-supplied value can never redirect the browser off-site. Invalid values yield null and
 * the caller falls back to a safe default path.
 *
 * A page's data request (React Router's `/page.data`, or `/_.data` for the
 * home page) names the page it loads for, so it is mapped back to that page
 * (without React Router's own `_routes` parameter) before validation. Otherwise signing in from an in-app link would return
 * the browser to the raw data instead of the page.
 */

export const DEFAULT_RETURN_PATH = "/account";

/**
 * Validate a same-origin return path against the exact public origin.
 * Returns the validated path, or null when the value is not a safe same-origin
 * path. The caller decides the fallback (normally DEFAULT_RETURN_PATH).
 */
export function safeReturnPath(
  value: string | null | undefined,
  origin: URL,
): string | null {
  if (typeof value !== "string") return null;
  if (value.length === 0 || value.length > MAX_RETURN_PATH_LENGTH) return null;
  value = pagePathOfDataRequest(value);
  if (!isSafeReturnPath(value)) return null;
  let parsed: URL;
  try {
    parsed = new URL(value, origin);
  } catch {
    return null;
  }
  if (parsed.origin !== origin.origin) return null;
  if (parsed.pathname + parsed.search !== value || parsed.hash !== "") return null;
  return value;
}

/** The page part of a return path, without its query string. */
export function returnPathPage(path: string): string {
  const queryStart = path.indexOf("?");
  return queryStart === -1 ? path : path.slice(0, queryStart);
}

function pagePathOfDataRequest(value: string): string {
  const page = returnPathPage(value);
  let path: string;
  if (page.endsWith("/_.data")) path = page.slice(0, -"_.data".length);
  else if (page.endsWith(".data")) path = page.slice(0, -".data".length);
  else return value;
  const query = new URLSearchParams(value.slice(page.length));
  query.delete("_routes");
  const search = query.toString();
  return search === "" ? path : `${path}?${search}`;
}

export const ACCOUNT_SETUP_PATH = "/account/setup";

/**
 * Where account setup sends the user once the account exists: the validated
 * return path, or the default. Setup itself is never a destination.
 */
export function setupReturnPath(value: string | null | undefined, origin: URL): string {
  const path = safeReturnPath(value, origin);
  return path === null || returnPathPage(path) === ACCOUNT_SETUP_PATH
    ? DEFAULT_RETURN_PATH
    : path;
}

/** The account setup page, carrying a validated non-default return path. */
export function accountSetupPath(value: string | null | undefined, origin: URL): string {
  const path = setupReturnPath(value, origin);
  if (path === DEFAULT_RETURN_PATH) return ACCOUNT_SETUP_PATH;
  return `${ACCOUNT_SETUP_PATH}?returnPath=${encodeURIComponent(path)}`;
}
