import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  StaticRouterProvider,
} from "react-router";

import type {
  PlayerPage,
  RefreshState,
  WebsiteErrorResponse,
} from "../../app/lib/contracts";
import PlayerRoute, { type loader as playerLoader } from "../../app/routes/player";

const TAG = "#2PP";
const OBSERVED_AT = "2026-08-06T12:00:00Z";
const PLAYER = {
  kind: "player-page",
  tag: TAG,
  trackingState: "tracking",
  profile: {
    tag: TAG,
    name: "Nova",
    clan: "Example",
    trophies: 6000,
    freshness: { state: "fresh", observedAt: OBSERVED_AT, ageSeconds: 0 },
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
    observedAt: OBSERVED_AT,
    freshness: "fresh",
    confidence: "high",
    coverage: "complete",
    version: "v1",
  },
} satisfies PlayerPage;

async function refreshMessage(
  state: RefreshState,
  refreshError: WebsiteErrorResponse | null = null,
) {
  const data: Awaited<ReturnType<typeof playerLoader>> = {
    requestedTag: TAG,
    player: PLAYER,
    error: null,
    refreshStatus: {
      kind: "refresh-status",
      workId: "work_1",
      tag: TAG,
      state,
      progressPercent: state === "complete" ? 100 : 0,
      message: state,
      publishedAt: null,
      player: null,
    },
    refreshError,
    noJsIdempotencyKey: "test-idempotency-key",
    lookup: { tag: TAG, state: "tracking" },
    lookupError: null,
    seasons: [],
    selectedSeason: null,
    historical: null,
    historicalError: null,
  };
  const handler = createStaticHandler([
    { path: "/players/:tag", Component: PlayerRoute, loader: () => data },
  ]);
  const context = await handler.query(
    new Request("https://clashlens.example/players/%232PP"),
  );
  if (context instanceof Response) throw new Error("unexpected route response");
  const router = createStaticRouter(handler.dataRoutes, context);
  const html = renderToString(createElement(StaticRouterProvider, { router, context }));
  return html.split('aria-label="Player refresh"')[1].split("</section>")[0];
}

describe("player refresh message", () => {
  it.each(["failed", "unavailable"] as const)(
    "says %s work could not refresh and saved results are shown",
    async (state) => {
      const message = await refreshMessage(state);
      expect(message).toContain(
        "Couldn&#x27;t refresh right now. Showing saved results.",
      );
      expect(message).not.toContain("Updated.");
    },
  );

  it("says Updated. only once the refresh completed", async () => {
    expect(await refreshMessage("complete")).toContain("Updated.");
    expect(await refreshMessage("queued")).toContain("Refreshing…");
  });

  it("replaces Refreshing… and its progress once the status read failed", async () => {
    const message = await refreshMessage("running", {
      error: { code: "unavailable", message: "Unavailable." },
    });
    expect(message).toContain("Couldn&#x27;t refresh right now. Showing saved results.");
    expect(message).not.toContain("Refreshing…");
    expect(message).not.toContain("<progress");
  });
});
