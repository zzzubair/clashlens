import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PassThrough } from "node:stream";
import { createElement, type ReactElement } from "react";
import { renderToPipeableStream } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  StaticRouterProvider,
} from "react-router";

const mocks = vi.hoisted(() => ({
  createPythonClient: vi.fn(),
  getPlayerLookup: vi.fn(),
  getPastSeasons: vi.fn(),
}));

vi.mock("../../app/services/player-lookup.server", () => ({
  getPlayerLookup: mocks.getPlayerLookup,
  startPlayerLookup: vi.fn(),
}));

vi.mock("../../app/services/python.server", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../app/services/python.server")>();
  return { ...actual, createPythonClient: mocks.createPythonClient };
});

vi.mock("../../app/services/past-seasons.server", () => ({
  getPastSeasons: mocks.getPastSeasons,
}));

import type { PastSeasonFinish, PlayerPage } from "../../app/lib/contracts";
import PlayerRoute, { loader as playerLoader } from "../../app/routes/player";
import { PythonApiError } from "../../app/services/python.server";

const TAG = "#2PP";
const FINISHES: PastSeasonFinish[] = [
  {
    seasonId: "1786338000",
    seasonStart: "2026-08-10T05:00:00+00:00",
    seasonEnd: "2026-09-07T05:00:00+00:00",
    trophies: 5856,
    globalRank: 1,
  },
  {
    seasonId: "2024-07",
    seasonStart: null,
    seasonEnd: null,
    trophies: 5011,
    globalRank: 934651,
  },
  {
    seasonId: "2021-12",
    seasonStart: null,
    seasonEnd: null,
    trophies: 4965,
    globalRank: null,
  },
];

const PLAYER = {
  kind: "player-page",
  tag: TAG,
  trackingState: "tracking",
  profile: {
    tag: TAG,
    name: "Nova",
    clan: "Example",
    trophies: 6000,
    freshness: { state: "fresh", observedAt: "2026-10-04T12:00:00Z", ageSeconds: 0 },
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
    observedAt: "2026-10-04T12:00:00Z",
    freshness: "fresh",
    confidence: "high",
    coverage: "complete",
    version: "v1",
  },
} satisfies PlayerPage;

function loadPage() {
  return playerLoader({
    request: new Request("https://clashlens.example/players/%232PP"),
    params: { tag: TAG },
  } as never);
}

// Renders like a browser visit: the page first, then streamed sections.
async function renderStreamed(data: Awaited<ReturnType<typeof playerLoader>>) {
  const handler = createStaticHandler([
    { path: "/players/:tag", Component: PlayerRoute, loader: () => data },
  ]);
  const context = await handler.query(
    new Request("https://clashlens.example/players/%232PP"),
  );
  if (context instanceof Response) throw new Error("unexpected route response");
  const router = createStaticRouter(handler.dataRoutes, context);
  return renderAll(createElement(StaticRouterProvider, { router, context }));
}

function renderAll(element: ReactElement): Promise<string> {
  return new Promise((resolve, reject) => {
    const sink = new PassThrough();
    let html = "";
    sink.on("data", (chunk) => (html += chunk));
    sink.on("end", () => resolve(html));
    const { pipe } = renderToPipeableStream(element, {
      onAllReady: () => pipe(sink),
      onShellError: reject,
    });
  });
}

