import { randomUUID } from "node:crypto";
import { expect, test, type Browser, type Page } from "@playwright/test";

import {
  ensureAccount,
  expectAccountButtonsRead,
  expectNoPageErrors,
  linkPlayer,
  signIn,
  trackPageErrors,
} from "./helpers/account";
import { websiteOrigin } from "../fixtures/test-values";

// Fake Clash API players in Legend League that no other browser test links.
const CAPTAIN_TAGS = ["#Q00UU", "#Q00UV"];
const MATE_TAGS = ["#Q00V0", "#Q00V2", "#Q00V8"];

/**
 * A fresh browser signed in as its own Google account, from its own address so
 * its links do not share the website's six a minute per address.
 */
async function clasher(
  browser: Browser,
  subject: string,
  username: string,
  name: string,
) {
  const context = await browser.newContext({
    baseURL: websiteOrigin,
    extraHTTPHeaders: { "CF-Connecting-IP": `203.0.113.${subject.slice(-3)}` },
  });
  const page = await context.newPage();
  await page.route("**/authorize?*", async (route) => {
    const url = new URL(route.request().url());
    url.searchParams.set("login_hint", `fixture-google-subject-${subject}`);
    await route.continue({ url: url.href });
  });
  await signIn(page);
  await ensureAccount(page, username, name);
  return { context, page, errors: trackPageErrors(page) };
}

async function link(page: Page, tag: string): Promise<void> {
  await page.goto("/account/verify-player");
  await linkPlayer(page, tag, /\/users\/[a-z][a-z0-9_]+\?linked=/);
}

const account = (page: Page, tag: string) =>
  page.locator("label").filter({ hasText: tag }).getByRole("checkbox");

