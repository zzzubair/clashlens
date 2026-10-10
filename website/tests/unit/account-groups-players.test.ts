import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getWebsiteConfig: vi.fn(),
  requireLogin: vi.fn(),
  listGroups: vi.fn(),
  checkPlayerTag: vi.fn(),
  addGroupPlayer: vi.fn(),
  removeGroupPlayer: vi.fn(),
}));

vi.mock("../../app/server/config.server", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/server/config.server")>();
  return { ...actual, getWebsiteConfig: mocks.getWebsiteConfig };
});

vi.mock("../../app/server/auth-guard.server", () => ({
  requireLogin: mocks.requireLogin,
}));

vi.mock("../../app/services/python.server", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../app/services/python.server")>();
  return {
    ...actual,
    createPythonClient: () => ({ listGroups: mocks.listGroups }),
  };
});

vi.mock("../../app/services/group-players.server", () => ({
  checkPlayerTag: mocks.checkPlayerTag,
  addGroupPlayer: mocks.addGroupPlayer,
  removeGroupPlayer: mocks.removeGroupPlayer,
}));

import GroupsRoute, { action } from "../../app/routes/account.groups";
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  StaticRouterProvider,
} from "react-router";
import { mapGroups } from "../../app/lib/account-contracts";
import { loadWebsiteConfig } from "../../app/server/config.server";
import { PythonApiError } from "../../app/services/python.server";
import { worstGroups } from "../fixtures/worst-case-accounts";

const ORIGIN = "https://clashlens.example";
const IDENTITY = { provider: "google", providerSubject: "11223344556677889900" } as const;
const IDEMPOTENCY_KEY = "3be934b5-68fa-4741-8c7b-e03592e4ad70";
const GROUP_ID = "6c1e3f8a-2a44-4b7d-9c0e-1f2a3b4c5d6e";
const TAG = "#P0LQ2Y8";

interface Outcome {
  status: number;
  data: {
    fieldErrors: { tag?: string };
    notice: string | null;
    playerIdempotencyKey: string;
    generalError: { error: { message: string } } | null;
  };
}

async function submit(fields: Record<string, string>): Promise<Outcome> {
  const result = (await action({
    request: new Request(`${ORIGIN}/account/groups`, {
      method: "POST",
      headers: {
        "content-type": "application/x-www-form-urlencoded",
        Origin: ORIGIN,
      },
      body: new URLSearchParams({
        groupId: GROUP_ID,
        idempotencyKey: IDEMPOTENCY_KEY,
        ...fields,
      }).toString(),
    }),
    context: undefined,
  } as never)) as { data: Outcome["data"]; init: { status?: number } };
  return { status: result.init.status ?? 200, data: result.data };
}

function groupWith(tags: string[]) {
  return {
    season: "1791176400",
    groups: [
      {
        groupId: GROUP_ID,
        name: "Clanmates",
        tags,
        players: tags.map((tag) => ({
          tag,
          name: null,
          trophies: null,
          state: "tracking",
        })),
      },
    ],
  };
}

