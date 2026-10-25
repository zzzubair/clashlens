import { PassThrough } from "node:stream";
import { createElement } from "react";
import { renderToPipeableStream } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  StaticRouterProvider,
} from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  createPythonClient: vi.fn(),
  getPlayerLookup: vi.fn(),
  getLiveClan: vi.fn(),
}));

vi.mock("../../app/services/live-clan.server", () => ({
  getLiveClan: mocks.getLiveClan,
}));

vi.mock("../../app/services/player-lookup.server", () => ({
  getPlayerLookup: mocks.getPlayerLookup,
  startPlayerLookup: vi.fn(),
}));

vi.mock("../../app/services/past-seasons.server", () => ({
  getPastSeasons: vi.fn().mockResolvedValue([]),
}));

vi.mock("../../app/services/python.server", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../app/services/python.server")>();
  return { ...actual, createPythonClient: mocks.createPythonClient };
});

import type { PlayerPage } from "../../app/lib/contracts";
import {
  clanProfileLink,
  copyArmyLink,
  playerProfileLink,
} from "../../app/lib/game-links";
import PlayerRoute, { loader as playerLoader } from "../../app/routes/player";
import { PythonApiError } from "../../app/services/python.server";

const TAG = "#PC2QRC9QY";
const PLAYER_LINK =
  "https://link.clashofclans.com/en?action=OpenPlayerProfile&amp;tag=%23PC2QRC9QY";
const CLAN_LINK =
  "https://link.clashofclans.com/en?action=OpenClanProfile&amp;tag=%232CLAN";

const page = (clan: string, clanTag: string | null) =>
  ({
    kind: "player-page",
    tag: TAG,
    trackingState: "tracking",
    profile: {
      tag: TAG,
      name: "VANGUARD",
      clan,
      clanTag,
      trophies: 6000,
      freshness: { state: "fresh", observedAt: "2026-10-23T12:00:00Z", ageSeconds: 0 },
      confidence: "high",
      coverage: "complete",
      eligibility: "legend-i",
    },
    season: null,
    currentDay: null,
    recentDays: [],
    seasonDays: [],
    dataQuality: [],
    provenance: {
      source: "api_player_daily_logs",
      observedAt: "2026-10-23T12:00:00Z",
      freshness: "fresh",
      confidence: "high",
      coverage: "complete",
      version: "v1",
    },
  }) satisfies PlayerPage;

// Only the first HTML sent, which is all a browser without JavaScript shows,
// or with `streamed`, the page once everything streaming in has arrived.
async function renderPlayer(streamed = false): Promise<string> {
  const url = `https://clashlens.example/players/${encodeURIComponent(TAG)}`;
  const data = await playerLoader({
    request: new Request(url),
    params: { tag: TAG },
  } as never);
  const handler = createStaticHandler([
    { path: "/players/:tag", Component: PlayerRoute, loader: () => data },
  ]);
  const context = await handler.query(new Request(url));
  if (context instanceof Response) throw new Error("unexpected route response");
  const router = createStaticRouter(handler.dataRoutes, context);
  return new Promise((resolve, reject) => {
    const sink = new PassThrough();
    let html = "";
    sink.on("data", (chunk) => (html += chunk));
    sink.on("end", () => resolve(html.replaceAll("<!-- -->", "")));
    const { pipe, abort } = renderToPipeableStream(
      createElement(StaticRouterProvider, { router, context }),
      {
        onShellReady: () => {
          if (streamed) return;
          pipe(sink);
          abort();
        },
        onAllReady: () => {
          if (streamed) pipe(sink);
        },
        onShellError: reject,
        onError: () => {},
      },
    );
  });
}

const header = (html: string) =>
  html.match(/<header class="player-header">.*?<\/p>/s)?.[0];

describe("in-game links", () => {
  it("opens the player's and the clan's profile by tag, keeping the #", () => {
    expect(playerProfileLink(TAG)).toBe(
      "https://link.clashofclans.com/en?action=OpenPlayerProfile&tag=%23PC2QRC9QY",
    );
    expect(clanProfileLink("#2CLAN")).toBe(
      "https://link.clashofclans.com/en?action=OpenClanProfile&tag=%232CLAN",
    );
    expect(copyArmyLink("u1x2-3")).toBe(
      "https://link.clashofclans.com/en?action=CopyArmy&army=u1x2-3",
    );
  });
});

