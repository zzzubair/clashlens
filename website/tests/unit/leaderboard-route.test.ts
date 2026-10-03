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

it.each([
  ["2026-10-03T04:55:00Z", false],
  ["2026-10-03T04:30:00Z", false],
  ["2026-10-03T00:00:19Z", true],
] as const)(
  "marks a Daily board whose newest input is %s as incomplete: %s",
  async (newestInput, incomplete) => {
    const fixture = structuredClone(board);
    fixture.view = "daily";
    fixture.daily = {
      officialSeasonId: "1788757200",
      dayNumber: 26,
      resetAt: "2026-10-03T05:00:00Z",
      seasonStartAt: "2026-09-07T05:00:00Z",
      seasonEndAt: "2026-10-05T05:00:00Z",
      previousSnapshot: null,
      nextSnapshot: null,
    };
    fixture.provenance.observedAt = newestInput;
    fixture.entries[0].freshness.observedAt = "2026-10-02T23:59:00Z";
    mocks.getTrackedLeaderboard.mockResolvedValue(fixture);
    const { html } = await render("view=daily&season=1788757200&day=26&page=1");
    expect(html.includes("These standings are incomplete.")).toBe(incomplete);
    if (incomplete) {
      expect(html).toContain("No player updates were saved in the 4 hours before");
      expect(html).toMatch(
        /the newest is from <time[^>]+dateTime="2026-10-03T00:00:19Z"/,
      );
      expect(html.match(/5 hours before Reset/g)).toHaveLength(2);
    } else expect(html).not.toContain("before Reset");
  },
);

it.each([
  [13263, "13,263 tracked players are waiting for their Season reset"],
  [1, "1 tracked player is waiting for their Season reset"],
  [0, null],
] as const)(
  "says how many players wait off the Live board for their Season reset: %s",
  async (seasonResetPending, text) => {
    mocks.getTrackedLeaderboard.mockResolvedValue({
      ...structuredClone(board),
      entries: [],
      totalEntries: 0,
      page: 1,
      pageCount: 0,
      hasPrevious: false,
      sourceObservations: null,
      seasonResetPending,
    });
    const { html } = await render("view=live&page=1");
    expect(html.includes("waiting for their Season reset")).toBe(text !== null);
    if (text) expect(html).toContain(text);
  },
);

it("describes Daily trophies as values saved before the Reset, even with recent inputs", async () => {
  const fixture = structuredClone(board);
  fixture.view = "daily";
  fixture.seasonResetPending = 40;
  fixture.daily = {
    officialSeasonId: "1788757200",
    dayNumber: 28,
    resetAt: "2026-10-05T05:00:00Z",
    seasonStartAt: "2026-09-07T05:00:00Z",
    seasonEndAt: "2026-10-05T05:00:00Z",
    previousSnapshot: null,
    nextSnapshot: null,
  };
  fixture.provenance.observedAt = "2026-10-05T04:59:00Z";
  mocks.getTrackedLeaderboard.mockResolvedValue(fixture);
  const { html } = await render("view=daily&season=1788757200&day=28&page=1");
  expect(html).toContain('id="leaderboard-title">Day 28 standings</h1>');
  expect(html).toContain("last value saved before this Reset");
  expect(html).toContain("7,211");
  expect(html).not.toContain("These standings are incomplete.");
  // The Live board's Season-reset rule never applies to a frozen day.
  expect(html).not.toContain("waiting for their Season reset");
});
