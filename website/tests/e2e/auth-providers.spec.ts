import { expect, test, type BrowserContext } from "@playwright/test";

import {
  LOGIN_COOKIE_NAME,
  createLoginCookieValue,
  parseLoginCookieValue,
  type LoginIdentity,
} from "../../app/server/auth-cookies.server";
import { devLoginSecret, websiteOrigin } from "../fixtures/test-values";
import {
  ensureAccount,
  expectNoSeriousAccessibilityViolations,
  signIn,
  signInDiscord,
} from "./helpers/account";

const DAY_MS = 86_400_000;

async function loginCookie(context: BrowserContext) {
  const cookies = await context.cookies(websiteOrigin);
  return cookies.find((cookie) => cookie.name === LOGIN_COOKIE_NAME);
}

/** Replace the browser's login cookie with one for `identity` issued `ageMs` ago. */
async function useLoginIssuedAgo(
  context: BrowserContext,
  identity: LoginIdentity,
  ageMs: number,
  value = createLoginCookieValue(identity, devLoginSecret, Date.now() - ageMs),
): Promise<string> {
  await context.addCookies([
    {
      name: LOGIN_COOKIE_NAME,
      value,
      url: websiteOrigin,
      httpOnly: true,
      sameSite: "Lax",
    },
  ]);
  return value;
}

test("loopback Google and Discord sign-in choices are available", async ({ page }) => {
  await page.goto("/login");

  await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Continue with Google" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Continue with Discord" })).toBeVisible();
  await expectNoSeriousAccessibilityViolations(page);
});

test("Discord completes a local sign-in without cloud credentials", async ({ page }) => {
  await page.goto("/login");
  await page.getByRole("link", { name: "Continue with Discord" }).click();

  await expect(page).toHaveURL(/\/(?:account\/setup|users\/[a-z][a-z0-9_]+)$/);
  await expect(
    page.getByRole("heading", { name: /Create your account|Linked accounts/ }),
  ).toBeVisible();
});

test("an account can link, use, and unlink a second sign-in provider", async ({
  page,
}) => {
  await signIn(page);
  await ensureAccount(page, "providerflow", "Provider Flow");
  const accountLabel = (await page.locator(".hero .player-tag").textContent())?.trim();
  expect(accountLabel).toMatch(/^@/);

  await page.goto("/account/providers");
  const discordRow = page.locator("li").filter({ hasText: "Discord" });
  await discordRow.getByRole("button", { name: "Link" }).click();
  await expect(page).toHaveURL(/\/account\/providers$/);
  await expect(discordRow.getByRole("button", { name: "Unlink" })).toBeVisible();

  await page.locator("details.account-menu > summary").click();
  await page.getByRole("button", { name: "Log out" }).click();
  await expect(page).toHaveURL("/");
  await signInDiscord(page);
  await expect(page.getByText(accountLabel!, { exact: true })).toBeVisible();

  await page.goto("/account/providers");
  await discordRow.getByRole("button", { name: "Unlink" }).click();
  await expect(page).toHaveURL(/\/login$/);

  await signIn(page);
  await expect(page.getByText(accountLabel!, { exact: true })).toBeVisible();
  await page.goto("/account/providers");
  await expect(discordRow.getByRole("button", { name: "Link" })).toBeVisible();
});

test("a signed-in visit restarts the 30 days, and a 30-day-old login no longer signs in", async ({
  page,
  context,
}) => {
  await signIn(page);
  await ensureAccount(page, "staysignedin", "Stays Signed In");
  const signedIn = await loginCookie(context);
  const session = parseLoginCookieValue(
    signedIn?.value,
    devLoginSecret,
    Math.floor(Date.now() / 1000),
  );
  expect(session).not.toBeNull();
  const identity = {
    provider: session!.provider,
    providerSubject: session!.providerSubject,
  };
  const accountMenu = page.locator("details.account-menu > summary");

  // A cookie from two hours ago is replaced on the next visit by one that
  // lasts 30 days from that visit.
  const copied = await useLoginIssuedAgo(context, identity, 2 * 3_600_000);
  const visitedAt = Date.now();
  await page.goto("/account");
  await expect(page).not.toHaveURL(/\/login/);
  const renewed = await loginCookie(context);
  expect(renewed?.value).not.toBe(copied);
  expect(renewed?.httpOnly).toBe(true);
  expect(renewed?.sameSite).toBe("Lax");
  expect(renewed!.expires * 1000).toBeGreaterThan(visitedAt + 30 * DAY_MS - 60_000);
  expect(renewed!.expires * 1000).toBeLessThan(Date.now() + 30 * DAY_MS + 60_000);
  const renewedSession = parseLoginCookieValue(
    renewed!.value,
    devLoginSecret,
    Math.floor(Date.now() / 1000),
  );
  expect(renewedSession?.issuedAtMs).toBeGreaterThanOrEqual(visitedAt - 1_000);

  // Logging out also ends the copy from before the renewal.
  await accountMenu.click();
  await page.getByRole("button", { name: "Log out" }).click();
  await expect(page).toHaveURL("/");
  // Wait for the signed-out header so the logout itself has finished.
  await expect(accountMenu).toBeHidden();
  await useLoginIssuedAgo(context, identity, 0, copied);
  await page.goto("/account");
  await expect(page).toHaveURL(/\/login\?returnPath=%2Faccount$/);

  // A login 29 days old still signs in; one 30 days old does not.
  await useLoginIssuedAgo(context, identity, 29 * DAY_MS);
  await page.goto("/account");
  await expect(page).not.toHaveURL(/\/login/);
  await useLoginIssuedAgo(context, identity, 30 * DAY_MS + 1_000);
  await page.goto("/account");
  await expect(page).toHaveURL(/\/login\?returnPath=%2Faccount$/);
});
