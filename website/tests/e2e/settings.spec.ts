import { expect, test, type Page } from "@playwright/test";

import {
  ensureAccount,
  expectNoPageErrors,
  expectNoSeriousAccessibilityViolations,
  linkPlayer,
  signIn,
  trackPageErrors,
} from "./helpers/account";

const KEPT = "#Q00V9";
const UNLINKED = "#Q00VY";

async function link(page: Page, tag: string): Promise<void> {
  await page.goto("/account/verify-player");
  await linkPlayer(page, tag);
}

test("Settings saves the name and default player, and unlinks a player after a calm check", async ({
  page,
}) => {
  const errors = trackPageErrors(page);
  await signIn(page);
  await ensureAccount(page, "lensscout", "Lens Scout");
  for (const tag of [KEPT, UNLINKED]) await link(page, tag);

  await page.getByRole("link", { name: "Edit your profile in Settings" }).click();
  await expect(page).toHaveURL(/\/account\/settings$/);
  await expectNoSeriousAccessibilityViolations(page);

  await page.getByLabel("Display name").fill("Lens Scout");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect(page).toHaveURL(/\?saved=name$/);
  await expect(page.getByRole("status")).toHaveText("Display name saved.");

  const row = (tag: string) =>
    page.locator(".settings-row").filter({ has: page.getByText(tag, { exact: true }) });
  await row(UNLINKED).getByRole("button", { name: "Make default" }).click();
  await expect(page.getByRole("status")).toHaveText("Default player saved.");
  await expect(row(UNLINKED).locator(".linked-player-default")).toBeVisible();

  await row(UNLINKED).getByRole("link", { name: "Unlink" }).click();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText(/^Unlink /);
  await expect(page.getByText(/\/stats uses .+ instead\./)).toBeVisible();
  await expect(page.getByText("You can link it again any time")).toBeVisible();
  await expectNoSeriousAccessibilityViolations(page);
  await page.getByRole("link", { name: "Cancel" }).click();
  await expect(page).toHaveURL(/\/account\/settings$/);
  await expect(row(UNLINKED)).toBeVisible();

  await row(UNLINKED).getByRole("link", { name: "Unlink" }).click();
  await page.getByRole("button", { name: /^Unlink / }).click();
  await expect(page).toHaveURL(/\/account\/settings\?saved=unlinked&tag=%23Q00VY$/);
  await expect(page.getByRole("status")).toHaveText(`${UNLINKED} unlinked.`);
  await expect(row(UNLINKED)).toHaveCount(0);
  await expect(row(KEPT)).toBeVisible();

  await page.goto("/account");
  const players = page.getByRole("region", { name: "Clash players" });
  await expect(players.getByText(KEPT, { exact: true })).toBeVisible();
  await expect(players.getByText(UNLINKED, { exact: true })).toHaveCount(0);
  const playerPage = await page.request.get(`/players/${encodeURIComponent(UNLINKED)}`);
  expect(playerPage.status()).toBe(200);

  expectNoPageErrors(errors);
});

test("Save stays busy, and the rejected name's warning stays away, until the saved name loads", async ({
  page,
}) => {
  await signIn(page);
  await ensureAccount(page, "lensscout", "Lens Scout");
  await page.goto("/account/settings");
  const name = page.getByLabel("Display name");
  const save = page.locator(".settings-name").getByRole("button");
  const warning = page.locator("#settings-display-name-error");

  await name.fill("N".repeat(81));
  await save.click();
  await expect(warning).toBeVisible();
  await expect(name).toHaveValue("N".repeat(81));

  // Hold the read of the page that follows a successful save.
  let release = () => {};
  const held = new Promise<void>((resolve) => (release = resolve));
  let reading = () => {};
  const readStarted = new Promise<void>((resolve) => (reading = resolve));
  await page.route(/\/account\/settings\.data\?saved=name/, async (route) => {
    reading();
    await held;
    await route.continue();
  });
  try {
    await name.fill("Lens Scout");
    await save.click();
    await readStarted;
    await expect(save).toBeDisabled();
    await expect(save).toHaveText("Saving…");
    await expect(warning).toHaveCount(0);
  } finally {
    release();
  }
  await expect(page.getByRole("status")).toHaveText("Display name saved.");
  await expect(save).toBeEnabled();
  await expect(save).toHaveText("Save");
});
