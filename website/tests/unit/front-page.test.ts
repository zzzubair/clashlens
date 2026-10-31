import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  MemoryRouter,
  RouterContextProvider,
  StaticRouterProvider,
  useLoaderData,
} from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  createPythonClient: vi.fn(),
  navigationState: "idle",
}));

vi.mock("react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router")>();
  return { ...actual, useNavigation: () => ({ state: mocks.navigationState }) };
});

vi.mock("../../app/services/python.server", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../app/services/python.server")>();
  return { ...actual, createPythonClient: mocks.createPythonClient };
});

import { PythonApiError } from "../../app/services/python.server";
import Search, { loader as searchLoader } from "../../app/routes/search";
import { loader as suggestionLoader } from "../../app/routes/player-search";
import { clearPublicRefreshLimits } from "../../app/server/abuse.server";
import { clientAddressContext } from "../../app/server/client-address.server";
import { loader as frontLoader } from "../../app/routes/dashboard";
import { loader as oldDashboard } from "../../app/routes/dashboard.redirect";
import { loader as oldGroups } from "../../app/routes/account.groups.redirect";
import { FrontPage, type FrontPageData } from "../../app/components/FrontPage";
import { SearchSuggestions } from "../../app/components/PlayerSearch";
import { TagSearchAgain } from "../../app/components/TagSearchAgain";
import { TeaserBoard } from "../../app/components/DashboardTeaser";

const search = {
  kind: "player-search",
  query: "Nova",
  exactTag: null,
  results: [],
  users: [],
  knownOnly: true,
};

function Front() {
  return createElement(FrontPage, { data: useLoaderData<FrontPageData>() });
}

// Searches open /search; everything else is the signed-out front page.
async function renderHome(data: object, search: string) {
  const front = { loginAvailable: false, dashboardEnabled: false, error: null, ...data };
  const handler = createStaticHandler([
    { path: "/", Component: Front, loader: () => front },
    { path: "/search", Component: Search, loader: () => data },
  ]);
  const path = search.startsWith("?q=") ? `/search${search}` : `/${search}`;
  const context = await handler.query(new Request(`https://clashlens.example${path}`));
  if (context instanceof Response) throw new Error("unexpected response");
  return renderToString(
    createElement(StaticRouterProvider, {
      router: createStaticRouter(handler.dataRoutes, context),
      context,
      hydrate: false,
    }),
  ).replaceAll("<!-- -->", "");
}

it("keeps the exact player tag link when another player's name matches the tag", async () => {
  const html = await renderHome(
    {
      leaderboard: null,
      query: "#2PP",
      error: null,
      search: {
        exactTag: "#2PP",
        users: [],
        results: [{ tag: "#2PY", name: "#2PP", clan: "Test clan", trophies: 5000 }],
      },
    },
    "?q=%232PP",
  );
  expect(html).toContain('href="/players/%232PP"');
  expect(html).toContain("Open player profile");
  expect(html).not.toContain('href="/players/%232PY"');
});

it("labels Clash Lens profiles so they do not look like game players", async () => {
  const html = await renderHome(
    {
      leaderboard: null,
      query: "Nova",
      error: null,
      search: {
        exactTag: null,
        users: [{ username: "nova_star", displayName: "Nova", linkedPlayerCount: 2 }],
        results: [{ tag: "#2PP", name: "Nova", clan: "Test clan", trophies: 5000 }],
      },
    },
    "?q=Nova",
  );
  expect(html).toContain('class="search-result search-result-profile"');
  expect(html).toMatch(
    /href="\/users\/nova_star"[^>]*><bdi>Nova<\/bdi> <span class="profile-badge">Clash Lens profile<\/span><\/a>/,
  );
  expect(html.match(/profile-badge/g)).toHaveLength(1);
});

it("disables the search button and marks results busy while a search loads", async () => {
  const data = {
    leaderboard: null,
    query: "Nova",
    error: null,
    search: { query: "Nova", exactTag: null, users: [], results: [] },
  };
  const idle = await renderHome(data, "?q=Nova");
  expect(idle).toContain('<button type="submit">Search</button>');
  expect(idle).toContain('aria-busy="false"');

  mocks.navigationState = "loading";
  try {
    const pending = await renderHome(data, "?q=Nova");
    expect(pending).toContain('<button type="submit" disabled="">Searching…</button>');
    expect(pending).toContain('aria-busy="true"');
  } finally {
    mocks.navigationState = "idle";
  }
});

