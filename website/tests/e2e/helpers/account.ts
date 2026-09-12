import AxeBuilder from "@axe-core/playwright";
import { expect, type Page } from "@playwright/test";

import { websiteOrigin } from "../../fixtures/test-values";

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

/** Complete the local Google sign-in flow against the loopback provider. */
export async function signIn(page: Page): Promise<void> {
  await page.goto("/login");
  await page.getByRole("link", { name: "Continue with Google" }).click();
  await expect(page).toHaveURL(/\/account(\/setup)?$/);
}

/** Complete the local Discord sign-in flow against the loopback provider. */
export async function signInDiscord(page: Page): Promise<void> {
  await page.goto("/login");
  await page.getByRole("link", { name: "Continue with Discord" }).click();
  await expect(page).toHaveURL(/\/account(\/setup)?$/);
}

/** Create the account only when this identity has not already been used. */
export async function ensureAccount(
  page: Page,
  username: string,
  displayName: string,
): Promise<void> {
  if (new URL(page.url()).pathname === "/account/setup") {
    await page.getByLabel("Username").fill(username);
    await page.getByLabel("Display name").fill(displayName);
    await page.getByRole("button", { name: "Create account" }).click();
  }
  await expect(page).toHaveURL(`${websiteOrigin}/account`);
  await expect(page.getByRole("heading", { name: "Your account" })).toBeVisible();
}

export async function expectNoSeriousAccessibilityViolations(page: Page): Promise<void> {
  const results = await new AxeBuilder({ page }).analyze();
  const seriousViolations = results.violations.filter((violation) =>
    ["critical", "serious"].includes(violation.impact ?? ""),
  );
  expect(seriousViolations, JSON.stringify(seriousViolations, null, 2)).toEqual([]);
}
