/**
 * Shared helpers for the account browser tests.
 *
 * These helpers are test-only and deterministic. They own the local fixture
 * reset, the browser sign-in dance against the local OIDC provider, request
 * and console tracking for the privacy assertions, response no-store checks,
 * and the serious/critical axe gate used across the account pages.
 */

import AxeBuilder from "@axe-core/playwright";
import { expect, type APIRequestContext, type Page } from "@playwright/test";

import {
  fixtureApiUrl,
  fixtureKey,
  loginSecretB64,
  oidcClientSecret,
  oidcSubject,
  websiteOrigin,
} from "../../fixtures/test-values";

/** Canonical lowercase UUID used by every idempotency key and group ID. */
export const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/**
 * Values that must never reach the browser: the provider subject, the OAuth
 * client secret, the login-cookie signing secret, and the private-API key.
 */
export const SENSITIVE_FIXTURE_VALUES = [
  oidcSubject,
  oidcClientSecret,
  loginSecretB64,
  fixtureKey,
];

/** Clear all in-memory account state in the local fixture server. */
export async function resetFixture(request: APIRequestContext): Promise<void> {
  const response = await request.post(`${fixtureApiUrl}/__fixture/reset`);
  expect(response.ok(), `fixture reset failed: ${response.status()}`).toBe(true);
}

/**
 * Collect uncaught page errors and console errors; assert none at test end.
 *
 * The suite intentionally loads a few error-status documents (unknown-user
 * pages and the consumed-callback error page), and Chromium logs a generic
 * "Failed to load resource" console error for those. The location of the
 * message identifies the exact URL, so only those intentional error documents
 * are ignored; every other console error still fails the test.
 */
export function trackPageErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() !== "error") return;
    const text = message.text();
    if (text.startsWith("Failed to load resource")) {
      try {
        const pathname = new URL(message.location().url).pathname;
        if (pathname.startsWith("/users/") || pathname === "/auth/google/callback") {
          return;
        }
      } catch {
        // Keep the error when the location cannot be resolved.
      }
    }
    errors.push(`console: ${text}`);
  });
  return errors;
}

export function expectNoPageErrors(errors: string[]): void {
  expect(errors, errors.join("\n")).toEqual([]);
}

/** Collect every request URL the browser makes during the test. */
export function trackRequests(page: Page): string[] {
  const urls: string[] = [];
  page.on("request", (request) => urls.push(request.url()));
  return urls;
}

/**
 * Assert that the browser never called the given TCP port. The browser may
 * reach the website (5173) and, only during sign-in, the OIDC provider (8011);
 * it must never reach the private API (8010) directly.
 */
export function expectNoPortRequests(
  urls: readonly string[],
  port: number,
  label: string,
): void {
  const hits = urls.filter((url) => {
    try {
      return new URL(url).port === String(port);
    } catch {
      return false;
    }
  });
  expect(hits, `${label}: unexpected browser requests to port ${port}`).toEqual([]);
}

/**
 * Record the first Cache-Control header for each same-origin pathname seen
 * while the page is used, then assert every expected path responded no-store.
 * Call the returned assertion function after the navigations complete.
 */
export function trackResponseCacheControl(
  page: Page,
  pathnames: readonly string[],
): () => void {
  const cacheControl = new Map<string, string>();
  page.on("response", (response) => {
    const url = new URL(response.url());
    const pathname = url.pathname.endsWith(".data")
      ? url.pathname.slice(0, -".data".length)
      : url.pathname;
    if (
      url.origin === websiteOrigin &&
      pathnames.includes(pathname) &&
      !cacheControl.has(pathname)
    ) {
      cacheControl.set(pathname, response.headers()["cache-control"] ?? "(missing)");
    }
  });
  return () => {
    for (const pathname of pathnames) {
      expect(cacheControl.get(pathname), `cache-control for ${pathname}`).toBe(
        "no-store",
      );
    }
  };
}

/**
 * Run the real local OIDC flow: /login -> Continue with Google -> provider ->
 * callback. Waits until the browser lands on the account area (/account/setup
 * for a fresh identity, /account for an existing account).
 */
export async function signIn(page: Page): Promise<void> {
  await page.goto("/login");
  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await page.getByRole("link", { name: "Continue with Google" }).click();
  await expect(page).toHaveURL(/\/account(\/setup)?$/);
}

/** Fill and submit the setup form; expects success on /account. */
export async function createAccount(
  page: Page,
  username: string,
  displayName: string,
): Promise<void> {
  await expect(page.getByRole("heading", { name: "Create your account" })).toBeVisible();
  await page.getByLabel("Username").fill(username);
  await page.getByLabel("Display name").fill(displayName);
  await page.getByRole("button", { name: "Create account" }).click();
  await expect(page).toHaveURL(`${websiteOrigin}/account`);
  await expect(page.getByRole("heading", { name: "Your account" })).toBeVisible();
}

/** Fail on any serious or critical axe violation. */
export async function expectNoSeriousAccessibilityViolations(page: Page): Promise<void> {
  const results = await new AxeBuilder({ page }).analyze();
  const seriousViolations = results.violations.filter((violation) =>
    ["critical", "serious"].includes(violation.impact ?? ""),
  );
  expect(seriousViolations, JSON.stringify(seriousViolations, null, 2)).toEqual([]);
}
