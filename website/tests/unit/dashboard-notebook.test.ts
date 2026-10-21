import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  StaticRouterProvider,
  type RouteObject,
} from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getWebsiteConfig: vi.fn(),
  readLoginIdentity: vi.fn(),
}));

vi.mock("../../app/server/config.server", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/server/config.server")>();
  return { ...actual, getWebsiteConfig: mocks.getWebsiteConfig };
});

vi.mock("../../app/server/actions.server", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/server/actions.server")>();
  return { ...actual, readLoginIdentity: mocks.readLoginIdentity };
});

import {
  addableCards,
  DEFAULT_LAYOUT,
  defaultSettings,
  serializeSettings,
  type CardId,
} from "../../app/lib/dashboard";
import type { CardDataSet, CardReads } from "../../app/lib/dashboard-cards";
import { parseNotebook } from "../../app/lib/notebook";
import DashboardRoute, {
  loader,
  type DashboardLoaderData,
} from "../../app/routes/dashboard";
import NotebookRoute from "../../app/routes/notebook";
import NotebookLayout from "../../app/routes/notebook.layout";
import { loadWebsiteConfig } from "../../app/server/config.server";

const ORIGIN = "https://clashlens.example";
const MAIN = "#LQ2V8PJ0";
const ALT = "#2PP8QLY0";
const RING = "B-7K3QX9";
const RECORD = {
  days: 6,
  defenses: 50,
  held: 31,
  stars: [4, 9, 18, 19],
  trophies_lost: 1200,
};

const outcome = (battles: number, three_stars: number, trophies: number) => ({
  battles,
  three_stars,
  trophies,
});

/** Main runs Ring from Day 10 and Ring v2 from tomorrow; Alt runs no base. */
function apiNotebook() {
  const legendDays = Array.from({ length: 17 }, (_, index) => index + 1);
  return {
    kind: "notebook",
    season: {
      id: "2026-10",
      start: "2026-10-05T05:00:00Z",
      end: "2026-11-02T05:00:00Z",
      today: 16,
      tomorrow: 17,
    },
    as_of_day: 15,
    players: [
      {
        tag: MAIN,
        name: "Lens Main",
        league: "Legend League",
        runs: [{ from: 10, to: 16, base: RING, record: RECORD }],
        current: RING,
        next: { day: 17, base: "B-9QW2M4" },
        picks: [
          { day: 10, base: RING },
          { day: 17, base: "B-9QW2M4" },
        ],
        legend_days: legendDays,
      },
      {
        tag: ALT,
        name: "Lens Alt",
        league: "Legend League",
        runs: [],
        current: null,
        next: null,
        picks: [],
        legend_days: legendDays,
      },
    ],
    bases: [
      {
        id: RING,
        name: "Ring",
        status: "active",
        added_day: 10,
        last_ran_day: 16,
        recent: "2026-10-20T06:00:00Z",
        record: RECORD,
      },
      {
        id: "B-9QW2M4",
        name: "Ring v2",
        status: "active",
        added_day: 16,
        last_ran_day: null,
        recent: "2026-10-21T06:00:00Z",
        record: { days: 0, defenses: 0, held: 0, stars: [0, 0, 0, 0], trophies_lost: 0 },
      },
    ],
  };
}

function linked(tag: string, name: string) {
  return {
    tag,
    name,
    clan: null,
    state: "tracking",
    reason: null,
    trophies: 5370,
    season_reset_pending: false,
    rank: 316,
    league: null,
    today: { net: 55, attacks: 2, defenses: 1 },
  };
}

/** The fake Python service, and every path it was asked for. */
const python = { layout: [] as CardId[], requests: [] as string[] };

function answer(url: URL): [status: number, body: unknown] {
  const path = decodeURIComponent(url.pathname);
  python.requests.push(path);
  if (path === "/v1/account")
    return [
      200,
      {
        username: "nova88",
        display_name: "Nova",
        preferences: {
          dashboard: serializeSettings({ ...defaultSettings(), layout: python.layout }),
        },
        providers: ["google"],
      },
    ];
  if (path === "/v1/users/nova88")
    return [
      200,
      {
        username: "nova88",
        display_name: "Nova",
        verified_players: [linked(MAIN, "Lens Main"), linked(ALT, "Lens Alt")],
      },
    ];
  if (path === "/v1/account/notebook") return [200, apiNotebook()];
  if (path.endsWith("/armies")) {
    const tag = path.split("/")[3]!;
    return [
      200,
      {
        tag,
        lens: "defense",
        days: 7,
        covered_days: ["2026-10-20T05:00:00+00:00"],
        rows: [{ type_id: 3, name: "Thrower", ...outcome(5, 2, 170), legends: null }],
        total: outcome(5, 2, 170),
        legends_total: null,
      },
    ];
  }
  return [404, { error: "not_found" }];
}

