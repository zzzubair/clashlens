import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  StaticRouterProvider,
} from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ requestJson: vi.fn() }));

vi.mock("../../app/server/config.server", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/server/config.server")>();
  return {
    ...actual,
    isNotebookEnabled: () => true,
    getWebsiteConfig: () => ({ publicOrigin: new URL("https://clashlens.test") }),
  };
});
vi.mock("../../app/server/auth-guard.server", () => ({
  requireLogin: async () => ({ provider: "google", providerSubject: "subject-sloothy" }),
}));
vi.mock("../../app/services/python.server", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../app/services/python.server")>();
  return { ...actual, requestJson: mocks.requestJson };
});

import { Library } from "../../app/components/NotebookParts";
import { parseNotebook, type Notebook } from "../../app/lib/notebook";
import { action } from "../../app/routes/notebook.picture";
import { PythonApiError } from "../../app/services/python.server";

const LINKS = {
  full: "https://bucket.test/abc/full.webp?X-Amz-Signature=1",
  small: "https://bucket.test/abc/small.webp?X-Amz-Signature=1",
};
const RECORD = { days: 0, defenses: 0, held: 0, stars: [0, 0, 0, 0], trophies_lost: 0 };

function apiNotebook(picture: unknown) {
  return {
    kind: "notebook",
    season: {
      id: "1",
      start: "2026-10-05T05:00:00Z",
      end: "2026-11-02T05:00:00Z",
      today: 16,
      tomorrow: 17,
    },
    as_of_day: 15,
    players: [],
    bases: [
      {
        id: "B-7K3QX9",
        name: "Ring",
        status: "to_try",
        added_day: 9,
        last_ran_day: null,
        recent: "2026-10-20T06:00:00Z",
        record: RECORD,
        picture,
      },
    ],
  };
}

async function post(target: string, body?: Uint8Array<ArrayBuffer>, type = "image/png") {
  const request = new Request(`https://clashlens.test/notebook/picture${target}`, {
    method: "POST",
    headers: {
      Origin: "https://clashlens.test",
      ...(body ? { "Content-Type": type, "Content-Length": String(body.length) } : {}),
    },
    body,
  });
  const answer = (await action({ request, params: {}, context: {} } as never)) as {
    data: { picture?: unknown; error?: string };
    init: { status?: number } | null;
  };
  return { status: answer.init?.status ?? 200, ...answer.data };
}

describe("picture uploads", () => {
  beforeEach(() => mocks.requestJson.mockReset());

  it("sends the file to the private API and answers the new links", async () => {
    mocks.requestJson.mockResolvedValue({ kind: "notebook-picture", picture: LINKS });
    const file = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 1, 2, 3]);
    expect(await post("?base=B-7K3QX9", file)).toEqual({ status: 200, picture: LINKS });
    const [target, method, body] = mocks.requestJson.mock.calls[0]!;
    expect([target, method]).toEqual(["/v1/account/notebook/picture", "POST"]);
    expect(JSON.parse((body as Buffer).toString("utf8"))).toEqual({
      base: "B-7K3QX9",
      image: Buffer.from(file).toString("base64"),
    });
  });

  it("removes a picture with no file", async () => {
    mocks.requestJson.mockResolvedValue({ kind: "notebook-picture", picture: null });
    expect(await post("?base=B-7K3QX9&remove=1")).toEqual({ status: 200, picture: null });
    const body = mocks.requestJson.mock.calls[0]![2] as Buffer;
    expect(JSON.parse(body.toString("utf8"))).toEqual({ base: "B-7K3QX9", image: null });
  });

  it("refuses a file over 15 MB, or not a picture, before the private API", async () => {
    const big = new Uint8Array(15 * 1024 * 1024 + 1);
    expect(await post("?base=B-7K3QX9", big)).toEqual({
      status: 413,
      error: "picture_too_large",
    });
    expect((await post("?base=B-7K3QX9", new Uint8Array(4), "text/html")).status).toBe(
      400,
    );
    expect((await post("?base=nope", new Uint8Array(4))).status).toBe(400);
    expect(mocks.requestJson).not.toHaveBeenCalled();
  });

  it("passes on what the page can explain, and nothing else", async () => {
    mocks.requestJson.mockRejectedValueOnce(
      new PythonApiError(429, { error: "daily_limit" }),
    );
    expect(await post("?base=B-7K3QX9", new Uint8Array(4))).toEqual({
      status: 429,
      error: "daily_limit",
    });
    mocks.requestJson.mockRejectedValueOnce(
      new PythonApiError(500, { error: "secret_detail" }),
    );
    expect(await post("?base=B-7K3QX9", new Uint8Array(4))).toEqual({
      status: 503,
      error: "unavailable",
    });
  });
});

describe("pictures in the notebook", () => {
  it("reads a base's links, and refuses links that are not web addresses", () => {
    expect(parseNotebook(apiNotebook(LINKS))?.bases[0]?.picture).toEqual(LINKS);
    expect(parseNotebook(apiNotebook(null))?.bases[0]?.picture).toBeNull();
    expect(
      parseNotebook(apiNotebook({ ...LINKS, full: "javascript:alert(1)" })),
    ).toBeNull();
  });

  it("shows the small copy in the Library, opened by its own button", async () => {
    const notebook = parseNotebook(apiNotebook(LINKS)) as Notebook;
    const element = createElement(Library, { notebook, onNew: () => {} });
    const handler = createStaticHandler([{ path: "/", Component: () => element }]);
    const context = await handler.query(new Request("http://localhost/"));
    if (context instanceof Response) throw new Error("unexpected response");
    const html = renderToString(
      createElement(StaticRouterProvider, {
        router: createStaticRouter(handler.dataRoutes, context),
        context,
        hydrate: false,
      }),
    );
    expect(html).toContain(`src="${LINKS.small.replace("&", "&amp;")}"`);
    expect(html).toContain('loading="lazy"');
    // The button sits beside the row's link, not inside it.
    expect(html).toMatch(/<\/a><button[^>]*aria-label="See Ring&#x27;s picture"/);
  });
});
