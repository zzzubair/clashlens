import AxeBuilder from "@axe-core/playwright";
import { expect, type Page } from "@playwright/test";

import { LOGIN_COOKIE_NAME } from "../../../app/server/auth-cookies.server";

/** Collect browser failures so a page cannot appear healthy while JavaScript failed. */
export function trackPageErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(`console: ${message.text()}`);
  });
  return errors;
}

export function expectNoPageErrors(errors: string[]): void {
  expect(errors, errors.join("\n")).toEqual([]);
}

/** Confirm server-only traffic did not escape into the browser. */
export function trackRequests(page: Page): string[] {
  const urls: string[] = [];
  page.on("request", (request) => urls.push(request.url()));
  return urls;
}

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
 * Sign in with the loopback provider. A new identity has no account yet, so
 * it answers "No account found", still signed out, and Create account signs
 * up with the same provider and goes on to choosing a username.
 *
 * Tests pick a fake identity by rewriting the request to the provider, which
 * Playwright can only do when the page's JavaScript sends the browser straight
 * there. Clicked before it loads, the link follows a redirect instead and
 * signs in as the default identity, so each click waits for the page first.
 */
async function signInWith(page: Page, provider: "Google" | "Discord"): Promise<void> {
  await page.goto("/login", { waitUntil: "networkidle" });
  await page.getByRole("link", { name: `Sign in with ${provider}` }).click();
  await expect(page).toHaveURL(/\/(?:login\?none=|users\/[a-z][a-z0-9_]+$)/);
  if (new URL(page.url()).pathname === "/login") {
    await expect(page.getByRole("heading", { name: "No account found" })).toBeVisible();
    const cookies = await page.context().cookies();
    expect(cookies.map(({ name }) => name)).not.toContain(LOGIN_COOKIE_NAME);
    await page.waitForLoadState("networkidle");
    await page.getByRole("link", { name: `Create account with ${provider}` }).click();
    await expect(page).toHaveURL(/\/account\/setup$/);
  }
}

/** Complete the local Google sign-in flow against the loopback provider. */
export async function signIn(page: Page): Promise<void> {
  await signInWith(page, "Google");
}

/** Complete the local Discord sign-in flow against the loopback provider. */
export async function signInDiscord(page: Page): Promise<void> {
  await signInWith(page, "Discord");
}

/**
 * Create the account only when this identity has not already been used. A
 * new account is asked to link a player first; this goes on to the account.
 */
export async function ensureAccount(
  page: Page,
  username: string,
  displayName: string,
): Promise<void> {
  if (new URL(page.url()).pathname === "/account/setup") {
    await page.getByLabel("Username", { exact: true }).fill(username);
    await page.getByLabel("Display name").fill(displayName);
    await page.getByRole("button", { name: "Create account" }).click();
    await expect(page).toHaveURL(/\/account\/verify-player$/);
    await page.goto("/account");
  }
  await expect(page).toHaveURL(/\/users\/[a-z][a-z0-9_]+$/);
  await expect(
    page.getByRole("heading", { name: "Clash players", exact: true }),
  ).toBeVisible();
}

// When it was this test run's last attempt to link a player.
let lastLinkAttempt = 0;

/**
 * Link a fake player on the open Link page, which the fixture verifies by its
 * token, and wait to land on `done`. The game's verify call has a small budget
 * a second that the collector shares, so a busy second is tried again. Every
 * attempt counts toward six a minute per account and per address, so attempts
 * across all tests stay 11 seconds apart.
 */
export async function linkPlayer(
  page: Page,
  tag: string,
  done: RegExp | string = /\?linked=/,
): Promise<void> {
  await expect(async () => {
    await page.waitForTimeout(Math.max(0, lastLinkAttempt + 11_000 - Date.now()));
    await page.getByLabel("Player tag").fill(tag);
    await page.getByLabel("API token", { exact: true }).fill(`VERIFY-${tag.slice(1)}`);
    lastLinkAttempt = Date.now();
    await page.getByRole("button", { name: "Link player" }).click();
    await expect(page).toHaveURL(done, { timeout: 3_000 });
  }).toPass({ timeout: 90_000 });
}

export async function expectNoSeriousAccessibilityViolations(page: Page): Promise<void> {
  const results = await new AxeBuilder({ page }).analyze();
  const seriousViolations = results.violations.filter((violation) =>
    ["critical", "serious"].includes(violation.impact ?? ""),
  );
  expect(seriousViolations, JSON.stringify(seriousViolations, null, 2)).toEqual([]);
}

/**
 * "[Create account] Already have one? [Sign in]" on this page: the question
 * always sits beside the Sign in it introduces, every button is at least
 * 44 px tall, and on a phone the pair wraps below Create account as a whole.
 */
export async function expectAccountButtonsRead(
  page: Page,
  phone: boolean,
): Promise<void> {
  const row = page.locator(".account-buttons").filter({ hasText: "Already have one?" });
  const create = (await row.getByRole("link", { name: "Create account" }).boundingBox())!;
  const question = (await row.getByText("Already have one?").boundingBox())!;
  const signIn = (await row
    .getByRole("link", { name: "Sign in", exact: true })
    .boundingBox())!;
  const middle = (box: { y: number; height: number }) => box.y + box.height / 2;
  expect(Math.abs(middle(question) - middle(signIn))).toBeLessThanOrEqual(4);
  expect(question.x).toBeLessThan(signIn.x);
  expect(Math.min(create.height, signIn.height)).toBeGreaterThanOrEqual(44);
  if (phone) expect(middle(signIn)).toBeGreaterThan(create.y + create.height);
}
