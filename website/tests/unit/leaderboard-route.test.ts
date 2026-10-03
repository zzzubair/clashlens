import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  StaticRouterProvider,
} from "react-router";
import { beforeEach, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ getTrackedLeaderboard: vi.fn() }));
vi.mock("../../app/services/python.server", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../app/services/python.server")>()),
  createPythonClient: () => mocks,
}));

import Leaderboard, { loader } from "../../app/routes/tracked-leaderboard";
import { PythonApiError } from "../../app/services/python.server";
import type { TrackedLeaderboard } from "../../app/lib/contracts";

const board: TrackedLeaderboard = {
  kind: "tracked-leaderboard",
  view: "live",
  entries: [
    {
      rank: 101,
      tag: "#2PP",
      name: "Nova",
      clan: "Northwind",
      trophies: 7211,
      freshness: {
        state: "stale",
        observedAt: "2026-10-02T11:40:00Z",
        ageSeconds: 1200,
      },
      state: "stale",
      confidence: "high",
    },
  ],
  totalTracked: 150,
  totalEntries: 101,
  page: 2,
  pageSize: 100,
  pageCount: 2,
  generatedAt: "2026-10-02T12:00:00Z",
  hasPrevious: true,
  hasNext: false,
  daily: null,
  coverage: { state: "partial", trackedPlayers: 150, measuredPercent: 67, note: "" },
  provenance: {
    source: "current accepted profiles",
    observedAt: "2026-10-02T11:59:00Z",
    freshness: "stale",
    confidence: "partial",
    coverage: "partial",
    version: "tracked-trophies-md5-v1",
  },
  sourceObservations: {
    newestObservedAt: "2026-10-02T11:59:00Z",
    oldestObservedAt: "2026-10-02T11:30:00Z",
    staleCount: 2,
  },
  qualityStates: ["stale"],
};

async function render(query = "view=live&page=2") {
  const handler = createStaticHandler([
    { path: "/leaderboards/tracked", Component: Leaderboard, loader },
  ]);
  const context = await handler.query(
    new Request(`https://clashlens.example/leaderboards/tracked?${query}`),
  );
  if (context instanceof Response) throw new Error("unexpected redirect");
  const html = renderToString(
    createElement(StaticRouterProvider, {
      router: createStaticRouter(handler.dataRoutes, context),
      context,
      hydrate: false,
    }),
  ).replaceAll("<!-- -->", "");
  return { html, status: context.statusCode };
}

beforeEach(() => {
  mocks.getTrackedLeaderboard.mockReset().mockResolvedValue(structuredClone(board));
});

it("explains tracked ranks and distinguishes whole-board times from the row's confirmation", async () => {
  const { html } = await render();
  expect(html).toContain('id="leaderboard-title">Live Leaderboard</h1>');
  expect(html).toContain("position among players tracked by Clash Lens");
  expect(html).toContain("not the official global rank");
  expect(html).toContain("fixed order based on player tags");
  expect(html).toContain("even if it was unchanged");
  expect(html).toMatch(/Newest player update: <time[^>]+dateTime="2026-10-02T11:59:00Z"/);
  expect(html).toMatch(/Oldest player update: <time[^>]+dateTime="2026-10-02T11:30:00Z"/);
  expect(html).toContain("Across the whole leaderboard");
  expect(html).toContain("101 listed · 150 tracked players");
  expect(html).toContain('<span class="rank-mark">101</span>');
  expect(html).toMatch(/<summary>Last updated 20 minutes ago/);
  expect(html).toContain("Over 10 min old");
  expect(html).toMatch(/<details[^>]*>.*dateTime="2026-10-02T11:40:00Z".*<\/details>/);
  expect(mocks.getTrackedLeaderboard).toHaveBeenCalledWith(
    100,
    "live",
    100,
    undefined,
    undefined,
  );
});

it.each([
  ["2026-10-02T11:50:00Z", "10 minutes ago", false],
  ["2026-10-02T11:49:59Z", "10 minutes ago", true],
  ["2026-10-02T11:58:00Z", "2 minutes ago", false],
] as const)(
  "measures a row updated at %s from its timestamp at page load",
  async (observedAt, age, stale) => {
    const fixture = structuredClone(board);
    fixture.entries[0].freshness = { ...fixture.entries[0].freshness, observedAt };
    mocks.getTrackedLeaderboard.mockResolvedValue(fixture);
    const { html } = await render();
    expect(html).toContain(`<summary>Last updated ${age}`);
    expect(html).toContain(`${age}${stale ? " · Over 10 min old" : ""}</span>`);
    expect(html.includes("Over 10 min old")).toBe(stale);
  },
);

it("formats large ranks and explains why some tracked players are not listed", async () => {
  mocks.getTrackedLeaderboard.mockResolvedValue({
    ...structuredClone(board),
    entries: [{ ...board.entries[0], rank: 11801 }],
    totalTracked: 13263,
    totalEntries: 11854,
  });
  const { html } = await render();
  expect(html).toContain("Ranks 11,801–11,801 · 11,854 listed · 13,263 tracked players");
  expect(html).toContain(
    "Tracked players are listed once Clash Lens confirms their current profile.",
  );
});

it("shows an empty message without a table or impossible pagination", async () => {
  mocks.getTrackedLeaderboard.mockResolvedValue({
    ...board,
    entries: [],
    totalEntries: 0,
    page: 1,
    pageCount: 0,
    hasPrevious: false,
    sourceObservations: null,
  });
  const { html, status } = await render("view=live&page=1");
  expect(status).toBe(200);
  expect(html).toContain("No standings available yet");
  expect(html).not.toContain("<table");
  expect(html).not.toContain("Page 1 of 0");
  expect(html).not.toContain('aria-label="Leaderboard pages"');
  expect(html).not.toContain("Newest player update");
});

it.each([
  ["view=live&page=999", "/leaderboards/tracked?view=live&amp;page=1"],
  [
    "view=daily&season=1785714000&day=21&page=999",
    "/leaderboards/tracked?view=daily&amp;season=1785714000&amp;day=21&amp;page=1",
  ],
])(
  "offers page one for an unavailable page, preserving its view: %s",
  async (query, href) => {
    mocks.getTrackedLeaderboard.mockRejectedValue(new PythonApiError(404, {}));
    const { html, status } = await render(query);
    expect(status).toBe(404);
    expect(html).toContain("This standings page is unavailable");
    expect(html).toContain(`href="${href}"`);
    expect(html).toContain("Go to page 1");
    expect(html).not.toContain("<table");
  },
);

it("does not mislabel service failures as missing standings", async () => {
  mocks.getTrackedLeaderboard.mockRejectedValue(new PythonApiError(503, {}));
  const { html } = await render();
  expect(html).toContain("Leaderboard unavailable");
  expect(html).not.toContain("No standings available yet");
  expect(html).not.toContain("Go to page 1");
});
