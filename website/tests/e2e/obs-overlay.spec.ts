import { expect, test } from "@playwright/test";

import { websiteOrigin } from "../fixtures/test-values";
import {
  ensureAccount,
  expectNoPageErrors,
  signIn,
  trackPageErrors,
} from "./helpers/account";

// A fake Clash API player in Legend League that no other browser test links.
const TAG = "#Q00VP";

test("a streamer sets up the OBS overlay, OBS shows it live at stream size, and a new link retires the old one", async ({
  browser,
  page,
}) => {
  test.setTimeout(150_000);
  const errors = trackPageErrors(page);
  await page.route("**/authorize?*", async (route) => {
    const url = new URL(route.request().url());
    url.searchParams.set("login_hint", "fixture-google-subject-3301");
    await route.continue({ url: url.href });
  });
  await signIn(page);
  await ensureAccount(page, "lensstream", "Lens Stream");

  // The game's verify call has a small shared budget a second, so try again.
  await page.goto("/account/verify-player");
  await expect(async () => {
    await page.getByLabel("Player tag").fill(TAG);
    await page.getByLabel("API token", { exact: true }).fill(`VERIFY-${TAG.slice(1)}`);
    await page.getByRole("button", { name: "Link player" }).click();
    await expect(page).toHaveURL(/\?linked=/, { timeout: 3_000 });
  }).toPass({ intervals: [1_000, 2_000], timeout: 30_000 });

  // The account page leads to it, next to Discord alerts.
  await page.getByRole("link", { name: "OBS overlay" }).click();
  await expect(page).toHaveURL(/\/account\/obs-overlay$/);
  await expect(page.getByRole("radio", { name: new RegExp(TAG) })).toBeChecked();
  const address = page.getByLabel("Overlay address");
  const firstLink = await address.inputValue();
  expect(firstLink).toMatch(/\/o\/[A-Za-z0-9_-]{22}$/);
  const saved = page.getByRole("status").filter({ hasText: /Saved|save by themselves/ });

  // A slim strip along the top, with made-up battles on.
  await page.locator(".obs-shape", { hasText: "Slim strip" }).click();
  await expect(page.getByLabel("Width")).toHaveValue("1110");
  await page.getByRole("button", { name: "▶ Start demo" }).click();
  await expect(page.getByRole("button", { name: "■ Stop demo" })).toBeVisible();
  await expect(saved).toHaveText("Saved. Your overlay in OBS updates within 3 seconds.");

  // OBS: a browser source the size of the whole stream, with nothing signed in.
  const obs = await browser.newContext({
    baseURL: websiteOrigin,
    viewport: { width: 1920, height: 1080 },
  });
  const source = await obs.newPage();
  const sourceErrors = trackPageErrors(source);
  await source.goto(new URL(firstLink).pathname);
  const overlay = source.locator(".obs-overlay");
  await expect(overlay).toHaveClass(/ov-strip/);
  await expect(source.locator(".site-header")).toHaveCount(0);
  await expect(source.locator("footer")).toHaveCount(0);
  expect(
    await source.evaluate(() => getComputedStyle(document.body).backgroundColor),
  ).toBe("rgba(0, 0, 0, 0)");
  // Placed where the streamer put it, the size they gave, with the quiet mark.
  const box = await source.locator(".ov-box").boundingBox();
  expect(box).toMatchObject({ x: 40, y: 40, width: 1110 });
  await expect(source.getByText("clashlens.net")).toBeVisible();
  // The first made-up battle lands within a second or two, with its pop-up.
  await expect(source.locator(".ov-sq:not(.ov-open)")).not.toHaveCount(0);
  await expect(source.locator(".ov-toast.ov-show")).toBeVisible({ timeout: 15_000 });

  // A change on the account page reaches OBS within a few seconds.
  await page.locator(".obs-seg label", { hasText: "Dark" }).click();
  await expect(overlay).toHaveClass(/ov-dark-text/, { timeout: 8_000 });
  await page.getByRole("button", { name: "■ Stop demo" }).click();
  await expect(source.locator(".ov-name")).not.toHaveText("Demo player", {
    timeout: 8_000,
  });

  expectNoPageErrors(sourceErrors);

  // A new link retires the old one for good: OBS on it shows nothing.
  page.once("dialog", (dialog) => void dialog.accept());
  await page.getByRole("button", { name: "Make a new link" }).click();
  await expect(address).not.toHaveValue(firstLink);
  const secondLink = await address.inputValue();
  await expect(source.locator(".ov-stage")).toBeEmpty({ timeout: 8_000 });
  expect((await source.request.get(`${new URL(firstLink).pathname}/settings`)).status()).toBe(
    404,
  );
  await source.goto(new URL(secondLink).pathname);
  await expect(source.locator(".obs-overlay")).toHaveClass(/ov-strip/);
  await expect(source.locator(".ov-name")).toBeVisible();

  // A smaller stream keeps the overlay inside it.
  await page.getByLabel("Width").fill("1800");
  await page.getByLabel("Height").fill("800");
  await page.getByLabel("Height").press("Enter");
  await expect(page.getByLabel("Width")).toHaveValue("1800");
  await page.getByLabel("Stream size").selectOption("1280x720");
  await expect(page.getByLabel("Width")).toHaveValue("1280");
  await expect(page.getByLabel("Height")).toHaveValue("720");

  await obs.close();
  expectNoPageErrors(errors);
});
