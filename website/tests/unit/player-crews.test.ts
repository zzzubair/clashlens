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
  requestJson: vi.fn(),
}));

vi.mock("../../app/services/player-lookup.server", () => ({
  getPlayerLookup: mocks.getPlayerLookup,
  startPlayerLookup: vi.fn(),
}));

vi.mock("../../app/services/past-seasons.server", () => ({
  getPastSeasons: vi.fn().mockResolvedValue([]),
}));

vi.mock("../../app/services/live-clan.server", () => ({
  getLiveClan: vi.fn().mockResolvedValue(null),
}));

vi.mock("../../app/services/python.server", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../app/services/python.server")>();
  return {
    ...actual,
    createPythonClient: mocks.createPythonClient,
    requestJson: mocks.requestJson,
  };
});

import type { PlayerPage } from "../../app/lib/contracts";
import PlayerRoute, { loader as playerLoader } from "../../app/routes/player";
import "../../app/services/crews.server";
import { PythonApiError } from "../../app/services/python.server";

const TAG = "#2PP";
const CREW_ID = "6f1c2b9e-4a7d-4c1e-9b2a-0d5e8f7a6b3c";
const OTHER_ID = "0b6c1f2e-3d4a-4b5c-8d6e-7f8091a2b3c4";
/** The small crest before a Crew's name: its first letter on a shield. */
const crest = (letter: string) =>
  `<svg class="crest" viewBox="0 0 40 44" aria-hidden="true" focusable="false">.*?>${letter}</text></svg>`;

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

// Only the first HTML sent, which is all a browser without JavaScript shows.
async function renderPlayer(): Promise<string> {
  const data = await playerLoader({
    request: new Request("https://clashlens.example/players/%232PP"),
    params: { tag: TAG },
  } as never);
  const handler = createStaticHandler([
    { path: "/players/:tag", Component: PlayerRoute, loader: () => data },
  ]);
  const context = await handler.query(
    new Request("https://clashlens.example/players/%232PP"),
  );
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

describe("crews on the player page", () => {
  beforeEach(() => {
    vi.stubEnv("CLASHLENS_CREWS_ENABLED", "true");
    // The page's other reads take a moment, so a quick crews answer comes first.
    mocks.createPythonClient.mockReset().mockReturnValue({
      getPlayer: vi.fn().mockResolvedValue(PLAYER),
      getPlayerSeasons: vi.fn(
        () => new Promise((resolve) => setTimeout(() => resolve([]), 20)),
      ),
    });
    mocks.getPlayerLookup.mockReset().mockResolvedValue({ tag: TAG, state: "tracking" });
    mocks.requestJson.mockReset();
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("links each crew the player is in", async () => {
    mocks.requestJson.mockResolvedValue({
      kind: "player-crews",
      tag: TAG,
      crews: [
        { crew_id: CREW_ID, name: "Red Dawn" },
        { crew_id: OTHER_ID, name: "Owls" },
      ],
    });
    const html = await renderPlayer();
    expect(mocks.requestJson.mock.calls[0]?.[0]).toBe("/v1/players/%232PP/crews");
    expect(html).toMatch(
      new RegExp(
        `<span class="player-crews-label">Crews</span><span><a href="/crews/${CREW_ID}"[^>]*>${crest("R")}<bdi>Red Dawn</bdi></a></span>` +
          `<span> · <a href="/crews/${OTHER_ID}"[^>]*>${crest("O")}<bdi>Owls</bdi></a></span>`,
      ),
    );
  });

  it("links them on a page that only knows the tag", async () => {
    mocks.getPlayerLookup.mockResolvedValue({
      tag: TAG,
      state: "tracking",
      reason: "profile_rejected",
    });
    mocks.requestJson.mockResolvedValue({
      kind: "player-crews",
      tag: TAG,
      crews: [{ crew_id: CREW_ID, name: "Red Dawn" }],
    });
    const html = await renderPlayer();
    expect(html).toContain(`<h1>${TAG}</h1>`);
    expect(html).toMatch(
      new RegExp(
        `<span class="player-crews-label">Crew</span><span><a href="/crews/${CREW_ID}"[^>]*>${crest("R")}<bdi>Red Dawn</bdi></a></span>`,
      ),
    );
  });

  it("shows nothing for a player in no crew, or when the list is unavailable", async () => {
    mocks.requestJson.mockResolvedValueOnce({
      kind: "player-crews",
      tag: TAG,
      crews: [],
    });
    expect(await renderPlayer()).not.toContain('class="player-crews"');
    mocks.requestJson.mockRejectedValueOnce(
      new PythonApiError(503, { error: "timeout" }),
    );
    const html = await renderPlayer();
    expect(html).toContain("Nova");
    expect(html).not.toContain('class="player-crews"');
  });

  it("leaves the list out rather than wait for a late answer", async () => {
    mocks.requestJson.mockReturnValue(new Promise(() => {}));
    const html = await renderPlayer();
    expect(html).toContain("Nova");
    expect(html).not.toContain('class="player-crews"');
  });

  it("asks for nothing while crews are switched off", async () => {
    vi.stubEnv("CLASHLENS_CREWS_ENABLED", "");
    const html = await renderPlayer();
    expect(mocks.requestJson).not.toHaveBeenCalled();
    expect(html).not.toContain('class="player-crews"');
  });
});