it("leaves search help to the placeholder and keeps a short tag hint when nothing matches", async () => {
  const html = await renderHome(
    { leaderboard: null, query: "Nova", error: null, search },
    "?q=Nova",
  );
  expect(html).not.toContain("To look someone up");
  expect(html).toContain('placeholder="Name, @username or #tag"');
  expect(html).toContain("<h3>No players or profiles found</h3>");
  expect(html).toContain("<p>Enter a full #tag to look up anyone.</p>");
  const dropdown = renderToString(
    createElement(
      MemoryRouter,
      null,
      createElement(SearchSuggestions, {
        id: "suggestions",
        data: { search, error: null } as never,
        loading: false,
      }),
    ),
  );
  expect(dropdown).toContain(
    "No players or profiles found. <!-- -->Enter a full #tag to look up anyone.",
  );
});

it("says a complete #tag with letters the game never uses is not a tag", async () => {
  const query = "#ZZZZZZZZ";
  const html = await renderHome(
    { leaderboard: null, query, error: null, search: { ...search, query } },
    "?q=%23ZZZZZZZZ",
  );
  expect(html).toContain(
    "<p>#ZZZZZZZZ is not a Clash of Clans player tag. Tags use only 0, 2, 8, 9 and the letters P, Y, L, Q, G, R, J, C, U and V.</p>",
  );
  expect(html).not.toContain("Enter a full #tag");
});

it("shows the search heading and explains a logout the server could not record", async () => {
  const data = {
    leaderboard: { entries: [], totalTracked: 13263 },
    query: "",
    error: null,
    search: null,
  };
  expect(await renderHome(data, "")).not.toContain("could not record it");
  expect(await renderHome(data, "?saved=logout")).toContain("You&#x27;re signed out.");
  const html = await renderHome(data, "?logout=unrecorded");
  expect(html).toContain("Find any Legend League player");
  expect(html).not.toContain("13,263 tracked players.");
  expect(html).not.toContain("Top 0");
  expect(html).toContain("<h3>No standings available yet</h3>");
  expect(html).not.toContain("<table");
  expect(html).toContain(
    "You are signed out on this browser, but Clash Lens could not record it.",
  );
  expect(html).not.toContain("log out again");
  expect(html).not.toContain("sign out again");
});

it("keeps worst-case names readable: one player, right-to-left text and no clan", async () => {
  const html = await renderHome(
    {
      leaderboard: {
        totalTracked: 1,
        generatedAt: "2026-10-04T04:00:00Z",
        entries: [
          {
            rank: 1,
            tag: "#2PP0JLQ8VV",
            name: "محمد الأسطورة 👑",
            clan: "العائلة الملكية",
            trophies: 6512,
            freshness: {
              state: "fresh",
              observedAt: "2026-10-04T03:59:00Z",
              ageSeconds: 60,
            },
            state: "available",
            resetRank: null,
            todayChange: null,
            recentDays: [],
          },
        ],
      },
      query: "",
      error: null,
      search: null,
    },
    "",
  );
  expect(html).toContain("Top 10 now<");
  // Isolated so right-to-left names and clans do not reorder the text around them.
  expect(html).toContain("<bdi>محمد الأسطورة 👑</bdi>");
  expect(html).toContain("<bdi>العائلة الملكية</bdi>");
  // The clan has its own column on wide screens, and a line under the name on
  // a phone.
  expect(html).toContain('<th scope="col" class="clan-cell">Clan</th>');
  expect(html).toContain('<td class="clan-cell"><bdi>العائلة الملكية</bdi></td>');
  expect(html).toContain(
    '<span class="player-sub"><span class="player-clan"><bdi>العائلة الملكية</bdi></span></span>',
  );

  const dropdown = renderToString(
    createElement(
      MemoryRouter,
      null,
      createElement(SearchSuggestions, {
        id: "suggestions",
        data: {
          search: {
            ...search,
            results: [
              {
                tag: "#P0Y8URGQ2",
                name: "محمد الأسطورة",
                clan: "العائلة الملكية",
                trophies: 6455,
              },
            ],
            users: [
              {
                username: "mohammad_legend",
                displayName: "محمد 👑",
                linkedPlayerCount: 1,
              },
            ],
          },
          error: null,
        } as never,
        loading: false,
      }),
    ),
  ).replaceAll("<!-- -->", "");
  expect(dropdown).toContain("#P0Y8URGQ2 · <bdi>العائلة الملكية</bdi>");
  expect(dropdown).toMatch(/<\/svg>6,455<\/span>/);
  expect(dropdown).toContain("<bdi>محمد 👑</bdi>");
  expect(dropdown).toContain("1 linked account<");
});

