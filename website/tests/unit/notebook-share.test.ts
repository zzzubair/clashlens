import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ readSharedBase: vi.fn() }));

vi.mock("../../app/services/notebook-share.server", () => ({
  readSharedBase: mocks.readSharedBase,
}));

import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  StaticRouterProvider,
} from "react-router";

import {
  parseShareChange,
  parseSharedBase,
  type SharedBase,
} from "../../app/lib/notebook-share";
import SharedBaseRoute, { loader, meta } from "../../app/routes/b.$link";

const LINK = "Xq7pA2bC9dE0fG1hI2jK3l";
const GAME =
  "https://link.clashofclans.com/en?action=OpenLayout&id=TH17%3AHV%3AAAAAKgAAAAJ";
const RECORD = { days: 3, defenses: 6, held: 5, stars: [1, 2, 2, 1], trophies_lost: 101 };

function apiShared(overrides: Record<string, unknown> = {}) {
  return {
    kind: "shared-base",
    name: "Ring",
    link: GAME,
    record: RECORD,
    season: { start: "2026-10-05T05:00:00+00:00", end: "2026-11-02T05:00:00+00:00" },
    as_of_day: 3,
    players: null,
    ...overrides,
  };
}

async function page(link: string): Promise<{ status: number; html: string }> {
  const handler = createStaticHandler([
    {
      path: "/b/:link",
      Component: SharedBaseRoute,
      loader: (args) => loader(args as Parameters<typeof loader>[0]),
    },
  ]);
  const context = await handler.query(new Request(`https://clashlens.example/b/${link}`));
  if (context instanceof Response) throw new Error("unexpected response");
  const html = renderToString(
    createElement(StaticRouterProvider, {
      router: createStaticRouter(handler.dataRoutes, context),
      context,
      hydrate: false,
    }),
  ).replaceAll("&#x27;", "'");
  return { status: context.statusCode, html };
}

describe("shared base page", () => {
  beforeEach(() => {
    vi.stubEnv("CLASHLENS_NOTEBOOK_ENABLED", "true");
    mocks.readSharedBase.mockReset();
  });

  it("shows the base's record and base link to anyone with the link", async () => {
    mocks.readSharedBase.mockResolvedValue(parseSharedBase(apiShared()));
    const { status, html } = await page(LINK);
    expect(mocks.readSharedBase).toHaveBeenCalledWith(LINK);
    expect(status).toBe(200);
    expect(html).toContain("<h1>Ring</h1>");
    expect(html).toContain("5<!-- -->/<!-- -->6");
    expect(html).toContain("Open in game");
    expect(html).toContain("as of Day 3");
    // No player names unless the Clasher shows them.
    expect(html).not.toContain("Each account");
  });

  it("leaves out the base link and shows player names as the Clasher chooses", async () => {
    mocks.readSharedBase.mockResolvedValue(
      parseSharedBase(
        apiShared({
          link: null,
          players: [
            { name: "curtypie", record: { ...RECORD, days: 2 } },
            { name: "#8PP", record: { ...RECORD, days: 1 } },
          ],
        }),
      ),
    );
    const { html } = await page(LINK);
    expect(html).not.toContain("Open in game");
    expect(html).toContain("curtypie");
    expect(html).toContain("#8PP");
  });

  it("shows nothing of a stopped link, and never asks about a malformed one", async () => {
    mocks.readSharedBase.mockResolvedValue(null);
    const stopped = await page(LINK);
    expect(stopped.status).toBe(404);
    expect(stopped.html).toContain("This link doesn't open a base");
    const malformed = await page("B-7K3QX9");
    expect(malformed.status).toBe(404);
    expect(mocks.readSharedBase).toHaveBeenCalledTimes(1);
  });

  it("is an unknown page while the Notebook is off", async () => {
    vi.stubEnv("CLASHLENS_NOTEBOOK_ENABLED", "false");
    const { status } = await page(LINK);
    expect(status).toBe(404);
    expect(mocks.readSharedBase).not.toHaveBeenCalled();
  });

  it("asks search engines not to list it", () => {
    const shared = parseSharedBase(apiShared()) as SharedBase;
    const tags = meta({ loaderData: { shared, error: null } } as Parameters<
      typeof meta
    >[0]);
    expect(tags).toContainEqual({ name: "robots", content: "noindex" });
  });
});

describe("share contract", () => {
  it("refuses a shared page that does not fit the contract", () => {
    expect(parseSharedBase(apiShared({ record: { ...RECORD, held: -1 } }))).toBeNull();
    expect(parseSharedBase(apiShared({ players: [{ record: RECORD }] }))).toBeNull();
    expect(parseSharedBase(apiShared({ kind: "notebook" }))).toBeNull();
  });

  it("checks a posted share change before it reaches the private API", () => {
    expect(parseShareChange({ action: "stop", base: "B-7K3QX9" })).toEqual({
      action: "stop",
      base: "B-7K3QX9",
    });
    expect(parseShareChange({ action: "delete", base: "B-7K3QX9" })).toBeNull();
    expect(parseShareChange({ action: "share", base: "B-7K3QXI" })).toBeNull();
    expect(
      parseShareChange({ action: "settings", base: "B-7K3QX9", show_names: "yes" }),
    ).toBeNull();
    expect(
      parseShareChange({ action: "share", base: "B-7K3QX9", account: 7 }),
    ).toBeNull();
  });
});
