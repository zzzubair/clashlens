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

  // The profile's small Edit button opens Settings.
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

  // Unlinking asks first, says what changes and what stays, and Cancel keeps it.
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

  // Gone from the profile too, while the player's own page stays.
  await page.goto("/account");
  const players = page.getByRole("region", { name: "Clash players" });
  await expect(players.getByText(KEPT, { exact: true })).toBeVisible();
  await expect(players.getByText(UNLINKED, { exact: true })).toHaveCount(0);
  const playerPage = await page.request.get(`/players/${encodeURIComponent(UNLINKED)}`);
  expect(playerPage.status()).toBe(200);

  expectNoPageErrors(errors);
});