it.each([
  [11869, "11,869 tracked players are"],
  [1, "1 tracked player is"],
])("explains an empty Season-reset board with %i waiting", async (waiting, count) => {
  const html = await renderHome(
    {
      leaderboard: {
        entries: [],
        totalTracked: 13263,
        seasonResetPending: waiting,
        generatedAt: "2026-10-05T05:00:01Z",
      },
      query: "",
      error: null,
      search: null,
    },
    "",
  );
  expect(html).not.toContain("Top 0");
  expect(html).toContain("Waiting for the new Season");
  expect(html).toContain(`${count} waiting`);
  expect(html).toContain("will be ranked once their profile shows the new Season");
  expect(html).toContain('role="status"');
  expect(html).not.toContain("<table");
  expect(html).not.toContain("No standings available yet");
  expect(html).not.toContain("could not be loaded");
});

it("shows ranked players without the waiting note once the board has entries", async () => {
  const html = await renderHome(
    {
      leaderboard: {
        entries: [
          {
            rank: 1,
            tag: "#2PP",
            name: "Nova",
            clan: "Example",
            trophies: 5000,
            freshness: {
              state: "fresh",
              observedAt: "2026-10-05T05:10:00Z",
              ageSeconds: 0,
            },
            resetRank: null,
            todayChange: null,
            recentDays: [],
          },
        ],
        totalTracked: 13263,
        seasonResetPending: 11868,
        generatedAt: "2026-10-05T05:10:00Z",
      },
      query: "",
      error: null,
      search: null,
    },
    "",
  );
  expect(html).toContain('<table aria-label="Latest saved standings"');
  expect(html).toContain('data-testid="tracked-player-row"');
  expect(html).not.toContain("Waiting for the new Season");
  expect(html).not.toContain("waiting for their Season reset");
});

it("keeps the empty-board message when no players are waiting for the Season reset", async () => {
  const html = await renderHome(
    {
      leaderboard: { entries: [], totalTracked: 0, seasonResetPending: 0 },
      query: "",
      error: null,
      search: null,
    },
    "",
  );
  expect(html).toContain("<h3>No standings available yet</h3>");
  expect(html).not.toContain("Waiting for the new Season");
  expect(html).not.toContain("could not be loaded");
});