test("an invite is accepted with 2 of 3 accounts, a signed-out visitor looks inside, one is kicked, and places stay above those in use", async ({
  browser,
}) => {
  // Five links, 11 seconds apart.
  test.setTimeout(200_000);
  const captain = await clasher(browser, "3101", "crewcaptain", "Crew Captain");
  const mate = await clasher(browser, "3102", "crewmate", "Crew Mate");
  try {
    for (const tag of CAPTAIN_TAGS) await link(captain.page, tag);
    for (const tag of MATE_TAGS.slice(0, 2)) await link(mate.page, tag);

    // The captain makes a crew with both accounts; its invite link opens straight away.
    const page = captain.page;
    await page.goto("/crews/new");
    await page.getByLabel("Crew name").fill("Night Owls");
    await page.getByLabel("Places", { exact: true }).fill("10");
    for (const tag of CAPTAIN_TAGS) await account(page, tag).check();
    await page.getByRole("button", { name: "Create crew" }).click();
    await expect(page).toHaveURL(/\/crews\/[0-9a-f-]{36}\?invite=1$/);
    const crewPath = new URL(page.url()).pathname;
    const sheet = page.getByRole("dialog", { name: "Invite to Night Owls" });
    await expect(sheet).toBeVisible();
    const invite = (await sheet.locator("code").textContent())!.trim();
    expect(invite).toMatch(/\/crews\/join\/[A-Za-z0-9_-]{22}$/);
    await expect(sheet.getByText(/8 places open/)).toBeVisible();
    await sheet.getByRole("button", { name: "Close" }).click();
    await expect(sheet).toBeHidden();

    // The mate links a third account from the invite and comes back to it.
    const invitePath = new URL(invite).pathname;
    await mate.page.goto(invitePath);
    await expect(mate.page.getByRole("heading", { name: "Night Owls" })).toBeVisible();
    await mate.page.getByRole("link", { name: "Link another player" }).click();
    await expect(mate.page).toHaveURL(/\/account\/verify-player\?return=/);
    await linkPlayer(mate.page, MATE_TAGS[2]!, invitePath);

    // 2 of 3 join.
    for (const tag of MATE_TAGS) await expect(account(mate.page, tag)).toBeEnabled();
    await account(mate.page, MATE_TAGS[0]!).check();
    await account(mate.page, MATE_TAGS[1]!).check();
    await account(mate.page, MATE_TAGS[2]!).uncheck();
    await mate.page.getByRole("button", { name: "Join with 2 players" }).click();
    await expect(mate.page).toHaveURL(`${crewPath}?saved=joined`);
    await expect(mate.page.getByRole("status")).toHaveText("You joined Night Owls.");
    await expect(mate.page.getByText("4 of 10 places")).toBeVisible();

    // Anyone can look inside, signed out too: from a player's page to the crew
    // and a board, with nothing to manage and no Clash Lens account named.
    const visitor = await browser.newContext({ baseURL: websiteOrigin });
    try {
      const view = await visitor.newPage();
      const viewErrors = trackPageErrors(view);
      // The list shows only when its answer beats the page's other reads.
      await expect(async () => {
        await view.goto(`/players/${encodeURIComponent(MATE_TAGS[0]!)}`);
        const crewLink = view.getByRole("link", { name: "Night Owls" });
        await expect(crewLink).toBeVisible({ timeout: 3_000 });
        await expect(crewLink).toHaveAttribute("href", crewPath);
      }).toPass({ intervals: [1_000, 2_000], timeout: 30_000 });
      await view.goto(crewPath);
      await expect(
        view.getByRole("heading", { name: "Night Owls", level: 1 }),
      ).toBeVisible();
      await expect(view.getByText("By invite only")).toBeVisible();
      const accounts = view.getByRole("region", { name: "Live leaderboard" });
      for (const tag of [...CAPTAIN_TAGS, ...MATE_TAGS.slice(0, 2)]) {
        await expect(accounts.getByText(tag)).toBeVisible();
      }
      await expect(view.getByRole("button", { name: "Invite" })).toHaveCount(0);
      await expect(view.getByRole("link", { name: "Members" })).toHaveCount(0);
      await expect(view.getByRole("link", { name: "Edit" })).toHaveCount(0);
      const html = await view.content();
      for (const hidden of ["Crew Captain", "crewcaptain", "Crew Mate", "crewmate"]) {
        expect(html).not.toContain(hidden);
      }
      expect(html).not.toContain(invitePath.split("/").pop()!);
      await view.goto(`${crewPath}/boards/live`);
      await expect(
        view.getByRole("heading", { name: "Live leaderboard", level: 1 }),
      ).toBeVisible();
      expect(await view.content()).not.toContain("crewcaptain");
      // The invite shows its crew before sign-in, and why an account is needed.
      await view.goto(invitePath);
      await expect(
        view.getByRole("heading", { name: "Join Night Owls", level: 1 }),
      ).toBeVisible();
      await expect(
        view.getByText("You need a Clash Lens account to join a crew."),
      ).toBeVisible();
      await expect(view.getByRole("link", { name: "Create account" })).toHaveAttribute(
        "href",
        `/signup?returnPath=${encodeURIComponent(invitePath)}`,
      );
      await view.setViewportSize({ width: 390, height: 844 });
      await expectAccountButtonsRead(view, true);
      expectNoPageErrors(viewErrors);
      // A link that can't be an invite says so; its 404 is the only page error.
      const broken = await view.goto("/crews/join/not-a-whole-link");
      expect(broken?.status()).toBe(404);
      await expect(
        view.getByRole("heading", { name: "This invite link doesn't work" }),
      ).toBeVisible();
    } finally {
      await visitor.close();
    }

    // The captain kicks one of the mate's accounts from its row, on a phone.
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto(`${crewPath}/members`);
    const mateCard = page.locator(".crew-member").filter({ hasText: "@crewmate" });
    await expect(mateCard.getByText(MATE_TAGS[2]!)).toHaveCount(0);
    const row = mateCard
      .locator(".crew-accounts > li")
      .filter({ hasText: MATE_TAGS[0]! });
    const x = row.getByLabel(/^Kick /);
    const box = (await x.boundingBox())!;
    expect(Math.min(box.width, box.height)).toBeGreaterThanOrEqual(44);
    expect(box.x + box.width).toBeLessThanOrEqual(390);
    await x.click();
    await row.getByRole("button", { name: "Kick", exact: true }).click();
    await expect(page.getByText(`Kicked ${MATE_TAGS[0]}.`)).toBeVisible();
    await expect(page.getByText("2 clashers · 3 of 10 places")).toBeVisible();

    // Places can't go below the 3 in use, in the form or behind it.
    await page.goto(`${crewPath}/settings`);
    const size = page.getByLabel("Places", { exact: true });
    await expect(size).toHaveAttribute("min", "3");
    await size.fill("2");
    await page.getByRole("button", { name: "Save size" }).click();
    expect(
      await size.evaluate((input: HTMLInputElement) => input.validity.rangeUnderflow),
    ).toBe(true);
    const origin = new URL(page.url()).origin;
    const refused = await captain.context.request.post(`${origin}${crewPath}/settings`, {
      headers: { Origin: origin },
      form: { intent: "resize", size: "2", idempotencyKey: randomUUID() },
    });
    expect(refused.status()).toBe(422);
    await page.reload();
    await expect(size).toHaveValue("10");

    expectNoPageErrors(captain.errors);
    expectNoPageErrors(mate.errors);
  } finally {
    await captain.context.close();
    await mate.context.close();
  }
});