async function load(player = MAIN) {
  const result = (await loader({
    request: new Request(`${ORIGIN}/dashboard?player=${encodeURIComponent(player)}`),
    params: {},
    context: {},
  } as unknown as Parameters<typeof loader>[0])) as unknown as {
    data: DashboardLoaderData;
  };
  if (result.data.kind !== "signed-in") throw new Error("expected signed in");
  const cards = Object.fromEntries(
    await Promise.all(
      Object.entries(result.data.cards as CardReads).map(async ([card, read]) => [
        card,
        await read,
      ]),
    ),
  ) as CardDataSet;
  return { ...result.data, cards };
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

/** Each dashboard card's own markup, by card id. */
async function renderCards(data: DashboardLoaderData): Promise<Record<string, string>> {
  const html = await render(
    [{ path: "/dashboard", Component: DashboardRoute, loader: () => data }],
    "/dashboard",
  );
  const found: Record<string, string> = {};
  for (const match of html.matchAll(
    /<section[^>]*data-card="([^"]+)"[^>]*>([\s\S]*?)<\/section>/g,
  ))
    found[match[1]!] = match[2]!;
  return found;
}

const text = (html: string) =>
  html
    .replace(/<[^>]+>/g, " ")
    .replaceAll("&#x27;", "'")
    .replace(/\s+/g, " ")
    .trim();

describe("the Notebook on the dashboard", () => {
  beforeEach(() => {
    vi.stubEnv("CLASHLENS_DASHBOARD_ENABLED", "true");
    vi.stubEnv("CLASHLENS_NOTEBOOK_ENABLED", "true");
    vi.stubEnv("CLASHLENS_PYTHON_API_URL", "http://python-fixture.test/");
    vi.stubEnv(
      "CLASHLENS_PYTHON_HMAC_SECRET_B64",
      "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8",
    );
    vi.stubEnv("CLASHLENS_PYTHON_HMAC_SECRET_FILE", "");
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
    mocks.readLoginIdentity.mockResolvedValue({
      provider: "google",
      providerSubject: "11223344556677889900",
    });
    Object.assign(python, { layout: ["armies-defense", "notebook"], requests: [] });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: URL | string) => {
        const [status, body] = answer(new URL(String(input)));
        return new Response(JSON.stringify(body), {
          status,
          headers: { "content-type": "application/json" },
        });
      }),
    );
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("links Armies on defense to today's base, and the card shows it", async () => {
    const data = await load();
    // Both cards share one read of the Notebook.
    expect(
      python.requests.filter((path) => path === "/v1/account/notebook"),
    ).toHaveLength(1);
    expect(data.cardsOff).toEqual([]);
    expect(data.cards.notebook).toEqual({
      tag: MAIN,
      base: {
        id: RING,
        name: "Ring",
        since: 10,
        record: {
          days: 6,
          defenses: 50,
          held: 31,
          stars: [4, 9, 18, 19],
          trophiesLost: 1200,
        },
      },
      next: { day: 17, name: "Ring v2" },
    });

    const cards = await renderCards(data);
    expect(cards["armies-defense"]).toContain(`href="/notebook/bases/${RING}"`);
    expect(text(cards["armies-defense"]!)).toContain("Base: Ring ›");
    expect(text(cards.notebook!)).toBe(
      "Notebook Open › R Ring since Day 10 · held 31 of 50 → Ring v2 from Day 17 Change base ›",
    );
    expect(cards.notebook).toContain('href="/notebook?change=%23LQ2V8PJ0"');
    // The base ID is in the address only, never on the page.
    expect(text(cards.notebook!)).not.toContain(RING);
    expect(text(cards["armies-defense"]!)).not.toContain(RING);
  });

  it("asks which base on an account that runs none, opening its Change sheet", async () => {
    const data = await load(ALT);
    expect(data.cards.notebook).toEqual({ tag: ALT, base: null, next: null });
    const cards = await renderCards(data);
    expect(text(cards["armies-defense"]!)).toContain("Which base? ›");
    expect(cards["armies-defense"]).toContain('href="/notebook?change=%232PP8QLY0"');
    expect(text(cards.notebook!)).toBe("Notebook Open › No base today Which base? ›");

    const notebook = parseNotebook(apiNotebook())!;
    const routes: RouteObject[] = [
      {
        Component: NotebookLayout,
        children: [
          {
            path: "/notebook",
            Component: NotebookRoute,
            loader: () => ({ notebook, error: null }),
          },
        ],
      },
    ];
    const opened = await render(routes, "/notebook?change=%232PP8QLY0");
    expect(opened).toMatch(/<dialog[^>]*class="nb-sheet"[^>]*aria-label="Lens Alt from"/);
    expect(await render(routes, "/notebook")).not.toContain('class="nb-sheet"');
  });

  it("leaves the base line and the card out while the Notebook is off", async () => {
    vi.stubEnv("CLASHLENS_NOTEBOOK_ENABLED", "false");
    const data = await load();
    expect(python.requests).not.toContain("/v1/account/notebook");
    expect(data.cardsOff).toEqual(["notebook"]);
    // A saved Notebook card is left out, as if never added.
    expect(data.settings.layout).toEqual(["armies-defense"]);
    expect(addableCards(["armies-defense"], data.cardsOff)).not.toContain("notebook");
    expect(data.cards["armies-defense"]).not.toHaveProperty("base");
    const cards = await renderCards(data);
    expect(Object.keys(cards)).toEqual(["armies-defense"]);
    expect(cards["armies-defense"]).not.toContain("/notebook");
  });

  it("keeps the Notebook card out of the default layout", () => {
    expect(DEFAULT_LAYOUT).not.toContain("notebook");
    expect(addableCards(DEFAULT_LAYOUT, [])).toContain("notebook");
  });
});