it("shows the Top 10's Today from the Legend day's start and names each bar by its own day", async () => {
  const html = await renderHome(
    {
      leaderboard: {
        totalTracked: 1,
        // Minutes after the Reset of Mon 5 Oct, before Sunday's result saved.
        generatedAt: "2026-10-05T05:10:00Z",
        entries: [
          {
            rank: 1,
            tag: "#2PP",
            name: "Player 1",
            clan: "",
            // 70 below its Reset board total: the automatic defense loss the
            // Reset reading was taken before, not today's.
            trophies: 5230,
            freshness: {
              state: "fresh",
              observedAt: "2026-10-05T05:04:00Z",
              ageSeconds: 360,
            },
            state: "available",
            resetRank: 1,
            todayChange: 0,
            recentDays: [
              { start: "2026-10-02T05:00:00Z", trophyChange: 40 },
              { start: "2026-10-03T05:00:00Z", trophyChange: null },
              { start: "2026-10-04T05:00:00Z", trophyChange: null },
            ],
          },
        ],
      },
      query: "",
      error: null,
      search: null,
    },
    "",
  );
  expect(html).toContain(
    '<td class="today-cell"><span class="today-change">0</span></td>',
  );
  expect(html).toContain('aria-label="Daily trophies: Fri 2 Oct +40"');
  expect(html).toMatch(/<th scope="col" class="recent-cell" title="2–4 Oct, /);
});

it("says once, under the search, how many players are ranked and how fresh, and counts old rows", async () => {
  const entry = (rank: number, state: string, observedAt: string) => ({
    rank,
    tag: ["#2PP", "#2PY", "#8PY"][rank - 1],
    name: `Player ${rank}`,
    clan: "",
    trophies: 5000,
    freshness: { state, observedAt, ageSeconds: 0 },
    state: "available",
    resetRank: null,
    todayChange: null,
    recentDays: [],
  });
  const html = await renderHome(
    {
      leaderboard: {
        totalTracked: 13263,
        totalEntries: 9000,
        generatedAt: "2026-10-04T04:00:00Z",
        entries: [
          entry(1, "fresh", "2026-10-04T03:54:00Z"),
          entry(2, "stale", "2026-10-03T23:59:00Z"),
          entry(3, "stale", "2026-10-03T23:58:00Z"),
        ],
      },
      query: "",
      error: null,
      search: null,
    },
    "",
  );
  // The newest row's time, 6 minutes before the board was read, with the
  // live pulse as it is under 10 minutes old; "updated … ago" wraps as one.
  expect(html).toMatch(
    /<p class="front-proof"><span class="live-pip" aria-hidden="true"><\/span>9,000 <span class="front-proof-wide">Legend <\/span>players ranked · <span class="front-proof-age">updated <time dateTime="2026-10-04T03:54:00Z" title="4 Oct 2026, 03:54 UTC">6 min ago<\/time><\/span><\/p>/,
  );
  // The Top 10 doesn't repeat it.
  expect(html).not.toMatch(/Updated <|Last updated </);
  expect(html).toContain("2 of 3 more than 10 minutes old");
});

it("counts a ranking as old once it passes 10 minutes while the page is open", async () => {
  const observedAt = "2026-10-04T04:00:00Z";
  const render = (generatedAt: string) =>
    renderHome(
      {
        leaderboard: {
          totalTracked: 13263,
          generatedAt,
          entries: [
            {
              rank: 1,
              tag: "#2PP",
              name: "Player 1",
              clan: "",
              trophies: 5000,
              // The server's flag stays fresh; only the clock decides.
              freshness: { state: "fresh", observedAt, ageSeconds: 0 },
              state: "available",
              resetRank: null,
              todayChange: null,
              recentDays: [],
            },
          ],
        },
        query: "",
        error: null,
        search: null,
      },
      "",
    );
  expect(await render("2026-10-04T04:10:00Z")).not.toContain("more than 10 minutes old");
  expect(await render("2026-10-04T04:10:01Z")).toContain(
    "1 of 1 more than 10 minutes old",
  );
});

function visitor(address?: string) {
  const context = new RouterContextProvider();
  context.set(clientAddressContext, address);
  return context;
}

describe("search loading", () => {
  beforeEach(() => {
    mocks.createPythonClient.mockReset();
    clearPublicRefreshLimits();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows a failed search as an error", async () => {
    mocks.createPythonClient.mockReturnValue({
      searchPlayers: vi
        .fn()
        .mockRejectedValue(new PythonApiError(429, { error: "rate_limited" })),
    });
    const result = await searchLoader({
      request: new Request("https://clashlens.example/search?q=Nova"),
      context: visitor("203.0.113.9"),
    } as never);
    expect("search" in result && result.search).toBeNull();
    expect("error" in result && result.error?.error.code).toBe("rate_limited");
  });

  it("gives one visitor 60 searches a minute across the search page and the header box", async () => {
    vi.useFakeTimers({ now: new Date("2026-10-25T12:00:00Z"), toFake: ["Date"] });
    const searchPlayers = vi.fn().mockResolvedValue(search);
    mocks.createPythonClient.mockReturnValue({ searchPlayers });
    const page = (address?: string) =>
      searchLoader({
        request: new Request("https://clashlens.example/search?q=Nova"),
        context: visitor(address),
      } as never);
    const box = (address?: string) =>
      suggestionLoader({
        request: new Request("https://clashlens.example/resources/players/search?q=Nova"),
        context: visitor(address),
      } as never);

    for (let index = 0; index < 30; index += 1) {
      expect(await page("203.0.113.9")).toMatchObject({ search, error: null });
      expect((await box("203.0.113.9")).status).toBe(200);
    }
    expect(searchPlayers).toHaveBeenCalledTimes(60);

    const refused = await box("203.0.113.9");
    expect(refused.status).toBe(429);
    expect(refused.headers.get("Retry-After")).toBe("60");
    expect(await refused.json()).toMatchObject({
      query: "Nova",
      search: null,
      error: { error: { code: "rate_limited", retryAfterSeconds: 60 } },
    });
    expect(await page("203.0.113.9")).toMatchObject({
      data: { search: null, error: { error: { code: "rate_limited" } } },
      init: { status: 429 },
    });
    expect(searchPlayers).toHaveBeenCalledTimes(60);

    // No known address asks nothing; another address has its own allowance.
    expect((await box()).status).toBe(503);
    expect((await box("203.0.113.10")).status).toBe(200);
    expect(searchPlayers).toHaveBeenCalledTimes(61);

    vi.setSystemTime(new Date("2026-10-25T12:01:00Z"));
    expect((await box("203.0.113.9")).status).toBe(200);
    expect(searchPlayers).toHaveBeenCalledTimes(62);
  });

  it("asks nothing for an empty search", async () => {
    const result = await searchLoader({
      request: new Request("https://clashlens.example/search?q=%20"),
    } as never);
    expect(result).toEqual({ search: null, query: "", error: null });
    expect(mocks.createPythonClient).not.toHaveBeenCalled();
  });

  it("forwards an old front-page search link to the search page", async () => {
    await expect(
      frontLoader({
        request: new Request("https://clashlens.example/?q=Nova%20Star"),
      } as never),
    ).rejects.toSatisfy(
      (response: Response) =>
        response.status === 302 &&
        response.headers.get("Location") === "/search?q=Nova+Star",
    );
  });
});

it("submitting a normalized exact tag opens its automatic lookup page", async () => {
  await expect(
    searchLoader({
      request: new Request("https://clashlens.example/search?q=%20%23lqqp%20"),
    } as never),
  ).rejects.toMatchObject({ status: 302 });
  try {
    await searchLoader({
      request: new Request("https://clashlens.example/search?q=%23lqqp"),
    } as never);
  } catch (response) {
    expect((response as Response).headers.get("Location")).toBe("/players/%23LQQP");
  }
});

it("asks the game again when the not-found box sends back the same tag", async () => {
  const handler = createStaticHandler([
    { path: "/", Component: () => createElement(TagSearchAgain, { tag: "#LQQP" }) },
  ]);
  const context = await handler.query(new Request("https://clashlens.example/"));
  if (context instanceof Response) throw new Error("unexpected response");
  const box = renderToString(
    createElement(StaticRouterProvider, {
      router: createStaticRouter(handler.dataRoutes, context),
      context,
      hydrate: false,
    }),
  );
  expect(box).toContain('action="/search" method="get"');
  expect(box).toContain('name="q" value="#LQQP"');
  expect(box).toContain('<input type="hidden" name="recheck" value="#LQQP"/>');
  const opens = (search: string) =>
    searchLoader({
      request: new Request(`https://clashlens.example/search?${search}`),
    } as never).catch((response: Response) => response.headers.get("Location"));
  // The box sends what it holds; a fixed typo is a normal search.
  expect(await opens("q=%23lqqp&recheck=%23LQQP")).toBe("/players/%23LQQP?retry=1");
  expect(await opens("q=%23LQQG&recheck=%23LQQP")).toBe("/players/%23LQQG");
  expect(await opens("q=%23LQQP")).toBe("/players/%23LQQP");
});

it.each([
  [oldDashboard, "/dashboard?player=%232PP", "/?player=%232PP"],
  [oldDashboard, "/dashboard", "/"],
  [oldGroups, "/account/groups", "/account/watchlists"],
  [oldGroups, "/account/groups/add/2PP", "/account/watchlists/add/2PP"],
  [
    oldGroups,
    "/account/groups/6c1e3f8a-2a44-4b7d-9c0e-1f2a3b4c5d6e?days=3",
    "/account/watchlists/6c1e3f8a-2a44-4b7d-9c0e-1f2a3b4c5d6e?days=3",
  ],
])("keeps an old link working: %#", (loader, from, to) => {
  let response: unknown;
  try {
    loader({ request: new Request(`https://clashlens.example${from}`) } as never);
  } catch (thrown) {
    response = thrown;
  }
  expect((response as Response).status).toBe(301);
  expect((response as Response).headers.get("Location")).toBe(to);
});

it("ends with three more things the site does, each one link with its art and a line", async () => {
  const front = { loginAvailable: false, dashboardEnabled: false, error: null };
  const render = async (crewsEnabled: boolean) => {
    const handler = createStaticHandler([
      {
        id: "root",
        path: "/",
        loader: () => ({ crewsEnabled }),
        children: [{ index: true, Component: Front, loader: () => front }],
      },
    ]);
    const context = await handler.query(new Request("https://clashlens.example/"));
    if (context instanceof Response) throw new Error("unexpected response");
    return renderToString(
      createElement(StaticRouterProvider, {
        router: createStaticRouter(handler.dataRoutes, context),
        context,
        hydrate: false,
      }),
    );
  };
  const links = (html: string) =>
    [
      ...html.matchAll(
        /<a href="([^"]+)"[^>]*><span class="unit-art"[^]*?<strong>([^<]+)<\/strong>/g,
      ),
    ].map(([, href, title]) => `${title} ${href}`);
  const all = await render(true);
  expect(links(all)).toEqual([
    "Watchlists /account/watchlists",
    "Crews /crews",
    "Armies /analytics/armies",
  ]);
  // Each led by three close-up portraits, the same kind of art for all three.
  expect(
    [...all.matchAll(/src="[^"]*\/((?:hero|troop)-\d+)\.webp/g)].map(([, art]) => art),
  ).toEqual(
    ["hero-0", "hero-1", "hero-2", "troop-0", "troop-1", "troop-3"].concat([
      "troop-110",
      "troop-12",
      "troop-11",
    ]),
  );
  // Crews switched off: no link to a page that isn't there.
  expect(links(await render(false))).toEqual([
    "Watchlists /account/watchlists",
    "Armies /analytics/armies",
  ]);
});