describe("adding and removing one group player", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.getWebsiteConfig.mockReturnValue(
      loadWebsiteConfig({
        NODE_ENV: "test",
        CLASHLENS_LOGIN_ENABLED: "true",
        CLASHLENS_PUBLIC_ORIGIN: ORIGIN,
        CLASHLENS_GOOGLE_CLIENT_ID: "test-client.apps.googleusercontent.com",
        CLASHLENS_GOOGLE_CLIENT_SECRET: "test-client-secret",
        CLASHLENS_DISCORD_CLIENT_ID: "1234567890123456789",
        CLASHLENS_DISCORD_CLIENT_SECRET: "discord-test-secret",
        CLASHLENS_LOGIN_SECRET_B64: "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8",
      }),
    );
    mocks.requireLogin.mockResolvedValue(IDENTITY);
    mocks.listGroups.mockResolvedValue(groupWith([]));
    mocks.checkPlayerTag.mockResolvedValue({ tag: TAG, state: "tracking" });
    mocks.addGroupPlayer.mockResolvedValue({
      tag: TAG,
      name: "Nova",
      trophies: 5412,
      state: "tracking",
    });
  });

  it("keeps trophies out of an add confirmation that may replay a saved response", async () => {
    const added = await submit({ action: "add-player", tag: " p0lq2y8 " });

    expect(added.status).toBe(200);
    expect(added.data.notice).toBe("Added Nova (#P0LQ2Y8).");
    expect(mocks.addGroupPlayer).toHaveBeenCalledWith(
      IDENTITY,
      GROUP_ID,
      TAG,
      IDEMPOTENCY_KEY,
    );
    expect(added.data.playerIdempotencyKey).not.toBe(IDEMPOTENCY_KEY);
  });

  it("explains a pending Season reset in both the add notice and member row", async () => {
    mocks.addGroupPlayer.mockResolvedValue({
      tag: TAG,
      name: "Nova",
      trophies: null,
      state: "tracking",
      seasonResetPending: true,
    });
    const added = await submit({ action: "add-player", tag: TAG });
    expect(added.data.notice).toBe(
      "Added Nova (#P0LQ2Y8). Waiting for this player's Season reset.",
    );
    const listed = mapGroups({
      season: "1791176400",
      groups: [
        {
          group_id: GROUP_ID,
          name: "Clanmates",
          tags: [TAG],
          players: [
            {
              tag: TAG,
              name: "Nova",
              trophies: null,
              state: "tracking",
              season_reset_pending: true,
            },
          ],
        },
      ],
    });
    const handler = createStaticHandler([
      {
        path: "/account/groups",
        Component: GroupsRoute,
        loader: () => ({
          groups: listed?.groups ?? [],
          // No Season: this checks the server's waiting flag, not the clock.
          season: null,
          error: null,
          createIdempotencyKey: IDEMPOTENCY_KEY,
          updateIdempotencyKeys: { [GROUP_ID]: IDEMPOTENCY_KEY },
          deleteIdempotencyKeys: { [GROUP_ID]: IDEMPOTENCY_KEY },
          addIdempotencyKeys: { [GROUP_ID]: IDEMPOTENCY_KEY },
          removeIdempotencyKeys: { [GROUP_ID]: { [TAG]: IDEMPOTENCY_KEY } },
        }),
      },
    ]);
    const context = await handler.query(new Request(`${ORIGIN}/account/groups`));
    if (context instanceof Response) throw new Error("unexpected response");
    const html = renderToString(
      createElement(StaticRouterProvider, {
        router: createStaticRouter(handler.dataRoutes, context),
        context,
      }),
    ).replaceAll("&#x27;", "'");
    expect(html).toContain("Waiting for this player's Season reset");
    expect(html).not.toContain("6,000 trophies");
  });

  it("adds a real player outside Legend League with a no-data label", async () => {
    mocks.checkPlayerTag.mockResolvedValue({ tag: TAG, state: "not_in_legend" });
    mocks.addGroupPlayer.mockResolvedValue({
      tag: TAG,
      name: "Nova",
      trophies: 3100,
      state: "not_in_legend",
    });

    const added = await submit({ action: "add-player", tag: TAG });

    expect(added.data.notice).toBe(
      "Added Nova (#P0LQ2Y8). Not in Legend League, no data.",
    );
  });

  it.each([
    [
      "a malformed tag",
      { tag: "hello!" },
      [],
      400,
      "Enter one valid player tag, like #2PY0LQ.",
    ],
    ["a duplicate", { tag: TAG }, [TAG], 409, "#P0LQ2Y8 is already in this group."],
    [
      "a 21st player",
      { tag: TAG },
      Array.from({ length: 20 }, (_, index) => `#2${"PYLQGRJCUV"[index % 10]}${index}`),
      422,
      "This group already has 20 players, the most a comparison shows.",
    ],
  ])(
    "refuses %s before checking the tag with the game",
    async (_, fields, tags, status, message) => {
      mocks.listGroups.mockResolvedValue(groupWith(tags));

      const refused = await submit({ action: "add-player", ...fields });

      expect(refused.status).toBe(status);
      expect(refused.data.fieldErrors.tag).toContain(message);
      expect(mocks.checkPlayerTag).not.toHaveBeenCalled();
      expect(mocks.addGroupPlayer).not.toHaveBeenCalled();
    },
  );

  it.each([
    ["not_found", 422, "Clash of Clans has no player with the tag #P0LQ2Y8."],
    ["checking", 409, "Still checking #P0LQ2Y8 with Clash of Clans."],
    ["failed", 503, "Clash of Clans could not be reached to check #P0LQ2Y8."],
  ])("does not add a tag the game answered %s for", async (state, status, message) => {
    mocks.checkPlayerTag.mockResolvedValue({ tag: TAG, state });

    const refused = await submit({ action: "add-player", tag: TAG });

    expect(refused.status).toBe(status);
    expect(refused.data.fieldErrors.tag).toContain(message);
    expect(mocks.addGroupPlayer).not.toHaveBeenCalled();
  });

  it("shows Python's refusal when the group filled up meanwhile", async () => {
    mocks.addGroupPlayer.mockRejectedValue(
      new PythonApiError(422, { error: "group_full" }),
    );

    const refused = await submit({ action: "add-player", tag: TAG });

    expect(refused.status).toBe(422);
    expect(refused.data.fieldErrors.tag).toContain("already has 20 players");
  });

  it("explains a lookup refused by the per-connection limit", async () => {
    mocks.checkPlayerTag.mockRejectedValue(
      new PythonApiError(429, { error: "rate_limited", retry_after_seconds: 60 }),
    );

    const refused = await submit({ action: "add-player", tag: TAG });

    expect(refused.status).toBe(429);
    expect(refused.data.fieldErrors.tag).toBe(
      "Too many player checks from your connection. Wait a minute and try again.",
    );
  });

  it("removes one player without a page redirect", async () => {
    const removed = await submit({ action: "remove-player", tag: TAG });

    expect(removed.status).toBe(200);
    expect(removed.data.notice).toBe("Removed #P0LQ2Y8 from the group.");
    expect(mocks.removeGroupPlayer).toHaveBeenCalledWith(
      IDENTITY,
      GROUP_ID,
      TAG,
      IDEMPOTENCY_KEY,
    );
    expect(mocks.checkPlayerTag).not.toHaveBeenCalled();
  });

  it("reports a group deleted elsewhere", async () => {
    mocks.removeGroupPlayer.mockRejectedValue(
      new PythonApiError(404, { error: "group_not_found" }),
    );

    const removed = await submit({ action: "remove-player", tag: TAG });

    expect(removed.data.generalError?.error.message).toBe(
      "The group no longer exists. Refresh the page.",
    );
  });

  it("lists a full, a one-player and an empty group with worst-case names", async () => {
    const html = await renderGroups(worstGroups());
    for (const text of ["20 of 20 players", "1 of 20 players", "No players yet"])
      expect(html).toContain(text);
  });

  it("opens adding and editing below the card's buttons without needing JavaScript", async () => {
    const html = await renderGroups(worstGroups());
    const card = html.slice(html.indexOf(`<li id="group-${GROUP_ID}"`));
    // The three buttons share one row; each panel below it holds ordinary forms, closed at first.
    expect(card).toMatch(
      new RegExp(
        `^<li[^>]*><div class="group-card-head"><h3>[^<]*</h3><a [^>]*>Compare players</a>` +
          `<a class="button button-secondary" href="/account/groups\\?group=${GROUP_ID}&amp;panel=add#group-${GROUP_ID}" aria-expanded="false"[^>]*>Add player</a>` +
          `<a class="button button-secondary" href="/account/groups\\?group=${GROUP_ID}&amp;panel=edit#group-${GROUP_ID}" aria-expanded="false"[^>]*>Edit</a></div>` +
          `<div id="group-${GROUP_ID}-add" class="group-panel" hidden=""><form[^>]* action="/account/groups" method="post"><input type="hidden" name="action" value="add-player"/>`,
      ),
    );
    expect(card).toMatch(
      /^[^]*?<div id="[^"]+-edit" class="group-panel" hidden=""><div class="group-edit">[^]*?name="action" value="update"[^]*?>Save name<\/button>[^]*?aria-label="Remove [^"]+ from [^"]+"[^]*?<details class="group-delete-step"><summary[^>]*>Delete group<\/summary>[^]*?<button type="submit"[^>]*>Yes, delete group<\/button><a [^>]*>Keep group<\/a>/,
    );
    expect(html).not.toMatch(/class="group-panel">/);
  });

  it("opens the panel a no-JavaScript link asks for, in that card only", async () => {
    const card = (html: string) =>
      html.slice(html.indexOf(`<li id="group-${GROUP_ID}"`)).split('<li id="group-')[1];
    const add = await renderGroups(worstGroups(), undefined, `?group=${GROUP_ID}&panel=add`);
    expect(card(add)).toContain(`<div id="group-${GROUP_ID}-add" class="group-panel">`);
    expect(card(add)).toContain(
      `href="/account/groups#group-${GROUP_ID}" aria-expanded="true" aria-controls="group-${GROUP_ID}-add">Add player</a>`,
    );
    expect(add.match(/class="group-panel">/g)).toHaveLength(1);

    const edit = await renderGroups(worstGroups(), undefined, `?group=${GROUP_ID}&panel=edit`);
    expect(card(edit)).toContain(`<div id="group-${GROUP_ID}-edit" class="group-panel">`);
    // Edit lists the players with their Remove buttons, so the plain list steps aside.
    expect(card(edit)).not.toContain("group-members");
    expect(edit.match(/class="group-panel">/g)).toHaveLength(1);
  });

  it("reopens the panel a no-JavaScript form came from, with its result", async () => {
    const reply = (action: string, outcome: object) => ({
      action,
      groupId: GROUP_ID,
      createIdempotencyKey: IDEMPOTENCY_KEY,
      updateIdempotencyKey: IDEMPOTENCY_KEY,
      deleteIdempotencyKey: IDEMPOTENCY_KEY,
      playerIdempotencyKey: IDEMPOTENCY_KEY,
      fieldErrors: {},
      notice: null,
      generalError: null,
      values: { name: "Taken", tag: "#2PP", action, groupId: GROUP_ID },
      ...outcome,
    });
    const card = (html: string) =>
      html.slice(html.indexOf(`<li id="group-${GROUP_ID}"`)).split('<li id="group-')[1];

    const added = card(
      await renderGroups(
        worstGroups(),
        reply("add-player", { fieldErrors: { tag: "Not a player." } }),
      ),
    );
    expect(added).toContain(`<div id="group-${GROUP_ID}-add" class="group-panel">`);
    expect(added).toContain("Not a player.");
    expect(added).toContain('value="#2PP"');
    expect(added).toContain(`<div id="group-${GROUP_ID}-edit" class="group-panel" hidden="">`);

    const renamed = await renderGroups(
      worstGroups(),
      reply("update", {
        fieldErrors: { name: "A group with this name already exists." },
      }),
    );
    expect(card(renamed)).toContain(`<div id="group-${GROUP_ID}-edit" class="group-panel">`);
    expect(card(renamed)).toContain("A group with this name already exists.");
    expect(card(renamed)).toContain('value="Taken"');
    // Only that group's card opens.
    expect(renamed.match(/class="group-panel">/g)).toHaveLength(1);

    const deleted = card(
      await renderGroups(
        worstGroups(),
        reply("delete", {
          generalError: { error: { code: "conflict", message: "Gone elsewhere." } },
        }),
      ),
    );
    expect(deleted).toContain(`<div id="group-${GROUP_ID}-edit" class="group-panel">`);
    expect(deleted).toMatch(/<details class="group-delete-step" open="">/);
    expect(deleted).toContain("Gone elsewhere.");
  });
});

/** The page as server-rendered HTML, after a no-JavaScript form result if given. */
async function renderGroups(
  groups: ReturnType<typeof worstGroups>,
  actionData?: object,
  search = "",
) {
  const keys = Object.fromEntries(
    groups.map((group) => [group.groupId, IDEMPOTENCY_KEY]),
  );
  const handler = createStaticHandler([
    {
      path: "/account/groups",
      Component: GroupsRoute,
      action: () => actionData,
      loader: () => ({
        groups,
        season: null,
        error: null,
        createIdempotencyKey: IDEMPOTENCY_KEY,
        updateIdempotencyKeys: keys,
        deleteIdempotencyKeys: keys,
        addIdempotencyKeys: keys,
        removeIdempotencyKeys: {},
      }),
    },
  ]);
  const context = await handler.query(
    actionData === undefined
      ? new Request(`${ORIGIN}/account/groups${search}`)
      : new Request(`${ORIGIN}/account/groups`, { method: "POST", body: new FormData() }),
  );
  if (context instanceof Response) throw new Error("unexpected response");
  return renderToString(
    createElement(StaticRouterProvider, {
      router: createStaticRouter(handler.dataRoutes, context),
      context,
    }),
  ).replaceAll("<!-- -->", "");
}
