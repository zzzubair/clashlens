import { PassThrough } from "node:stream";
import { createElement } from "react";
import { renderToPipeableStream } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  StaticRouterProvider,
} from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  createPythonClient: vi.fn(),
  getPlayerLookup: vi.fn(),
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

// Only the first HTML sent, which is all a browser without JavaScript shows.
async function renderPlayer(): Promise<string> {
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
          pipe(sink);
          abort();
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
  });

  const tracked = (player: PlayerPage) =>
    mocks.createPythonClient.mockReset().mockReturnValue({
      getPlayer: vi.fn().mockResolvedValue(player),
      getPlayerSeasons: vi.fn().mockResolvedValue([]),
    });

  it("links the name and the clan into the game", async () => {
    tracked(page("Titans2.0", "#2CLAN"));
    const html = header(await renderPlayer());
    expect(html).toContain(
      `<h1><a class="game-link" href="${PLAYER_LINK}" target="_blank" ` +
        `rel="noopener noreferrer" aria-label="Open VANGUARD in Clash of Clans">` +
        `<bdi>VANGUARD</bdi><svg`,
    );
    expect(html).toContain(
      `<span class="player-clan"><a class="game-link" href="${CLAN_LINK}" ` +
        `target="_blank" rel="noopener noreferrer" ` +
        `aria-label="Open Titans2.0 in Clash of Clans"><bdi>Titans2.0</bdi><svg`,
    );
  });

  it("shows no clan link for a player in no clan", async () => {
    tracked(page("", null));
    const html = header(await renderPlayer());
    expect(html).toContain(PLAYER_LINK);
    expect(html).not.toContain("player-clan");
    expect(html).not.toContain("OpenClanProfile");
  });

  it("keeps a clan without a saved tag as plain text", async () => {
    tracked(page("Titans2.0", null));
    const html = header(await renderPlayer());
    expect(html).toContain('<span class="player-clan"><bdi>Titans2.0</bdi></span>');
    expect(html).not.toContain("OpenClanProfile");
  });

  it("links them for a player shown only from their newest profile", async () => {
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
    const html = header(await renderPlayer());
    expect(html).toContain(`href="${PLAYER_LINK}"`);
    expect(html).toContain(`href="${CLAN_LINK}"`);
  });
});
