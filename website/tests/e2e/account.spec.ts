/**
 * Deterministic browser coverage for the Google account UI (issue #36).
 *
 * The suite runs serially against the local fixture servers (private API on
 * 8010, OIDC provider on 8011, website on 5173) and resets the fixture
 * account state once at the start. Every test signs in through the real local
 * OIDC flow, so the cookie, privacy, and mutation behavior is exercised
 * end to end through the browser only.
 *
 * Test identities are deliberate: the first identity signs in as lenskeeper /
 * "Lens Keeper" and is renamed to lensscout / "Lens Scout" by the profile
 * edit. Groups use "Roster" -> "Main roster", saved players use bare tag
 * "2pl" (#2PL), and player verification uses the fixture one-time token
 * FIXTURE-VERIFY-2PP for tag #2PP.
 */

import { expect, test } from "@playwright/test";

import {
  createAccount,
  expectNoPageErrors,
  expectNoPortRequests,
  expectNoSeriousAccessibilityViolations,
  resetFixture,
  SENSITIVE_FIXTURE_VALUES,
  signIn,
  trackPageErrors,
  trackRequests,
  trackResponseCacheControl,
  UUID_PATTERN,
} from "./helpers/account";
import {
  loginSecretB64,
  oidcClientId,
  oidcIssuerUrl,
  oidcSubject,
  websiteOrigin,
} from "../fixtures/test-values";

const VERIFY_TOKEN = "FIXTURE-VERIFY-2PP";
const WRONG_TOKEN = "FIXTURE-VERIFY-NOPE";
const LOGIN_COOKIE = "clashlens_login";
const OAUTH_COOKIE = "clashlens_oauth";
const LOGIN_COOKIE_LIFETIME_SECONDS = 24 * 60 * 60;

