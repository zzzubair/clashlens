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
  await expect(page.getByRole("link", { name: "Sign in with Google" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Sign in with Discord" })).toBeVisible();
  await expect(
    page.getByRole("main").getByRole("link", { name: "Create account" }),
  ).toHaveAttribute("href", "/signup?returnPath=%2Faccount");
  await page.goto("/signup");
  await expect(
    page.getByRole("heading", { name: "Create your Clash Lens account" }),
  ).toBeVisible();
  await expect(page.getByRole("link", { name: "Sign up with Google" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Sign up with Discord" })).toBeVisible();
  await expectNoSeriousAccessibilityViolations(page);
  await expectNoSeriousAccessibilityViolations(page);
});

test("Discord completes a local sign-in without cloud credentials", async ({
  page,
  context,
}) => {
  await page.goto("/login");
  await page.getByRole("link", { name: "Sign in with Discord" }).click();

  await expect(page).toHaveURL(/\/(?:login\?none=discord|users\/[a-z][a-z0-9_]+)$/);
  if (new URL(page.url()).pathname === "/login") {
    // A sign-in no account uses yet asks before making one, and stays signed
    // out, so pages that need an account still explain why.
    await expect(page.getByRole("heading", { name: "No account found" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Try Google instead" })).toBeVisible();
    await expect(page.getByRole("banner").getByText("Log out")).toHaveCount(0);
    expect(await loginCookie(context)).toBeUndefined();
    await page.goto("/account/watchlists");
    await expect(page.getByText("Sign in to keep them.")).toBeVisible();
    await page.goBack();
    await page.getByRole("link", { name: "Create account with Discord" }).click();
    await expect(page).toHaveURL(/\/account\/setup$/);
    await expect(
      page.getByRole("heading", { name: "Choose your username" }),
    ).toBeVisible();
    expect(await loginCookie(context)).toBeDefined();
  } else {
    await expect(
      page.getByRole("heading", { name: "Clash players", exact: true }),
    ).toBeVisible();
  }
});

test("a sign-up quit at Choose your username can make way for a different sign-in", async ({
  page,
  context,
}) => {
  // A Google sign-in that never gets an account.
  await page.route("**/authorize?*", async (route) => {
    const url = new URL(route.request().url());
    url.searchParams.set("login_hint", "fixture-google-subject-3401");
    await route.continue({ url: url.href });
  });
  const signUp = "/signup?returnPath=%2Faccount%2Fwatchlists";
  const setup = /\/account\/setup\?returnPath=%2Faccount%2Fwatchlists$/;
  // The fake identity only applies once the page's JavaScript has loaded.
  await page.goto(signUp, { waitUntil: "networkidle" });
  await page.getByRole("link", { name: "Sign up with Google" }).click();
  await expect(page).toHaveURL(setup);
  await expect(page.getByRole("heading", { name: "Choose your username" })).toBeVisible();

  // They leave. The next Create account on this browser comes back to their setup.
  await page.goto("/");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(signUp);
  await expect(page).toHaveURL(setup);
  const notYou = page.getByRole("button", { name: "Not you? Use a different sign-in" });
  expect((await notYou.boundingBox())!.height).toBeGreaterThanOrEqual(44);
  await notYou.click();

  await expect(page).toHaveURL(signUp);
  await expect(
    page.getByRole("heading", { name: "Create your Clash Lens account" }),
  ).toBeVisible();
  await expect(page.getByRole("link", { name: "Sign up with Google" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Sign up with Discord" })).toBeVisible();
  expect(await loginCookie(context)).toBeUndefined();
});

test("an account can link, use, and unlink a second sign-in provider", async ({
  page,
}) => {
  await signIn(page);
  await ensureAccount(page, "providerflow", "Provider Flow");
  const accountLabel = (await page.locator(".hero .player-tag").textContent())?.trim();
  expect(accountLabel).toMatch(/^@/);

  await page.goto("/account/settings");
  const discordRow = page
    .locator(".settings-row")
    .filter({ has: page.getByText("Discord", { exact: true }) });
  await discordRow.getByRole("button", { name: "Connect" }).click();
  await expect(page).toHaveURL(/\/account\/settings\?saved=discord-connected$/);
  await expect(page.getByRole("status")).toHaveText("Discord connected.");
  await expect(discordRow.getByRole("button", { name: "Disconnect" })).toBeVisible();

  await page.locator("details.account-menu > summary").click();
  await page.getByRole("button", { name: "Log out" }).click();
  await expect(page).toHaveURL("/?saved=logout");
  await expect(page.getByRole("status")).toHaveText("You're logged out.");
  await signInDiscord(page);
  await expect(page.getByText(accountLabel!, { exact: true })).toBeVisible();

  await page.goto("/account/settings");
  await discordRow.getByRole("button", { name: "Disconnect" }).click();
  await expect(page).toHaveURL(/\/login$/);

  await signIn(page);
  await expect(page.getByText(accountLabel!, { exact: true })).toBeVisible();
  await page.goto("/account/settings");
  await expect(discordRow.getByRole("button", { name: "Connect" })).toBeVisible();
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
  await expect(page).toHaveURL(/\/users\/[a-z][a-z0-9_]+$/);
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
  await expect(page).toHaveURL("/?saved=logout");
  // Wait for the signed-out header so the logout itself has finished.
  await expect(accountMenu).toBeHidden();
  await useLoginIssuedAgo(context, identity, 0, copied);
  await page.goto("/account");
  await expect(page.getByRole("region", { name: "Account needed" })).toBeVisible();

  // A login 29 days old still signs in; one 30 days old does not.
  await useLoginIssuedAgo(context, identity, 29 * DAY_MS);
  await page.goto("/account");
  await expect(page).toHaveURL(/\/users\/[a-z][a-z0-9_]+$/);
  await useLoginIssuedAgo(context, identity, 30 * DAY_MS + 1_000);
  await page.goto("/account");
  await expect(page.getByRole("region", { name: "Account needed" })).toBeVisible();
});