describe("the player page header", () => {
  beforeEach(() => {
    vi.stubEnv("CLASHLENS_CREWS_ENABLED", "");
    mocks.getPlayerLookup.mockReset().mockResolvedValue({ tag: TAG, state: "tracking" });
    mocks.getLiveClan.mockReset().mockResolvedValue(null);
  });

  const tracked = (player: PlayerPage) =>
    mocks.createPythonClient.mockReset().mockReturnValue({
      getPlayer: vi.fn().mockResolvedValue(player),
      getPlayerSeasons: vi.fn().mockResolvedValue([]),
    });

  it("links the name into the game and shows the saved clan as plain text first", async () => {
    tracked(page("Titans2.0", "#2CLAN"));
    const html = header(await renderPlayer());
    expect(html).toContain(
      `<h1><a class="game-link" href="${PLAYER_LINK}" target="_blank" ` +
        `rel="noopener noreferrer" aria-label="Open VANGUARD in Clash of Clans">` +
        `<bdi>VANGUARD</bdi><svg`,
    );
    expect(html).toContain('<span class="player-clan"><bdi>Titans2.0</bdi></span>');
    expect(html).not.toContain("OpenClanProfile");
  });

  it("shows no clan link for a player in no clan", async () => {
    tracked(page("", null));
    const html = header(await renderPlayer());
    expect(html).toContain(PLAYER_LINK);
    expect(html).not.toContain("player-clan");
    expect(html).not.toContain("OpenClanProfile");
  });

  it("links the clan the game reports now when the saved profile has no clan tag", async () => {
    tracked(page("Titans2.0", null));
    mocks.getLiveClan.mockResolvedValue({ clan: { name: "Titans3.0", tag: "#2CLAN" } });
    const html = header(await renderPlayer(true));
    expect(mocks.getLiveClan).toHaveBeenCalledWith(TAG);
    expect(html).toContain(
      `<span class="player-clan"><a class="game-link" href="${CLAN_LINK}" ` +
        `target="_blank" rel="noopener noreferrer" ` +
        `aria-label="Open Titans3.0 in Clash of Clans"><bdi>Titans3.0</bdi><svg`,
    );
    expect(html).not.toContain("Titans2.0");
  });

  it("shows no clan once the game says the player is in none", async () => {
    tracked(page("Titans2.0", "#2CLAN"));
    mocks.getLiveClan.mockResolvedValue({ clan: null });
    const html = header(await renderPlayer(true));
    expect(html).toContain(PLAYER_LINK);
    expect(html).not.toContain("player-clan");
  });

  it.each([
    ["gives no answer", () => mocks.getLiveClan.mockResolvedValue(null)],
    ["read fails", () => mocks.getLiveClan.mockRejectedValue(new Error("down"))],
  ])("keeps the saved clan as plain text when the game %s", async (_case, answer) => {
    tracked(page("Titans2.0", "#2CLAN"));
    answer();
    const html = header(await renderPlayer(true));
    expect(html).toContain('<span class="player-clan"><bdi>Titans2.0</bdi></span>');
    expect(html).not.toContain("OpenClanProfile");
  });

  it("links the live clan for a player shown only from their newest profile", async () => {
    mocks.createPythonClient.mockReset().mockReturnValue({
      getPlayer: vi.fn().mockRejectedValue(new PythonApiError(404, { error: "missing" })),
      getPlayerSeasons: vi.fn().mockResolvedValue([]),
    });
    mocks.getPlayerLookup.mockResolvedValue({
      tag: TAG,
      state: "not_in_legend",
      profile: {
        name: "VANGUARD",
        clan: "Titans2.0",
        clan_tag: "#2CLAN",
        trophies: 4900,
      },
    });
    const first = header(await renderPlayer());
    expect(first).toContain(`href="${PLAYER_LINK}"`);
    expect(first).toContain('<span class="player-clan"><bdi>Titans2.0</bdi></span>');
    expect(first).not.toContain("OpenClanProfile");
    mocks.getLiveClan.mockResolvedValue({ clan: { name: "Titans2.0", tag: "#2CLAN" } });
    const streamed = header(await renderPlayer(true));
    expect(streamed).toContain(`href="${CLAN_LINK}"`);
  });
});

describe("live clan client boundary", () => {
  const saved = { ...process.env };

  beforeEach(() => {
    vi.resetModules();
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_API_URL = "http://python-fixture.test/";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 =
      "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8";
    delete process.env.CLASHLENS_PYTHON_HMAC_SECRET_FILE;
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    process.env = { ...saved };
  });

  async function readWith(status: number, payload: unknown) {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(payload), { status }));
    vi.stubGlobal("fetch", fetchMock);
    const { getLiveClan } = await vi.importActual<
      typeof import("../../app/services/live-clan.server")
    >("../../app/services/live-clan.server");
    return { clan: await getLiveClan(TAG), fetchMock };
  }

  it("reads the player's clan right now from the private API", async () => {
    const { clan, fetchMock } = await readWith(200, {
      tag: TAG,
      live: true,
      clan: { name: "Titans2.0", tag: "#2CLAN" },
    });
    expect(clan).toEqual({ clan: { name: "Titans2.0", tag: "#2CLAN" } });
    expect(String(fetchMock.mock.calls[0][0])).toBe(
      "http://python-fixture.test/v1/players/%23PC2QRC9QY/clan",
    );
  });

  it("reads a player in no clan", async () => {
    const { clan } = await readWith(200, { tag: TAG, live: true, clan: null });
    expect(clan).toEqual({ clan: null });
  });

  // The private API logs the views it answered without the game itself.
  it.each([
    [200, { tag: TAG, live: false }, []],
    [200, { tag: "#2PP", live: true, clan: null }, ["unusable_answer"]],
    [
      200,
      { tag: TAG, live: true, clan: { name: "Titans2.0", tag: "2CLAN" } },
      ["unusable_answer"],
    ],
    [503, { error: "unavailable" }, ["api_error"]],
  ])("keeps the saved clan for HTTP %i %j", async (status, payload, reasons) => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { clan } = await readWith(status, payload);
    expect(clan).toBeNull();
    expect(loggedReasons(warn)).toEqual(reasons);
  });

  it("keeps the saved clan and logs why when the private API is too slow", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    vi.stubGlobal(
      "fetch",
      vi.fn().mockRejectedValue(new DOMException("too slow", "TimeoutError")),
    );
    const { getLiveClan } = await vi.importActual<
      typeof import("../../app/services/live-clan.server")
    >("../../app/services/live-clan.server");
    expect(await getLiveClan(TAG)).toBeNull();
    expect(loggedReasons(warn)).toEqual(["website_timeout"]);
  });

  function loggedReasons(warn: { mock: { calls: unknown[][] } }) {
    return warn.mock.calls.map(([line]) => JSON.parse(String(line)).reason);
  }
});
