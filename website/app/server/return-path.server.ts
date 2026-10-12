/**
 * Same-origin return-path validation for login redirects.
 *
 * Only a plain same-origin absolute path is accepted: ASCII path characters,
 * no query string, no fragment, no percent-encoding, no dot segments, no
 * backslashes, and no protocol-relative or external forms. The configured
 * public origin is the only base that may be used, so an attacker-supplied
 * value can never redirect the browser off-site. Invalid values yield null and
 * the caller falls back to a safe default path.
 *
 * A page's data request (React Router's `/page.data`, or `/_.data` for the
 * home page) names the page it loads for, so it is mapped back to that page
 * before validation. Otherwise signing in from an in-app link would return
 * the browser to the raw data instead of the page.
 */

export const MAX_RETURN_PATH_LENGTH = 200;
export const DEFAULT_RETURN_PATH = "/account";

const RETURN_PATH_CHARS = /^[A-Za-z0-9._~/-]+$/;

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
  if (!value.startsWith("/") || value.startsWith("//")) return null;
  if (!RETURN_PATH_CHARS.test(value)) return null;
  if (value.split("/").some((segment) => segment === "." || segment === "..")) {
    return null;
  }
  let parsed: URL;
  try {
    parsed = new URL(value, origin);
  } catch {
    return null;
  }
  if (parsed.origin !== origin.origin) return null;
  if (parsed.pathname !== value || parsed.search !== "" || parsed.hash !== "") {
    return null;
  }
  return value;
}

function pagePathOfDataRequest(path: string): string {
  if (path.endsWith("/_.data")) return path.slice(0, -"_.data".length);
  if (path.endsWith(".data")) return path.slice(0, -".data".length);
  return path;
}

export const ACCOUNT_SETUP_PATH = "/account/setup";

/**
 * Where account setup sends the user once the account exists: the validated
 * return path, or the default. Setup itself is never a destination.
 */
export function setupReturnPath(value: string | null | undefined, origin: URL): string {
  const path = safeReturnPath(value, origin);
  return path === null || path === ACCOUNT_SETUP_PATH ? DEFAULT_RETURN_PATH : path;
}

/** The account setup page, carrying a validated non-default return path. */
export function accountSetupPath(value: string | null | undefined, origin: URL): string {
  const path = setupReturnPath(value, origin);
  if (path === DEFAULT_RETURN_PATH) return ACCOUNT_SETUP_PATH;
  return `${ACCOUNT_SETUP_PATH}?returnPath=${encodeURIComponent(path)}`;
}
