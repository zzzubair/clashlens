import { expect, test, type Locator, type Page } from "@playwright/test";

import { ensureAccount, signIn } from "./helpers/account";

// Fake Clash API players in Legend League that no other browser test links:
// with two, setting a base Active asks which account.
const TAGS = ["#Q0208", "#Q0209"];
// A 2 by 2 PNG, enough for the private API to make both copies from.
const PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAIAAAACCAIAAAD91JpzAAAAEElEQVR4nGP438AARAwQCgAt7gX9iz9uhAAAAABJRU5ErkJggg==",
  "base64",
);

test.use({ viewport: { width: 390, height: 844 } });

/** Give the base on its open page a picture. */
async function addPicture(page: Page, name: string): Promise<void> {
  await page
    .getByText("+ Add a picture")
    .locator("input[type=file]")
    .setInputFiles({ name: `${name}.png`, mimeType: "image/png", buffer: PNG });
  await expect(page.getByRole("button", { name: `See ${name}'s picture` })).toBeVisible();
}

/**
 * Tap a base's picture: it opens full size over the page, at least 44 px to
 * tap, with the address unchanged and nothing saved, and closes again.
 */
async function preview(
  page: Page,
  within: Locator,
  name: string,
  saves: string[],
): Promise<void> {
  const before = page.url();
  const count = saves.length;
  const picture = within.getByRole("button", { name: `See ${name}'s picture` });
  await expect(picture).toBeVisible();
  const box = (await picture.boundingBox())!;
  expect(Math.min(box.width, box.height)).toBeGreaterThanOrEqual(44);
  await picture.click();
  const shown = page.getByRole("dialog", { name: `${name}, picture` });
  await expect(shown).toBeVisible();
  expect(page.url()).toBe(before);
  await shown.getByRole("button", { name: "Close" }).click();
  await expect(shown).toBeHidden();
  expect(page.url()).toBe(before);
  expect(saves).toHaveLength(count);
}

test("a base's picture opens full size from every list, never saving or leaving the page", async ({
  page,
}) => {
  test.setTimeout(120_000);
  await page.route("**/authorize?*", async (route) => {
    const url = new URL(route.request().url());
    url.searchParams.set("login_hint", "fixture-google-subject-3501");
    await route.continue({ url: url.href });
  });
  await signIn(page);
  await ensureAccount(page, "notebookpictures", "Notebook Pictures");
  for (const tag of TAGS) {
    await page.goto("/account/verify-player");
    await expect(async () => {
      await page.getByLabel("Player tag").fill(tag);
      await page.getByLabel("API token", { exact: true }).fill(`VERIFY-${tag.slice(1)}`);
      await page.getByRole("button", { name: "Link player" }).click();
      await expect(page).toHaveURL(/\?linked=/, { timeout: 3_000 });
    }).toPass({ intervals: [1_000, 2_000], timeout: 30_000 });
  }

  // Ring runs on the first account; Spare waits To try. Both get a picture.
  await page.goto("/notebook");
  const account = page
    .getByRole("region", { name: "Your accounts" })
    .locator("article")
    .first();
  await account.getByRole("button", { name: "Add your base" }).click();
  await page.getByRole("textbox", { name: "Name", exact: true }).fill("Ring");
  await page.getByRole("button", { name: "Save" }).click();
  await page.getByRole("button", { name: "+ New base" }).click();
  await page.getByRole("textbox", { name: "Name", exact: true }).fill("Spare");
  await page.getByRole("button", { name: "Save" }).click();
  for (const name of ["Ring", "Spare"]) {
    await page
      .getByRole("link", { name: new RegExp(`^${name}`) })
      .first()
      .click();
    await expect(page.getByRole("heading", { name, level: 1 })).toBeVisible();
    await addPicture(page, name);
    await page.goto("/notebook");
  }

  const saves: string[] = [];
  page.on("request", (request) => {
    if (request.method() === "POST") saves.push(request.url());
  });
  const library = page.locator(".nb-list").last();

  // The account row and the Library.
  await preview(page, account, "Ring", saves);
  await preview(page, library, "Spare", saves);

  // Change: the pick list.
  await account.getByRole("button", { name: "Change" }).click();
  const change = page.getByRole("dialog", { name: /from$/ });
  await preview(page, change, "Spare", saves);
  await expect(change).toBeVisible();
  await change.getByRole("button", { name: "Close" }).first().click();

  // Days, then today: the bases run near that day.
  await account.getByRole("button", { name: "Days" }).click();
  await page.getByRole("button", { name: /· today/ }).click();
  const day = page.getByRole("dialog", { name: /^Day \d+ · / });
  await preview(page, day, "Ring", saves);
  await expect(day).toBeVisible();
  await day.getByRole("button", { name: "Close" }).first().click();

  // Spare's page: setting it Active asks which account, shown by its base.
  await page
    .getByRole("link", { name: /^Spare/ })
    .first()
    .click();
  await page.getByRole("button", { name: /^Status: / }).click();
  const status = page.getByRole("dialog", { name: "Spare" });
  await status.getByRole("button", { name: "Active" }).click();
  await expect(status.getByText("Active on which account?")).toBeVisible();
  await preview(page, status, "Ring", saves);
  await expect(status.getByText("Active on which account?")).toBeVisible();
});
