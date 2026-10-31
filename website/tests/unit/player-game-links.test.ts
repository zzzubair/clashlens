import { PassThrough } from "node:stream";
import { createElement } from "react";
import { renderToPipeableStream } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  RouterContextProvider,
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
import { clearPublicRefreshLimits } from "../../app/server/abuse.server";
import { clientAddressContext } from "../../app/server/client-address.server";
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
async function renderPlayer(
  streamed = false,
  { tag = TAG, address }: { tag?: string; address?: string } = {},
): Promise<string> {
  const url = `https://clashlens.example/players/${encodeURIComponent(tag)}`;
  const context = new RouterContextProvider();
  context.set(clientAddressContext, address);
  const data = await playerLoader({
    request: new Request(url),
    params: { tag },
    context,
  } as never);
  const handler = createStaticHandler([
    { path: "/players/:tag", Component: PlayerRoute, loader: () => data },
  ]);
  const routed = await handler.query(new Request(url));
  if (routed instanceof Response) throw new Error("unexpected route response");
  const router = createStaticRouter(handler.dataRoutes, routed);
  return new Promise((resolve, reject) => {
    const sink = new PassThrough();
    let html = "";
    sink.on("data", (chunk) => (html += chunk));
    sink.on("end", () => resolve(html.replaceAll("<!-- -->", "")));
    const { pipe, abort } = renderToPipeableStream(
      createElement(StaticRouterProvider, { router, context: routed }),
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
  html.match(/<header class="player-hero"[^>]*>.*?<\/p>/s)?.[0];
// The clan opens with its word, before the name.
const CLAN = '<span class="player-clan"><span class="player-clan-label">Clan</span>';

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

  it("links the tag into the game and shows the saved clan as plain text first", async () => {
    tracked(page("Titans2.0", "#2CLAN"));
    const html = header(await renderPlayer());
    // The name stays plain, so the link's mark is not read as part of it.
    expect(html).toContain("<h1><bdi>VANGUARD</bdi></h1>");
    expect(html).toContain(
      `<a class="game-link player-tag prominent" href="${PLAYER_LINK}" target="_blank" ` +
        `rel="noopener noreferrer" aria-label="Open VANGUARD in Clash of Clans" ` +
        `title="Open in Clash of Clans"><bdi>${TAG}</bdi><svg`,
    );
    expect(html).toContain(`${CLAN}<bdi>Titans2.0</bdi></span>`);
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
    const html = header(await renderPlayer(true, { address: "203.0.113.9" }));
    expect(mocks.getLiveClan).toHaveBeenCalledWith("203.0.113.9", TAG);
    expect(html).toContain(
      `${CLAN}<a class="game-link" href="${CLAN_LINK}" ` +
        `target="_blank" rel="noopener noreferrer" ` +
        `aria-label="Open Titans3.0 in Clash of Clans" title="Open in Clash of Clans">` +
        `<bdi>Titans3.0</bdi><svg`,
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
    expect(html).toContain(`${CLAN}<bdi>Titans2.0</bdi></span>`);
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
    expect(first).toContain(`${CLAN}<bdi>Titans2.0</bdi></span>`);
    expect(first).not.toContain("OpenClanProfile");
    mocks.getLiveClan.mockResolvedValue({ clan: { name: "Titans2.0", tag: "#2CLAN" } });
    const streamed = header(await renderPlayer(true));
    expect(streamed).toContain(`href="${CLAN_LINK}"`);
  });

  it("asks the game for one visitor's live clans six times a minute, keeping the saved clan after", async () => {
    vi.stubEnv("CLASHLENS_PYTHON_API_URL", "http://python-fixture.test/");
    vi.stubEnv(
      "CLASHLENS_PYTHON_HMAC_SECRET_B64",
      "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8",
    );
    clearPublicRefreshLimits();
    const { getLiveClan } = await vi.importActual<
      typeof import("../../app/services/live-clan.server")
    >("../../app/services/live-clan.server");
    mocks.getLiveClan.mockImplementation(getLiveClan);
    const clanReads = vi.fn((input: string | URL | Request) => {
      const tag = decodeURIComponent(String(input).split("/players/")[1].split("/")[0]);
      const clan = { name: "Titans3.0", tag: "#2CLAN" };
      return Promise.resolve(Response.json({ tag, live: true, clan }));
    });
    vi.stubGlobal("fetch", clanReads);
    tracked(page("Titans2.0", "#2CLAN"));
    try {
      const tags = ["#2PP", "#8PY", "#9PY", "#2QQ", "#2LL", "#2GG", "#2RR"];
      const headers = [];
      for (const tag of tags) {
        headers.push(header(await renderPlayer(true, { tag, address: "203.0.113.9" })));
      }
      expect(clanReads).toHaveBeenCalledTimes(6);
      expect(headers[5]).toContain(`href="${CLAN_LINK}"`);
      expect(headers[6]).toContain(PLAYER_LINK);
      expect(headers[6]).toContain(`${CLAN}<bdi>Titans2.0</bdi></span>`);
      expect(headers[6]).not.toContain("OpenClanProfile");

      const other = header(await renderPlayer(true, { address: "203.0.113.10" }));
      expect(clanReads).toHaveBeenCalledTimes(7);
      expect(other).toContain(`href="${CLAN_LINK}"`);
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("shows the live rank and its move since the last Reset, or nothing when the board can't say", async () => {
    // The day that ended at the last Reset, 7 hours before the page was read.
    const yesterday = {
      ...page("Titans2.0", "#2CLAN"),
      seasonDays: [
        {
          dayNumber: 18,
          label: "Day 18",
          period: "2026-10-22T05:00:00Z – 2026-10-23T05:00:00Z",
          state: "Complete",
          resetRank: 173,
          offense: { attacks: 8, threeStars: 6, trophyGain: 300 },
          defense: { defenses: 8, threeStarsAgainst: 4, trophyLoss: 240 },
          trophyChange: 60,
          offenseEvents: [],
          defenseEvents: [],
          completeness: { state: "complete", reason: "" },
          uncertainty: [],
        },
      ],
    } satisfies PlayerPage;
    // The move is the live board's own place at the last Reset, as Rankings
    // shows it.
    const board = (resetRank: number | null) => ({
      entries: [
        { rank: 129, tag: "#2PP", resetRank: 140 },
        { rank: 130, tag: TAG, resetRank },
      ],
    });
    const getTrackedLeaderboard = vi.fn().mockResolvedValue(board(150));
    mocks.createPythonClient.mockReset().mockReturnValue({
      getPlayer: vi.fn().mockResolvedValue(yesterday),
      getPlayerSeasons: vi.fn().mockResolvedValue([]),
      getTrackedLeaderboard,
    });
    const html = await renderPlayer(true);
    expect(getTrackedLeaderboard).toHaveBeenCalledWith(1, "live", 0, undefined, TAG);
    const rank = html.match(/<a class="player-live-rank"[\s\S]*?<\/a>/)?.[0] ?? "";
    // Rank 130 is on Rankings' third page of 50 (101–150).
    expect(rank).toContain(
      'href="/leaderboards/tracked?view=live&amp;page=3&amp;player=%23PC2QRC9QY"',
    );
    expect(rank).toContain(
      'aria-label="Live rank 130, up 20 since the last Reset. Open the Leaderboard"',
    );
    expect(rank).toContain(">#130</strong>");
    // The move says what it is against, as the profile's cards do.
    expect(rank).toContain(
      '>▲20</span><span class="player-live-rank-since">since Reset</span>',
    );

    // With no place at the last Reset on the board, the log's day doesn't
    // stand in: Rankings shows no move either.
    getTrackedLeaderboard.mockResolvedValue(board(null));
    const unplaced = await renderPlayer(true);
    expect(unplaced).toContain('aria-label="Live rank 130. Open the Leaderboard"');
    expect(unplaced).not.toContain("since Reset");

    getTrackedLeaderboard.mockRejectedValue(
      new PythonApiError(404, { error: "missing" }),
    );
    const missing = await renderPlayer(true);
    expect(missing).toContain("VANGUARD");
    expect(missing).not.toContain("player-live-rank");
  });

  it("shows no move on a new Season's first Legend day against the Season before", async () => {
    // Seven hours into the November Season's Day 1. The board at its first
    // Reset belongs to the October Season, so the live board has no Reset
    // place, though the recent log still holds October's Day 28, ranked 173.
    const fresh = {
      state: "fresh" as const,
      observedAt: "2026-11-02T12:00:00Z",
      ageSeconds: 0,
    };
    const dayOne = {
      ...page("Titans2.0", "#2CLAN"),
      profile: { ...page("Titans2.0", "#2CLAN").profile, freshness: fresh },
      provenance: {
        ...page("Titans2.0", "#2CLAN").provenance,
        observedAt: fresh.observedAt,
      },
      season: {
        id: "2026-11",
        anchor: "2026-11-02T05:00:00Z",
        currentDayNumber: 1,
        dayCount: 28,
        anchorSource: "daily_publication",
        anchorObservedAt: "2026-11-02T05:10:00Z",
      },
      recentDays: [
        {
          dayNumber: 28,
          label: "Day 28",
          period: "2026-11-01T05:00:00Z – 2026-11-02T05:00:00Z",
          state: "Complete",
          resetRank: 173,
          offense: { attacks: 8, threeStars: 6, trophyGain: 300 },
          defense: { defenses: 8, threeStarsAgainst: 4, trophyLoss: 240 },
          trophyChange: 60,
          offenseEvents: [],
          defenseEvents: [],
          completeness: { state: "complete", reason: "" },
          uncertainty: [],
        },
      ],
    } satisfies PlayerPage;
    mocks.createPythonClient.mockReset().mockReturnValue({
      getPlayer: vi.fn().mockResolvedValue(dayOne),
      getPlayerSeasons: vi.fn().mockResolvedValue([]),
      getTrackedLeaderboard: vi.fn().mockResolvedValue({
        entries: [{ rank: 130, tag: TAG, resetRank: null }],
      }),
    });
    const rank =
      (await renderPlayer(true)).match(/<a class="player-live-rank"[\s\S]*?<\/a>/)?.[0] ??
      "";
    expect(rank).toContain('aria-label="Live rank 130. Open the Leaderboard"');
    expect(rank).toContain(">#130</strong>");
    expect(rank).not.toContain("▲");
    expect(rank).not.toContain("since Reset");
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
    return { clan: await getLiveClan("203.0.113.9", TAG), fetchMock };
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

  it.each([
    [200, { tag: TAG, live: false, reason: "cap" }, ["cap"]],
    [200, { tag: TAG, live: false, reason: "timeout" }, ["timeout"]],
    [200, { tag: TAG, live: false }, ["unusable_answer"]],
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
    expect(await getLiveClan("203.0.113.9", TAG)).toBeNull();
    expect(loggedReasons(warn)).toEqual(["website_timeout"]);
  });

  function loggedReasons(warn: { mock: { calls: unknown[][] } }) {
    return warn.mock.calls.map(([line]) => JSON.parse(String(line)).reason);
  }

  it("asks nothing for a visitor with no known address", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const { getLiveClan } = await vi.importActual<
      typeof import("../../app/services/live-clan.server")
    >("../../app/services/live-clan.server");
    expect(await getLiveClan(undefined, TAG)).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