describe("past Seasons from ClashKing on the player page", () => {
  beforeEach(() => {
    mocks.createPythonClient.mockReset().mockReturnValue({
      getPlayer: vi.fn().mockResolvedValue(PLAYER),
      getPlayerSeasons: vi.fn().mockResolvedValue([]),
    });
    mocks.getPlayerLookup.mockReset().mockResolvedValue({ tag: TAG, state: "tracking" });
    mocks.getPastSeasons.mockReset();
  });

  it("lists each finish with ClashKing credited and linked", async () => {
    mocks.getPastSeasons.mockResolvedValue(FINISHES);
    const html = await renderStreamed(await loadPage());

    expect(mocks.getPastSeasons).toHaveBeenCalledWith(TAG);
    expect(html).toContain("Past Seasons");
    expect(html).toMatch(/7 Sep 2026<\/th><td>5,856<\/td><td>#1<\/td>/);
    expect(html).toMatch(/Jul 2024<\/th><td>5,011<\/td><td>#934,651<\/td>/);
    expect(html).toMatch(/Dec 2021<\/th><td>4,965<\/td><td>Not recorded<\/td>/);
    expect(html).toMatch(/Source:.*<a href="https:\/\/clashk.ing"[^>]*>ClashKing<\/a>/);
  });

  it("keeps the player page working when ClashKing finishes are unavailable", async () => {
    mocks.getPastSeasons.mockRejectedValue(new PythonApiError(503, { error: "timeout" }));
    const data = await loadPage();
    expect(data.player?.tag).toBe(TAG);
    expect(data.error).toBeNull();
    await expect(data.pastSeasons).resolves.toBeNull();

    const html = await renderStreamed(data);
    expect(html).toContain("Nova");
    expect(html).toContain("Daily Legend log");
    expect(html).not.toContain("Past Seasons");
  });

  it("does not wait for ClashKing before the rest of the page loads", async () => {
    mocks.getPastSeasons.mockReturnValue(new Promise(() => {}));
    const data = await loadPage();
    expect(data.player?.tag).toBe(TAG);
  });

  it("shows no section for a player without ClashKing finishes", async () => {
    mocks.getPastSeasons.mockResolvedValue([]);
    const html = await renderStreamed(await loadPage());
    expect(html).toContain("Nova");
    expect(html).not.toContain("Past Seasons");
  });

  it("lists a long history in one table", async () => {
    const months = Array.from({ length: 12 }, (_, index) => ({
      seasonId: `2024-${String(12 - index).padStart(2, "0")}`,
      seasonStart: null,
      seasonEnd: null,
      trophies: 5000 + index,
      globalRank: index + 1,
    }));
    mocks.getPastSeasons.mockResolvedValue(months);
    const html = await renderStreamed(await loadPage());
    const section = html.slice(html.indexOf('id="past-seasons-title"'));
    const pastSeasons = section.slice(0, section.indexOf("</section>"));
    expect(pastSeasons).not.toContain("<details");
    expect(pastSeasons.match(/<table/g)?.length).toBe(1);
    expect(pastSeasons).toMatch(/Dec 2024<\/th>.*Feb 2024<\/th>.*Jan 2024<\/th>/);
  });
});

describe("past Seasons client boundary", () => {
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
    process.env = { ...saved };
  });

  async function fetchWith(payload: unknown) {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify(payload), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const { getPastSeasons } = await vi.importActual<
      typeof import("../../app/services/past-seasons.server")
    >("../../app/services/past-seasons.server");
    const result = getPastSeasons(TAG);
    return { result, fetchMock };
  }

  it("reads saved finishes from the private API", async () => {
    const { result, fetchMock } = await fetchWith({
      tag: TAG,
      source: "clashking",
      fetched_at: "2026-10-04T12:00:00+00:00",
      seasons: [
        {
          season_id: "1786338000",
          season_start: "2026-08-10T05:00:00+00:00",
          season_end: "2026-09-07T05:00:00+00:00",
          trophies: 5856,
          global_rank: 1,
        },
        {
          season_id: "2021-12",
          season_start: null,
          season_end: null,
          trophies: 4965,
          global_rank: null,
        },
      ],
    });
    await expect(result).resolves.toEqual([FINISHES[0], FINISHES[2]]);
    expect(String(fetchMock.mock.calls[0][0])).toBe(
      "http://python-fixture.test/v1/players/%232PP/past-seasons",
    );
  });

  it.each([
    { tag: "#OTHER", seasons: [] },
    { tag: TAG, seasons: [{ season_id: "2021-12", trophies: -1, global_rank: null }] },
    {
      tag: TAG,
      seasons: [
        {
          season_id: "1786338000",
          season_start: "2026-08-10T06:00:00+00:00",
          season_end: "2026-09-07T05:00:00+00:00",
          trophies: 5856,
          global_rank: 1,
        },
      ],
    },
  ])("refuses a malformed answer", async (payload) => {
    const { result } = await fetchWith(payload);
    await expect(result).rejects.toBeInstanceOf(Error);
  });
});
