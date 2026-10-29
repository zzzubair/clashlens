import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  StaticRouterProvider,
  type RouteObject,
} from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ requestJson: vi.fn() }));

vi.mock("../../app/services/python.server", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../app/services/python.server")>();
  return { ...actual, requestJson: mocks.requestJson };
});

import { ArmiesOnDefenseCard } from "../../app/components/ArmyCards";
import type { LinkedPlayerCard } from "../../app/lib/account-contracts";
import { defaultSettings } from "../../app/lib/dashboard";
import type { ArmiesData } from "../../app/lib/dashboard-cards";
import { parseNotebook } from "../../app/lib/notebook";
import NotebookRoute from "../../app/routes/notebook";
import NotebookLayout from "../../app/routes/notebook.layout";
import { readCards } from "../../app/services/dashboard.server";
import type { PythonClient } from "../../app/services/python.server";

const ORIGIN = "https://clashlens.example";
const MAIN = "#LQ2V8PJ0";
const ALT = "#2PP8QLY0";
const RING = "B-7K3QX9";
const RECORD = {
  days: 6,
  no_data: 0,
  defenses: 50,
  held: 31,
  stars: [4, 9, 18, 19],
  trophies_lost: 1200,
};
const LEGEND_DAYS = Array.from({ length: 17 }, (_, index) => index + 1);

/** Main runs Ring from Day 10; Alt runs no base. */
function apiNotebook() {
  const player = (tag: string, name: string, current: string | null) => ({
    tag,
    name,
    league: "Legend League",
    runs: current ? [{ from: 10, to: 16, base: current, record: RECORD }] : [],
    current,
    next: null,
    picks: current ? [{ day: 10, base: current }] : [],
    legend_days: LEGEND_DAYS,
  });
  return {
    kind: "notebook",
    season: {
      id: "1791176400",
      start: "2026-10-05T05:00:00Z",
      end: "2026-11-02T05:00:00Z",
      today: 16,
      tomorrow: 17,
      current: true,
    },
    seasons: ["1791176400"],
    crew: null,
    crews: [],
    as_of_day: 15,
    players: [player(MAIN, "Lens Main", RING), player(ALT, "Lens Alt", null)],
    bases: [
      {
        id: RING,
        name: "Ring",
        mine: true,
        own: true,
        status: "running",
        here: true,
        added_day: 10,
        last_ran_day: 16,
        ran_by: 1,
        record: RECORD,
      },
    ],
  };
}

const DEFENSE = {
  lens: "defense",
  days: 7,
  covered_days: ["2026-10-20T05:00:00+00:00"],
  rows: [
    {
      type_id: 3,
      name: "Thrower",
      battles: 5,
      three_stars: 2,
      trophies: 170,
      legends: null,
    },
  ],
  total: { battles: 5, three_stars: 2, trophies: 170 },
  legends_total: null,
};

/** The private API's answer for each path asked for. */
function answer(path: string): unknown {
  if (path === "/v1/account/notebook") return apiNotebook();
  const armies = /^\/v1\/players\/([^/]+)\/armies/.exec(path);
  if (armies) return { ...DEFENSE, tag: decodeURIComponent(armies[1]!) };
  throw new Error(`unexpected ${path}`);
}

async function armiesCard(tag: string, notebook: boolean): Promise<ArmiesData> {
  const reads = readCards(
    ["armies-defense"],
    {
      client: {} as PythonClient,
      selected: { tag } as LinkedPlayerCard,
      settings: defaultSettings(),
      notebook,
    },
    { provider: "google", providerSubject: "11223344556677889900" },
  );
  const read = await reads["armies-defense"];
  if (!read) throw new Error("no armies read");
  return read;
}

async function render(routes: RouteObject[], url: string): Promise<string> {
  const handler = createStaticHandler(routes);
  const context = await handler.query(new Request(`${ORIGIN}${url}`));
  if (context instanceof Response) throw new Error("unexpected response");
  return renderToString(
    createElement(StaticRouterProvider, {
      router: createStaticRouter(handler.dataRoutes, context),
      context,
      hydrate: false,
    }),
  ).replaceAll("<!-- -->", "");
}

const card = (data: ArmiesData) =>
  render(
    [{ path: "/", Component: () => createElement(ArmiesOnDefenseCard, { data }) }],
    "/",
  );

const text = (html: string) =>
  html
    .replace(/<[^>]+>/g, " ")
    .replaceAll("&#x27;", "'")
    .replace(/\s+/g, " ")
    .trim();

describe("the Notebook on the dashboard", () => {
  beforeEach(() => {
    mocks.requestJson.mockReset();
    mocks.requestJson.mockImplementation((path: string) => Promise.resolve(answer(path)));
  });

  it("links Armies on defense to the base the account runs today", async () => {
    const data = await armiesCard(MAIN, true);
    expect(data.base).toEqual({ tag: MAIN, base: { id: RING, name: "Ring" } });
    const html = await card(data);
    expect(html).toContain(`href="/notebook/bases/${RING}"`);
    expect(text(html)).toContain("Armies on defense");
    expect(text(html)).toContain("Base: Ring ›");
    // The base ID is in the address only, never on the page.
    expect(text(html)).not.toContain(RING);
  });

  it("asks which base on an account that runs none, opening its Change sheet", async () => {
    const data = await armiesCard(ALT, true);
    expect(data.base).toEqual({ tag: ALT, base: null });
    const html = await card(data);
    expect(text(html)).toContain("Which base? ›");
    expect(html).toContain('href="/notebook?change=%232PP8QLY0"');

    const routes = (notebook: unknown): RouteObject[] => [
      {
        Component: NotebookLayout,
        children: [
          {
            path: "/notebook",
            Component: NotebookRoute,
            loader: () => ({ notebook: parseNotebook(notebook), error: null }),
          },
        ],
      },
    ];
    const sheet = /<dialog[^>]*class="nb-sheet"[^>]*aria-label="Lens Alt from"/;
    expect(await render(routes(apiNotebook()), "/notebook?change=%232PP8QLY0")).toMatch(
      sheet,
    );
    expect(await render(routes(apiNotebook()), "/notebook")).not.toContain(
      'class="nb-sheet"',
    );
    // A past Season's page is locked, so it opens no sheet.
    const past = apiNotebook();
    past.season.current = false;
    expect(await render(routes(past), "/notebook?change=%232PP8QLY0")).not.toContain(
      'class="nb-sheet"',
    );
  });

  it("leaves the armies card alone when the Notebook can't be read", async () => {
    mocks.requestJson.mockImplementation((path: string) =>
      path === "/v1/account/notebook"
        ? Promise.reject(new Error("down"))
        : Promise.resolve(answer(path)),
    );
    const data = await armiesCard(MAIN, true);
    expect(data.base).toBeNull();
    expect(text(await card(data))).not.toContain("Base:");
  });

  it("reads no Notebook and shows no base line while the Notebook is off", async () => {
    const data = await armiesCard(MAIN, false);
    expect(mocks.requestJson.mock.calls.map(([path]) => path)).not.toContain(
      "/v1/account/notebook",
    );
    expect(data).not.toHaveProperty("base");
    const html = await card(data);
    expect(html).not.toContain("/notebook");
    expect(text(html)).not.toContain("Which base?");
  });
});