test.describe("account flows", () => {
  test.describe.configure({ mode: "serial" });

  test.beforeAll(async ({ request }) => {
    await resetFixture(request);
  });

  test("public pages work logged out and the browser never calls private ports", async ({
    page,
  }) => {
    const urls = trackRequests(page);
    const errors = trackPageErrors(page);

    await page.goto("/");
    await expect(page.getByRole("link", { name: "Log in" })).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "Clash Lens", exact: true }),
    ).toBeVisible();

    await page.goto("/players/%232PP");
    await expect(page.getByRole("heading", { name: "Nova" })).toBeVisible();

    await page.goto("/leaderboards/tracked");
    await expect(
      page.getByRole("heading", { name: "Live leaderboard", exact: true }),
    ).toBeVisible();

    await page.goto("/users/lensscout");
    await expect(page.getByRole("heading", { name: "User not found" })).toBeVisible();

    expectNoPortRequests(urls, 8010, "public pages");
    expectNoPortRequests(urls, 8011, "public pages");
    expectNoPageErrors(errors);
  });

  test("the local OIDC sign-in lands on setup with a clean URL and no leaked secrets", async ({
    page,
    context,
  }) => {
    const urls = trackRequests(page);
    const errors = trackPageErrors(page);
    const oidcUrls: string[] = [];
    const callbackUrls: string[] = [];
    page.on("request", (request) => {
      const url = request.url();
      if (url.startsWith(oidcIssuerUrl)) oidcUrls.push(url);
      if (new URL(url).pathname === "/auth/google/callback") callbackUrls.push(url);
    });
    const assertNoStore = trackResponseCacheControl(page, [
      "/login",
      "/auth/google/callback",
      "/account/setup",
    ]);

    await page.goto("/login");
    const startResponse = await page.request.get("/auth/google", {
      maxRedirects: 0,
    });
    expect(startResponse.status()).toBe(302);
    expect(startResponse.headers()["cache-control"]).toBe("no-store");
    await page.getByRole("link", { name: "Continue with Google" }).click();

    // First identity lands on account setup, not an existing account.
    await expect(page).toHaveURL(`${websiteOrigin}/account/setup`);
    await expect(
      page.getByRole("heading", { name: "Create your account" }),
    ).toBeVisible();

    // The browser never calls the private API port directly.
    expectNoPortRequests(urls, 8010, "sign-in");
    // The only provider request the browser makes is the authorization redirect.
    expect(oidcUrls).toHaveLength(1);
    const authorize = new URL(oidcUrls[0]);
    expect(authorize.pathname).toBe("/authorize");
    expect(authorize.searchParams.get("client_id")).toBe(oidcClientId);
    expect(authorize.searchParams.get("response_type")).toBe("code");
    expect(authorize.searchParams.get("scope")).toBe("openid");
    expect(authorize.searchParams.get("redirect_uri")).toBe(
      `${websiteOrigin}/auth/google/callback`,
    );
    expect(authorize.searchParams.get("code_challenge_method")).toBe("S256");
    const state = authorize.searchParams.get("state") ?? "";
    const nonce = authorize.searchParams.get("nonce") ?? "";
    const challenge = authorize.searchParams.get("code_challenge") ?? "";
    expect(state).toMatch(/^[A-Za-z0-9_-]{16,128}$/);
    expect(nonce).toMatch(/^[A-Za-z0-9_-]{16,128}$/);
    expect(challenge).toMatch(/^[A-Za-z0-9_-]{43,128}$/);
    // The PKCE verifier and client secret are never sent to the provider.
    expect(authorize.searchParams.get("code_verifier")).toBeNull();
    expect(authorize.searchParams.get("client_secret")).toBeNull();
    expect(authorize.search).not.toContain(oidcSubject);
    expect(authorize.search).not.toContain(loginSecretB64);

    // The callback carried a one-time code plus the echoed state, then the
    // browser landed on a query-free URL.
    expect(callbackUrls).toHaveLength(1);
    const callback = new URL(callbackUrls[0]);
    expect(callback.searchParams.get("code")).toMatch(/^[A-Za-z0-9_-]{20,}$/);
    expect(callback.searchParams.get("state")).toBe(state);
    expect(page.url()).toBe(`${websiteOrigin}/account/setup`);

    // No secret or one-time transaction value survives in the URL, HTML, or
    // browser storage.
    const html = await page.content();
    for (const value of [...SENSITIVE_FIXTURE_VALUES, state, nonce, challenge]) {
      expect(page.url(), `URL must not contain ${value}`).not.toContain(value);
      expect(html, `HTML must not contain ${value}`).not.toContain(value);
    }
    for (const label of ["access_token", "id_token", "code_verifier"]) {
      expect(html, `HTML must not contain ${label}`).not.toContain(label);
    }
    const storageDump = await page.evaluate(() => {
      const entries: string[] = [];
      for (const area of [window.localStorage, window.sessionStorage]) {
        for (let index = 0; index < area.length; index += 1) {
          const key = area.key(index);
          if (key !== null) entries.push(key, area.getItem(key) ?? "");
        }
      }
      return entries.join("\n");
    });
    for (const value of [...SENSITIVE_FIXTURE_VALUES, state, nonce, challenge]) {
      expect(storageDump, `storage must not contain ${value}`).not.toContain(value);
    }

    // The OAuth transaction cookie is consumed and cleared after the callback.
    const cookies = await context.cookies();
    const oauthCookie = cookies.find((cookie) => cookie.name === OAUTH_COOKIE);
    expect(oauthCookie, "OAuth transaction cookie must be cleared").toBeUndefined();

    // The login cookie carries the fixed attributes and a 24-hour lifetime.
    const loginCookie = cookies.find((cookie) => cookie.name === LOGIN_COOKIE);
    expect(loginCookie).toBeDefined();
    expect(loginCookie?.httpOnly).toBe(true);
    expect(loginCookie?.sameSite).toBe("Lax");
    expect(loginCookie?.path).toBe("/");
    expect(loginCookie?.secure).toBe(false);
    const lifetime = (loginCookie?.expires ?? 0) - Date.now() / 1000;
    expect(lifetime).toBeGreaterThan(LOGIN_COOKIE_LIFETIME_SECONDS - 120);
    expect(lifetime).toBeLessThan(LOGIN_COOKIE_LIFETIME_SECONDS + 120);

    assertNoStore();
    expectNoPageErrors(errors);
  });

  test("a consumed callback code cannot be replayed", async ({ page }) => {
    let callbackUrl = "";
    page.on("request", (request) => {
      const url = new URL(request.url());
      if (url.pathname === "/auth/google/callback") callbackUrl = request.url();
    });
    await signIn(page);
    await expect(page).toHaveURL(`${websiteOrigin}/account/setup`);
    expect(callbackUrl).toContain("code=");

    const errors = trackPageErrors(page);
    await page.goto(callbackUrl);
    await expect(
      page.getByRole("heading", { name: "Sign-in could not be completed" }),
    ).toBeVisible();
    await expect(page.getByRole("alert")).toContainText("Try signing in again");

    // The dead code and state are never echoed into the rendered page.
    const deadCallback = new URL(callbackUrl);
    const html = await page.content();
    expect(html).not.toContain(deadCallback.searchParams.get("code") ?? "");
    expect(html).not.toContain(deadCallback.searchParams.get("state") ?? "");
    expect(html).not.toContain(oidcSubject);
    expectNoPageErrors(errors);
  });

  test("setup rejects reserved names before account creation and rotates its idempotency key", async ({
    page,
    browser,
    context,
  }) => {
    const urls = trackRequests(page);
    await signIn(page);
    await expect(page).toHaveURL(`${websiteOrigin}/account/setup`);
    await expect(page.getByLabel("Username")).toBeVisible();
    await expect(page.getByLabel("Display name")).toBeVisible();

    // The setup form carries a canonical idempotency UUID.
    const setupKey = await page
      .getByRole("region", { name: "Account setup form" })
      .locator('input[name="idempotencyKey"]')
      .inputValue();
    expect(setupKey).toMatch(UUID_PATTERN);

    // A reserved username is rejected client-side before any network POST.
    await page.getByLabel("Username").fill("login");
    await page.getByLabel("Display name").fill("Lens Keeper");
    await page.getByRole("button", { name: "Create account" }).click();
    await expect(page.getByRole("alert")).toContainText("Username");
    await expect(page).toHaveURL(`${websiteOrigin}/account/setup`);
    expectNoPortRequests(urls, 8010, "reserved-name rejection");

    // With JavaScript disabled the server action still rejects the reserved
    // name with its own strict message and rotates the idempotency key.
    const cookies = await context.cookies();
    const noJsContext = await browser.newContext({ javaScriptEnabled: false });
    await noJsContext.addCookies(cookies);
    const noJsPage = await noJsContext.newPage();
    await noJsPage.goto("/account/setup");
    const keyBefore = await noJsPage
      .getByRole("region", { name: "Account setup form" })
      .locator('input[name="idempotencyKey"]')
      .inputValue();
    expect(keyBefore).toMatch(UUID_PATTERN);
    await noJsPage.getByLabel("Username").fill("login");
    await noJsPage.getByLabel("Display name").fill("Lens Keeper");
    await noJsPage.getByRole("button", { name: "Create account" }).click();
    await expect(noJsPage.getByRole("alert")).toContainText(
      "That username is reserved. Choose a different one.",
    );
    await expect(noJsPage).toHaveURL(`${websiteOrigin}/account/setup`);
    await expect(noJsPage.getByLabel("Username")).toHaveValue("login");
    await expect(noJsPage.getByLabel("Display name")).toHaveValue("Lens Keeper");
    const keyAfter = await noJsPage
      .getByRole("region", { name: "Account setup form" })
      .locator('input[name="idempotencyKey"]')
      .inputValue();
    expect(keyAfter).toMatch(UUID_PATTERN);
    expect(keyAfter).not.toBe(keyBefore);
    await noJsContext.close();
  });

  test("setup succeeds, the login cookie never slides, and profile edits publish canonical data", async ({
    page,
    browser,
    context,
  }) => {
    const errors = trackPageErrors(page);
    const assertNoStore = trackResponseCacheControl(page, [
      "/account/setup",
      "/account",
      "/account/profile",
      "/users/lensscout",
      "/users/lenskeeper",
    ]);

    await signIn(page);
    await expect(page).toHaveURL(`${websiteOrigin}/account/setup`);

    const loginBefore = (await context.cookies()).find(
      (cookie) => cookie.name === LOGIN_COOKIE,
    );
    expect(loginBefore).toBeDefined();
    const valueBefore = loginBefore?.value;
    const expiresBefore = loginBefore?.expires;

    await createAccount(page, "lenskeeper", "Lens Keeper");
    await expect(
      page.getByRole("link", { name: "Account Lens Keeper", exact: true }),
    ).toBeVisible();
    await expect(page.getByRole("button", { name: "Log out" })).toBeVisible();
    await expect(
      page.locator('form[action="/logout"] input[name="idempotencyKey"]'),
    ).toHaveValue(UUID_PATTERN);
    await expect(page.locator(".lede")).toContainText("Lens Keeper");
    await expect(page.locator(".lede")).toContainText("@lenskeeper");

    // Navigation does not slide the cookie: value and expiry are unchanged.
    const loginAfterNavigation = (await context.cookies()).find(
      (cookie) => cookie.name === LOGIN_COOKIE,
    );
    expect(loginAfterNavigation?.value).toBe(valueBefore);
    expect(loginAfterNavigation?.expires).toBe(expiresBefore);

    // A signed mutation (profile update) does not slide the cookie either.
    await page.getByRole("link", { name: "Edit profile" }).click();
    await expect(page.getByRole("heading", { name: "Edit profile" })).toBeVisible();
    await page.getByLabel("Username").fill("lensscout");
    await page.getByLabel("Display name").fill("Lens Scout");
    await page.getByRole("button", { name: "Save changes" }).click();
    await expect(page.getByRole("heading", { name: "Edit profile" })).toBeVisible();
    const loginAfterMutation = (await context.cookies()).find(
      (cookie) => cookie.name === LOGIN_COOKIE,
    );
    expect(loginAfterMutation?.value).toBe(valueBefore);
    expect(loginAfterMutation?.expires).toBe(expiresBefore);

    // Server-rendered validation keeps non-secret profile values without JavaScript.
    const noJsContext = await browser.newContext({ javaScriptEnabled: false });
    await noJsContext.addCookies(await context.cookies());
    const noJsPage = await noJsContext.newPage();
    await noJsPage.goto("/account/profile");
    await noJsPage.getByLabel("Username").fill("login");
    await noJsPage.getByLabel("Display name").fill("Kept Name");
    await noJsPage.getByRole("button", { name: "Save changes" }).click();
    await expect(noJsPage.getByRole("alert")).toContainText(
      "That username is reserved. Choose a different one.",
    );
    await expect(noJsPage.getByLabel("Username")).toHaveValue("login");
    await expect(noJsPage.getByLabel("Display name")).toHaveValue("Kept Name");
    await noJsContext.close();

    // The account page reflects the renamed canonical values.
    await page.goto("/account");
    await expect(page.locator(".lede")).toContainText("Lens Scout");
    await expect(page.locator(".lede")).toContainText("@lensscout");

    // The public user page shows the canonical public data only.
    await page.goto("/users/lensscout");
    await expect(page.getByRole("heading", { name: "Lens Scout" })).toBeVisible();
    await expect(page.getByText("@lensscout")).toBeVisible();
    await expect(page.getByText("No verified players")).toBeVisible();

    // The old username is released and resolves to a clean 404.
    await page.goto("/users/lenskeeper");
    await expect(page.getByRole("heading", { name: "User not found" })).toBeVisible();

    assertNoStore();
    expectNoPageErrors(errors);
  });

  test("saved players normalize bare tags, list the player, and remove cleanly", async ({
    page,
  }) => {
    const urls = trackRequests(page);
    const errors = trackPageErrors(page);
    const assertNoStore = trackResponseCacheControl(page, [
      "/account/saved-players",
      "/account",
    ]);

    await signIn(page);
    await page.goto("/account/saved-players");
    await expect(
      page.getByRole("heading", { name: "Saved players", exact: true }),
    ).toBeVisible();

    const addKey = await page
      .locator("section[aria-label='Add a saved player'] input[name='idempotencyKey']")
      .inputValue();
    expect(addKey).toMatch(UUID_PATTERN);

    // A bare tag is normalized to the canonical player tag.
    await page.getByLabel("Player tag").fill("2pl");
    await page.getByRole("button", { name: "Save player" }).click();
    await expect(page.getByRole("link", { name: "Mira" })).toBeVisible();
    await expect(page.getByText("#2PL", { exact: true })).toBeVisible();
    await expect(page.getByRole("link", { name: "Mira" })).toHaveAttribute(
      "href",
      "/players/%232PL",
    );

    // An invalid tag gets a safe field error.
    await page.getByLabel("Player tag").fill("not-a-tag");
    await page.getByRole("button", { name: "Save player" }).click();
    await expect(page.getByRole("alert")).toContainText("Enter a valid player tag.");
    expect(errors).toEqual([
      "console: Failed to load resource: the server responded with a status of 400 (Bad Request)",
    ]);
    errors.length = 0;
    expectNoPortRequests(urls, 8010, "saved players");

    // The account overview lists the saved player.
    await page.goto("/account");
    await expect(
      page.getByRole("heading", { name: "Saved players", exact: true }),
    ).toBeVisible();
    await expect(page.getByRole("link", { name: "Mira" })).toBeVisible();

    // The saved player removes cleanly.
    await page.goto("/account/saved-players");
    await page.getByRole("button", { name: "Remove" }).click();
    await expect(page.getByText("No saved players yet")).toBeVisible();

    assertNoStore();
    expectNoPageErrors(errors);
  });

  test("player verification uses a one-time token that is cleared and never exposed", async ({
    page,
  }) => {
    const urls = trackRequests(page);
    const errors = trackPageErrors(page);
    const consoleMessages: string[] = [];
    page.on("console", (message) => consoleMessages.push(message.text()));
    const assertNoStore = trackResponseCacheControl(page, [
      "/account/verify-player",
      "/account",
    ]);

    await signIn(page);
    await page.goto("/account/verify-player");
    await expect(page.getByRole("heading", { name: "Verify a player" })).toBeVisible();

    // An invalid token is reported safely and the idempotency key rotates.
    await page.getByLabel("Player tag").fill("#2pp");
    await page.getByLabel("One-time verification token").fill(WRONG_TOKEN);
    const keyBefore = await page
      .getByRole("region", { name: "Player verification form" })
      .locator('input[name="idempotencyKey"]')
      .inputValue();
    await page.getByRole("button", { name: "Verify player" }).click();
    await expect(page.getByRole("status")).toContainText(
      "The one-time token is invalid or expired",
    );
    await expect(page.getByLabel("One-time verification token")).toHaveValue("");
    const keyAfter = await page
      .getByRole("region", { name: "Player verification form" })
      .locator('input[name="idempotencyKey"]')
      .inputValue();
    expect(keyAfter).toMatch(UUID_PATTERN);
    expect(keyAfter).not.toBe(keyBefore);

    // The valid fixture token links the player; the token input is cleared.
    await page.getByLabel("Player tag").fill("#2PP");
    await page.getByLabel("One-time verification token").fill(VERIFY_TOKEN);
    await page.getByRole("button", { name: "Verify player" }).click();
    await expect(page.getByRole("status")).toContainText(
      "The player was verified and linked to your account.",
    );
    await expect(page.getByLabel("One-time verification token")).toHaveValue("");
    expect(page.url()).not.toContain(VERIFY_TOKEN);
    expect(page.url()).not.toContain(WRONG_TOKEN);

    // The tokens never appear in the rendered page or any console message.
    const html = await page.content();
    expect(html).not.toContain(VERIFY_TOKEN);
    expect(html).not.toContain(WRONG_TOKEN);
    expect(consoleMessages.join("\n")).not.toContain(VERIFY_TOKEN);

    // Re-submitting the same token reports already-linked.
    await page.getByLabel("Player tag").fill("#2PP");
    await page.getByLabel("One-time verification token").fill(VERIFY_TOKEN);
    await page.getByRole("button", { name: "Verify player" }).click();
    await expect(page.getByRole("status")).toContainText(
      "This player is already linked to your account.",
    );

    // The account overview lists the verified player.
    await page.goto("/account");
    await expect(
      page.getByRole("heading", { name: "Verified players", exact: true }),
    ).toBeVisible();
    await expect(page.getByRole("link", { name: "Nova" })).toBeVisible();

    assertNoStore();
    expectNoPortRequests(urls, 8010, "verify player");
    expectNoPageErrors(errors);
  });

  test("private groups support create, rename, confirmed delete, and canonical ids", async ({
    page,
  }) => {
    const urls = trackRequests(page);
    const errors = trackPageErrors(page);
    const assertNoStore = trackResponseCacheControl(page, [
      "/account/groups",
      "/account",
    ]);

    await signIn(page);
    await page.goto("/account/groups");
    await expect(
      page.getByRole("heading", { name: "Private groups", exact: true }),
    ).toBeVisible();

    // Create a group from a raw tag list; ids and keys are canonical UUIDs.
    await page.getByLabel("Group name").fill("Roster");
    await page.getByLabel("Player tags").fill("#2pp, #2py");
    await page.getByRole("button", { name: "Create group" }).click();
    await expect(
      page.getByRole("heading", { name: "Roster", exact: true }),
    ).toBeVisible();
    await expect(page.getByRole("link", { name: "#2PP", exact: true })).toBeVisible();
    await expect(page.getByRole("link", { name: "#2PY", exact: true })).toBeVisible();
    const groupId = await page.locator('input[name="groupId"]').first().inputValue();
    expect(groupId).toMatch(UUID_PATTERN);
    expectNoPortRequests(urls, 8010, "group creation");

    // A duplicate group name is rejected with a safe field error.
    const createForm = page.locator("section[aria-label='Create a group']");
    await createForm.getByLabel("Group name").fill("Roster");
    await createForm.getByLabel("Player tags").fill("#2PP");
    await createForm.getByRole("button", { name: "Create group" }).click();
    await expect(page.getByRole("alert")).toContainText(
      "A group with this name already exists.",
    );
    expect(errors).toEqual([
      "console: Failed to load resource: the server responded with a status of 409 (Conflict)",
    ]);
    errors.length = 0;

    // Rename the group and update its membership.
    const card = page.locator("li.group-card").first();
    await card.getByLabel("Group name").fill("Main roster");
    await card.getByLabel("Player tags").fill("#2PY");
    await card.getByRole("button", { name: "Save changes" }).click();
    await expect(page.getByRole("heading", { name: "Main roster" })).toBeVisible();
    await expect(page.getByRole("link", { name: "#2PY", exact: true })).toBeVisible();
    await expect(page.getByRole("link", { name: "#2PP", exact: true })).toHaveCount(0);

    // Deletion requires an explicit confirmation.
    const confirmation = card.getByRole("checkbox", { name: /I understand this group/ });
    await card.getByRole("button", { name: "Delete group" }).click();
    expect(
      await confirmation.evaluate(
        (input: HTMLInputElement) => input.validity.valueMissing,
      ),
    ).toBe(true);
    await expect(page.getByRole("heading", { name: "Main roster" })).toBeVisible();

    // Confirmed deletion removes the group.
    await confirmation.check();
    await card.getByRole("button", { name: "Delete group" }).click();
    await expect(page.getByText("No private groups yet")).toBeVisible();

    // The account overview shows no groups.
    await page.goto("/account");
    await expect(page.getByText("No private groups", { exact: true })).toBeVisible();

    assertNoStore();
    expectNoPageErrors(errors);
  });

  test("the public user page exposes only canonical public data", async ({ page }) => {
    const urls = trackRequests(page);
    const errors = trackPageErrors(page);
    const assertNoStore = trackResponseCacheControl(page, ["/users/lensscout"]);

    await signIn(page);

    // Create private data that must never appear publicly.
    await page.goto("/account/saved-players");
    await page.getByLabel("Player tag").fill("#2p8");
    await page.getByRole("button", { name: "Save player" }).click();
    await expect(page.getByRole("link", { name: "Ember" })).toBeVisible();
    await page.goto("/account/groups");
    await page.getByLabel("Group name").fill("Scout Circle");
    await page.getByLabel("Player tags").fill("#2PP");
    await page.getByRole("button", { name: "Create group" }).click();
    await expect(page.getByRole("heading", { name: "Scout Circle" })).toBeVisible();

    // The public page shows only username, display name, and verified players.
    await page.goto("/users/lensscout");
    await expect(page.getByRole("heading", { name: "Lens Scout" })).toBeVisible();
    await expect(page.getByText("@lensscout")).toBeVisible();
    await expect(page.getByRole("link", { name: "Nova" })).toBeVisible();
    await expect(page.getByText("#2PP", { exact: true })).toBeVisible();

    await expect(
      page.getByRole("heading", { name: "Saved players", exact: true }),
    ).toHaveCount(0);
    await expect(page.getByRole("link", { name: "Ember" })).toHaveCount(0);
    await expect(page.getByText("#2P8", { exact: true })).toHaveCount(0);
    await expect(page.getByText("Scout Circle")).toHaveCount(0);
    await expect(page.getByText("Lens Keeper")).toHaveCount(0);
    const html = await page.content();
    for (const value of SENSITIVE_FIXTURE_VALUES) {
      expect(html, `public page must not contain ${value}`).not.toContain(value);
    }

    assertNoStore();
    expectNoPortRequests(urls, 8010, "public user page");
    expectNoPageErrors(errors);
  });

  test("logout clears the login and a fresh sign-in skips setup", async ({
    page,
    context,
  }) => {
    const errors = trackPageErrors(page);
    const assertNoStore = trackResponseCacheControl(page, ["/logout", "/login"]);

    await signIn(page);
    // The account already exists, so the sign-in lands directly on /account.
    await expect(page).toHaveURL(`${websiteOrigin}/account`);
    await expect(page.getByRole("heading", { name: "Your account" })).toBeVisible();

    await page.getByRole("button", { name: "Log out" }).click();
    await expect(page).toHaveURL(`${websiteOrigin}/`);
    await expect(page.getByRole("link", { name: "Log in" })).toBeVisible();
    const afterLogout = (await context.cookies()).find(
      (cookie) => cookie.name === LOGIN_COOKIE,
    );
    expect(afterLogout, "login cookie must be cleared by logout").toBeUndefined();

    // Protected routes redirect to login with a same-origin return path.
    await page.goto("/account");
    await expect(page).toHaveURL(/\/login\?returnPath=%2Faccount$/);
    await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();

    // Signing in again with the same identity skips setup entirely.
    await page.getByRole("link", { name: "Continue with Google" }).click();
    await expect(page).toHaveURL(`${websiteOrigin}/account`);
    await expect(page.getByRole("heading", { name: "Your account" })).toBeVisible();

    assertNoStore();
    expectNoPageErrors(errors);
  });

  test("account pages have no serious or critical accessibility violations", async ({
    page,
  }) => {
    await page.goto("/login");
    await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
    await expectNoSeriousAccessibilityViolations(page);

    await signIn(page);
    const pages: ReadonlyArray<{ path: string; heading: string }> = [
      { path: "/account/setup", heading: "Create your account" },
      { path: "/account", heading: "Your account" },
      { path: "/account/profile", heading: "Edit profile" },
      { path: "/account/saved-players", heading: "Saved players" },
      { path: "/account/verify-player", heading: "Verify a player" },
      { path: "/account/groups", heading: "Private groups" },
      { path: "/users/lensscout", heading: "Lens Scout" },
    ];
    for (const { path, heading } of pages) {
      await page.goto(path);
      await expect(
        page.getByRole("heading", { name: heading, exact: true }),
      ).toBeVisible();
      await expectNoSeriousAccessibilityViolations(page);
    }
  });

  test("account pages stay usable at a narrow viewport and 200 percent zoom", async ({
    page,
  }) => {
    const errors = trackPageErrors(page);

    const assertLayout = async () => {
      const layout = await page.evaluate(() => {
        const root = document.documentElement;
        const overflow = root.scrollWidth - root.clientWidth;
        const offenders = [...document.querySelectorAll("body *")]
          .map((element) => {
            const box = element.getBoundingClientRect();
            return {
              tag: element.tagName,
              className: (element as HTMLElement).className,
              text: (element.textContent ?? "").trim().slice(0, 40),
              left: Math.round(box.left),
              right: Math.round(box.right),
              width: Math.round(box.width),
            };
          })
          .filter((item) => item.left < -1 || item.right > window.innerWidth + 1)
          .sort((left, right) => right.right - left.right)
          .slice(0, 8);
        return { overflow, offenders };
      });
      expect(
        layout.overflow,
        `horizontal document overflow of ${layout.overflow}px:\n${JSON.stringify(layout.offenders, null, 2)}`,
      ).toBeLessThanOrEqual(1);

      const clipped = await page.evaluate(() => {
        const bad: string[] = [];
        for (const element of document.querySelectorAll("button, input, textarea")) {
          const style = getComputedStyle(element);
          const box = element.getBoundingClientRect();
          if (style.display === "none" || style.visibility === "hidden") continue;
          if (box.width === 0 || box.height === 0) continue;
          if (box.left < -1 || box.right > window.innerWidth + 1) {
            bad.push(
              `${element.tagName}.${(element as HTMLElement).className}: ${Math.round(box.left)}..${Math.round(box.right)}`,
            );
          }
        }
        return bad;
      });
      expect(
        clipped,
        `controls clipped at the viewport edge:\n${clipped.join("\n")}`,
      ).toEqual([]);

      const overlaps = await page.evaluate(() => {
        const controls = [...document.querySelectorAll("input, textarea, button")].filter(
          (element) => {
            const style = getComputedStyle(element);
            const box = element.getBoundingClientRect();
            return (
              style.display !== "none" &&
              style.visibility !== "hidden" &&
              box.width > 0 &&
              box.height > 0
            );
          },
        );
        const bad: string[] = [];
        for (let index = 0; index < controls.length; index += 1) {
          for (let other = index + 1; other < controls.length; other += 1) {
            const first = controls[index].getBoundingClientRect();
            const second = controls[other].getBoundingClientRect();
            const overlapX =
              Math.min(first.right, second.right) - Math.max(first.left, second.left);
            const overlapY =
              Math.min(first.bottom, second.bottom) - Math.max(first.top, second.top);
            if (overlapX > 2 && overlapY > 2) {
              bad.push(
                `${controls[index].tagName}.${(controls[index] as HTMLElement).className} × ${controls[other].tagName}.${(controls[other] as HTMLElement).className}`,
              );
            }
          }
        }
        return bad;
      });
      expect(overlaps, `overlapping controls:\n${overlaps.join("\n")}`).toEqual([]);
    };

    await signIn(page);

    // Desktop viewport: no overflow on any account page.
    for (const path of [
      "/account",
      "/account/profile",
      "/account/saved-players",
      "/account/verify-player",
      "/account/groups",
    ]) {
      await page.goto(path);
      await expect(page.locator("main")).toBeVisible();
      await assertLayout();
    }

    const accountPaths = [
      "/account",
      "/account/profile",
      "/account/saved-players",
      "/account/verify-player",
      "/account/groups",
      "/users/lensscout",
    ];

    // Check a narrow mobile viewport, then halve its CSS width to model the
    // reflow area available at 200 percent browser zoom.
    for (const viewport of [
      { width: 360, height: 800 },
      { width: 180, height: 400 },
    ]) {
      await page.setViewportSize(viewport);
      for (const path of accountPaths) {
        await page.goto(path);
        await expect(page.locator("main")).toBeVisible();
        await assertLayout();
        await expect(
          page.getByRole("link", { name: "Account Lens Scout", exact: true }),
        ).toBeVisible();
        await expect(page.getByRole("button", { name: "Log out" })).toBeVisible();
      }
    }

    await page.getByRole("button", { name: "Log out" }).click();
    await expect(page).toHaveURL(`${websiteOrigin}/`);
    await page.goto("/login");
    await expect(page.getByRole("heading", { name: "Sign in" })).toBeVisible();
    await assertLayout();
    await expect(page.getByRole("link", { name: "Continue with Google" })).toBeVisible();

    expectNoPageErrors(errors);
  });

  test("setup and login forms work with the keyboard and announce errors", async ({
    page,
    request,
  }) => {
    await resetFixture(request);
    const errors = trackPageErrors(page);
    await page.goto("/login");
    await page.getByRole("link", { name: "Continue with Google" }).focus();
    await page.keyboard.press("Enter");
    await expect(page).toHaveURL(`${websiteOrigin}/account/setup`);
    await expect(
      page.getByRole("heading", { name: "Create your account" }),
    ).toBeVisible();

    // In-form tab order reaches username, display name, then submit.
    await page.getByLabel("Username").focus();
    await page.keyboard.press("Tab");
    await expect(page.getByLabel("Display name")).toBeFocused();
    await page.keyboard.press("Tab");
    await expect(page.getByRole("button", { name: "Create account" })).toBeFocused();

    // Enter submits; the client-side rejection is announced and wired to the field.
    await page.getByLabel("Username").fill("login");
    await page.getByLabel("Display name").fill("Lens Keeper");
    await page.getByLabel("Username").focus();
    await page.keyboard.press("Enter");
    await expect(page.getByRole("alert")).toContainText("Username");
    await expect(page.getByLabel("Username")).toHaveAttribute("aria-invalid", "true");
    await expect(page.getByLabel("Username")).toHaveAttribute(
      "aria-describedby",
      "setup-username-error",
    );
    expectNoPageErrors(errors);
  });
});
