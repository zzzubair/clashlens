import { randomUUID } from "node:crypto";
import { expect, test } from "@playwright/test";
import { ensureAccount, signIn } from "./helpers/account";
import { websiteOrigin } from "../fixtures/test-values";

test("two signed-in accounts keep saved players and groups private through direct requests", async ({
  browser,
}) => {
  const contexts = await Promise.all([
    browser.newContext({ baseURL: websiteOrigin }),
    browser.newContext({ baseURL: websiteOrigin }),
  ]);
  const owners = [];
  try {
    for (const [index, context] of contexts.entries()) {
      const page = await context.newPage();
      await page.route("**/authorize?*", async (route) => {
        const url = new URL(route.request().url());
        url.searchParams.set("login_hint", `fixture-google-subject-200${index}`);
        await route.continue({ url: url.href });
      });
      await signIn(page);
      await ensureAccount(page, `privacyowner${index}`, `Privacy Owner ${index}`);
      const origin = new URL(page.url()).origin;
      const tag = index === 0 ? "#2PP" : "#8PY";
      const groupName = `Private plan ${index}`;
      const saved = await context.request.post(`${origin}/account/saved-players`, {
        headers: { Origin: origin },
        form: { mode: "add", tag, idempotencyKey: randomUUID() },
      });
      expect(saved.ok()).toBeTruthy();
      const created = await context.request.post(`${origin}/account/groups`, {
        headers: { Origin: origin },
        form: { action: "create", name: groupName, idempotencyKey: randomUUID() },
      });
      expect(created.ok()).toBeTruthy();
      await page.goto("/account/groups");
      const group = page
        .locator("section")
        .filter({ has: page.getByRole("heading", { name: groupName, exact: true }) })
        .last();
      const groupId = await group.locator('input[name="groupId"]').first().inputValue();
      const added = await context.request.post(`${origin}/account/groups`, {
        headers: { Origin: origin },
        form: { action: "add-player", groupId, tag, idempotencyKey: randomUUID() },
      });
      expect(added.ok()).toBeTruthy();
      owners.push({
        context,
        page,
        origin,
        tag,
        groupName,
        groupId,
        username: `privacyowner${index}`,
      });
    }
    for (const [index, owner] of owners.entries()) {
      const other = owners[1 - index];
      for (const path of ["/account/saved-players", "/account/groups"]) {
        const response = await owner.context.request.get(
          `${owner.origin}${path}?username=${other.username}&groupId=${other.groupId}`,
        );
        expect(response.ok()).toBeTruthy();
        expect(response.headers()["cache-control"]).toContain("no-store");
        const body = await response.text();
        expect(body).not.toContain(other.groupName);
        expect(body).not.toContain(other.groupId);
        expect(body).not.toContain(other.tag);
      }
      // Forged forms use the attacker's real session and fresh operation ids.
      for (const action of ["update", "delete", "add-player", "remove-player"]) {
        const responses = [];
        for (const groupId of [other.groupId, randomUUID()]) {
          responses.push(
            await owner.context.request.post(`${owner.origin}/account/groups`, {
              headers: { Origin: owner.origin },
              form: {
                action,
                groupId,
                name: "Changed",
                tag: action === "remove-player" ? other.tag : owner.tag,
                confirm: "on",
                idempotencyKey: randomUUID(),
              },
            }),
          );
        }
        expect(responses[0].status()).toBe(responses[1].status());
        expect(responses[0].ok()).toBeFalsy();
      }
      await owner.context.request.post(`${owner.origin}/account/saved-players`, {
        headers: { Origin: owner.origin },
        form: { mode: "remove", tag: other.tag, idempotencyKey: randomUUID() },
      });
      const publicProfile = await owner.context.request.get(
        `${owner.origin}/users/${other.username}`,
      );
      const publicBody = await publicProfile.text();
      expect(publicBody).not.toContain(other.groupName);
      expect(publicBody).not.toContain(other.tag);
      await other.page.goto("/account/groups");
      await expect(
        other.page.getByRole("heading", { name: other.groupName, exact: true }),
      ).toBeVisible();
      await other.page.goto("/account/saved-players");
      await expect(
        other.page.getByText(other.tag, { exact: true }).first(),
      ).toBeVisible();
    }
  } finally {
    await Promise.all(contexts.map((context) => context.close()));
  }
});
