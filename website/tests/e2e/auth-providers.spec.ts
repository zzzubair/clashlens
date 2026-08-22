import { expect, test } from "@playwright/test";

import {
  createAccount,
  expectNoPortRequests,
  expectNoSeriousAccessibilityViolations,
  resetFixture,
  signIn,
  signInDiscord,
  trackPageErrors,
  trackRequests,
} from "./helpers/account";
import {
  fixtureApiUrl,
  loginSecretB64,
  oidcClientSecret,
  oidcSubject,
  websiteOrigin,
} from "../fixtures/test-values";

test.describe("two-provider authentication flows", () => {
  let errors: string[];
  let requests: string[];

  test.beforeEach(async ({ request }) => {
    await resetFixture(request);
    errors = [];
    requests = [];
  });

  test("a Google account can link Discord and both resolve one account", async ({
    page,
    request,
  }) => {
    void request;
    errors = trackPageErrors(page);
    requests = trackRequests(page);

    // Fresh Google identity -> account setup.
    await signIn(page);
    await createAccount(page, "providerflow", "Provider Flow");

    // The account overview links to the sign-in connections page.
    await page.getByRole("link", { name: "Sign-in connections" }).click();
    await expect(page).toHaveURL(`${websiteOrigin}/account/providers`);
    await expect(
      page.getByRole("heading", { name: "Sign-in connections" }),
    ).toBeVisible();
    const googleRow = page.locator("li").filter({ hasText: "Google" });
    const discordRow = page.locator("li").filter({ hasText: "Discord" });
    await expect(googleRow.getByRole("button", { name: "Unlink" })).toBeVisible();
    await expect(googleRow.getByRole("button", { name: "Unlink" })).toBeDisabled();
    await expect(discordRow.getByRole("button", { name: "Link" })).toBeVisible();

    // Linking Discord completes a real fresh OAuth authorization.
    await discordRow.getByRole("button", { name: "Link" }).click();
    await expect(page).toHaveURL(`${websiteOrigin}/account/providers`);
    await expect(discordRow.getByRole("button", { name: "Unlink" })).toBeVisible();
    await expect(googleRow.getByRole("button", { name: "Unlink" })).toBeEnabled();
    await expectNoSeriousAccessibilityViolations(page);
    await page.screenshot({
      path: "test-results/screenshots/providers-desktop.png",
      fullPage: true,
    });

    const mobileContext = await page
      .context()
      .browser()
      ?.newContext({
        viewport: { width: 390, height: 844 },
      });
    if (mobileContext !== undefined) {
      const mobilePage = await mobileContext.newPage();
      await signIn(mobilePage);
      await mobilePage.goto("/account/providers");
      await expect(
        mobilePage.getByRole("heading", { name: "Sign-in connections" }),
      ).toBeVisible();
      await mobilePage.screenshot({
        path: "test-results/screenshots/providers-mobile.png",
        fullPage: true,
      });
      await mobilePage.close();
      await mobileContext.close();
    }

    expect(errors).toEqual([]);
    expectNoPortRequests(requests, 8010, "private Python API");
  });

  test("unlinking the session's own provider ends the session but keeps the account", async ({
    page,
  }) => {
    errors = trackPageErrors(page);

    // Create an account through Discord only.
    await signInDiscord(page);
    await createAccount(page, "discordflow", "Discord Flow");

    // Unlink Discord: it is not the final identity yet, so first link Google.
    await page.getByRole("link", { name: "Sign-in connections" }).click();
    await page
      .locator("li")
      .filter({ hasText: "Google" })
      .getByRole("button", { name: "Link" })
      .click();
    await expect(page).toHaveURL(`${websiteOrigin}/account/providers`);

    // The session was created by Discord; unlinking it must clear the login
    // cookie and require a fresh sign-in while the account stays intact.
    await page
      .locator("li")
      .filter({ hasText: "Discord" })
      .getByRole("button", { name: "Unlink" })
      .click();
    await expect(page).toHaveURL(`${websiteOrigin}/login`);

    // Signing back in through the remaining linked provider (Google)
    // resolves the same Clash Lens account.
    await page.getByRole("link", { name: "Continue with Google" }).click();
    await expect(page).toHaveURL(`${websiteOrigin}/account`);
    await expect(page.getByText("@discordflow")).toBeVisible();

    // The unlinked provider no longer appears as connected.
    await page.getByRole("link", { name: "Sign-in connections" }).click();
    await expect(
      page
        .locator("li")
        .filter({ hasText: "Discord" })
        .getByRole("button", { name: "Link" }),
    ).toBeVisible();
    expect(errors).toEqual([]);
  });

  test("the final linked provider refuses to unlink from the browser", async ({
    page,
    request,
  }) => {
    await resetFixture(request);
    errors = trackPageErrors(page);

    await signInDiscord(page);
    await createAccount(page, "singlesource", "Single Source");
    await page.getByRole("link", { name: "Sign-in connections" }).click();
    const unlinkButton = page
      .locator("li")
      .filter({ hasText: "Discord" })
      .getByRole("button", { name: "Unlink" });
    await expect(unlinkButton).toBeDisabled();
    expect(errors).toEqual([]);
  });

  test("the login page offers both providers on desktop and mobile", async ({ page }) => {
    await page.goto("/login");
    await expect(page.getByRole("link", { name: "Continue with Google" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Continue with Discord" })).toBeVisible();
    await page.screenshot({
      path: "test-results/screenshots/login-desktop.png",
      fullPage: true,
    });

    const mobile = await page
      .context()
      .browser()
      ?.newContext({
        viewport: { width: 390, height: 844 },
      });
    if (mobile === undefined) throw new Error("no browser context available");
    const mobilePage = await mobile.newPage();
    await mobilePage.goto("/login");
    await expect(
      mobilePage.getByRole("link", { name: "Continue with Discord" }),
    ).toBeVisible();
    await mobilePage.screenshot({
      path: "test-results/screenshots/login-mobile.png",
      fullPage: true,
    });
    await mobile.close();
  });
});

test.describe("privacy boundaries", () => {
  test("provider subjects and secrets never reach the browser", async ({
    page,
    request,
  }) => {
    await resetFixture(request);
    const response = await request.get(`${fixtureApiUrl}/healthz`);
    expect(response.ok()).toBe(true);

    const errors: string[] = trackPageErrors(page);
    await signIn(page);
    await createAccount(page, "privacycheck", "Privacy Check");
    await page.getByRole("link", { name: "Sign-in connections" }).click();
    const content = await page.content();
    expect(content).not.toContain(oidcSubject);
    expect(content).not.toContain(oidcClientSecret);
    expect(content).not.toContain(loginSecretB64);
    expect(errors).toEqual([]);
  });
});