it("draws the example Legend day as the dashboard does: chip, places, range and heat map", () => {
  const html = renderToString(
    createElement(MemoryRouter, null, createElement(TeaserBoard, { link: false })),
  );
  expect(html).toContain(
    '<span class="teaser-net score">+104<small> today</small></span>',
  );
  // Three places side by side, the projected one with its diamond, over the
  // Next Reset range with the diamond 41% along it.
  expect(html).toMatch(
    /<small>Last Reset<\/small>#1,288<\/span><span class="teaser-now"><small>Now<\/small><b class="rank-mark">#1,188<\/b><\/span><span class="teaser-next"><small><i class="teaser-diamond"><\/i>Projected place<\/small>#1,140/,
  );
  expect(html).toMatch(
    /Next Reset range<\/span>#1,105<i style="--at:41\.17[\d]*%"><\/i>#1,190/,
  );
  // The Season in the shared heat map: 22 played days with their results,
  // the losing ones marked, Today on day 23 and 5 empty days to come.
  expect(html.match(/class="season-heat-played/g)).toHaveLength(22);
  // Every red day lost trophies, so each carries the mark and no gain does.
  expect(html.match(/class="season-heat-played is-loss"/g)).toHaveLength(8);
  for (const cell of html.match(/<li class="season-heat-played[^"]*"[^>]*>.*?<\/li>/g) ??
    []) {
    const red = /--heat:var\(--cl-heat-[01]\)/.test(cell);
    expect(cell.includes("is-loss")).toBe(red);
    expect(cell.includes("is-loss")).toBe(/>−\d/.test(cell));
  }
  expect(html).toMatch(
    /class="season-heat-today"[^>]*>(?:<[^>]+>[^<]*<\/span>)*<span aria-hidden="true">Today<\/span>/,
  );
  expect(html.match(/class="season-heat-future"/g)).toHaveLength(5);
  expect(html).toContain('class="season-heat-key"');
});
