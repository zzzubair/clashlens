import { createElement, Fragment } from "react";
import { renderToString } from "react-dom/server";
import { createRoutesStub, Meta, Outlet } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getPlayer: vi.fn(),
  getPlayerLookup: vi.fn(),
}));

vi.mock("../../app/services/python.server", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../app/services/python.server")>();
  return {
    ...actual,
    createPythonClient: () => ({
      getPlayer: mocks.getPlayer,
      getPlayerSeasons: vi.fn().mockResolvedValue([]),
    }),
  };
});

vi.mock("../../app/services/player-lookup.server", () => ({
  getPlayerLookup: mocks.getPlayerLookup,
  startPlayerLookup: vi.fn(),
}));

vi.mock("../../app/services/past-seasons.server", () => ({
  getPastSeasons: vi.fn().mockResolvedValue(null),
}));

import type { PlayerPage } from "../../app/lib/contracts";
import { PythonApiError } from "../../app/services/python.server";
import { loader, meta, type PlayerLoaderData } from "../../app/routes/player";

const TAG = "#2PP";
const ORIGIN = "http://localhost";
const URL_FOR_TAG = `${ORIGIN}/players/%232PP`;

function playerNamed(name: string, tag = TAG): PlayerPage {
  return {
    kind: "player-page",
    tag,
    trackingState: "tracking",
    profile: { tag, name, clan: "Example", trophies: 6123 },
  } as PlayerPage;
}

async function loadFor(tag: string): Promise<PlayerLoaderData> {
  return loader({
    request: new Request(`${ORIGIN}/players/${encodeURIComponent(tag)}`),
    params: { tag },
  } as never);
}

// What a link-preview bot sees: the page's <head> tags as server HTML.
async function headHtml(tag: string): Promise<string> {
  const loaderData = await loadFor(tag);
  const Stub = createRoutesStub([
    {
      id: "root",
      path: "/",
      Component: () =>
        createElement(Fragment, null, createElement(Meta), createElement(Outlet)),
      children: [
        {
          id: "player",
          path: "players/:tag",
          meta: (args) => meta(args as Parameters<typeof meta>[0]),
          Component: () => null,
        },
      ],
    },
  ]);
  return renderToString(
    createElement(Stub, {
      initialEntries: [`/players/${encodeURIComponent(tag)}`],
      hydrationData: { loaderData: { root: null, player: loaderData } },
    }),
  );
}

beforeEach(() => {
  mocks.getPlayer.mockReset();
  mocks.getPlayerLookup.mockReset();
  mocks.getPlayerLookup.mockResolvedValue({ tag: TAG, state: "tracking" });
});

describe("player link previews", () => {
  it("names a saved player with an absolute link and no trophy numbers", async () => {
    mocks.getPlayer.mockResolvedValue(playerNamed("Nova"));
    const tags = meta({ loaderData: await loadFor(TAG) });
    expect(tags).toEqual(
      expect.arrayContaining([
        { title: "Nova (#2PP) · Clash Lens" },
        { property: "og:title", content: "Nova (#2PP)" },
        { property: "og:url", content: URL_FOR_TAG },
        { property: "og:image", content: `${ORIGIN}/images/legend-league.webp` },
        { name: "twitter:card", content: "summary" },
        { tagName: "link", rel: "canonical", href: URL_FOR_TAG },
      ]),
    );
    expect(JSON.stringify(tags)).not.toMatch(/6123|6,123|rss/);
  });

  it("shows only the tag when the player has no saved profile", async () => {
    mocks.getPlayer.mockRejectedValue(new PythonApiError(404, { error: "missing" }));
    mocks.getPlayerLookup.mockResolvedValue({ tag: TAG, state: "not_found" });
    const tags = meta({ loaderData: await loadFor(TAG) });
    expect(tags).toContainEqual({ title: "Player #2PP · Clash Lens" });
    expect(tags).toContainEqual({ property: "og:url", content: URL_FOR_TAG });
  });

  it("names a player known only from the lookup, as the page heading does", async () => {
    mocks.getPlayer.mockRejectedValue(new PythonApiError(404, { error: "missing" }));
    mocks.getPlayerLookup.mockResolvedValue({
      tag: TAG,
      state: "tracking",
      reason: "no_legend_battles",
      profile: { name: "Nova", clan: null, trophies: 5000 },
    });
    const tags = meta({ loaderData: await loadFor(TAG) });
    expect(tags).toEqual(
      expect.arrayContaining([
        { title: "Nova (#2PP) · Clash Lens" },
        { property: "og:title", content: "Nova (#2PP)" },
        { name: "twitter:title", content: "Nova (#2PP)" },
      ]),
    );
  });

  it("uses a renamed player's newest name, never the older saved one", async () => {
    mocks.getPlayer.mockResolvedValue(playerNamed("Old name"));
    mocks.getPlayerLookup.mockResolvedValue({
      tag: TAG,
      state: "tracking",
      reason: "no_legend_battles",
      profile: { name: "New name", clan: null, trophies: 5000 },
    });
    expect(meta({ loaderData: await loadFor(TAG) })).toContainEqual({
      title: "New name (#2PP) · Clash Lens",
    });

    const saved = playerNamed("Old name");
    saved.profile.freshness = { observedAt: "2026-10-01T00:00:00Z" } as never;
    const refreshed = playerNamed("New name");
    refreshed.profile.freshness = { observedAt: "2026-10-02T00:00:00Z" } as never;
    const tags = meta({
      loaderData: {
        requestedTag: TAG,
        player: saved,
        refreshStatus: { kind: "refresh-status", tag: TAG, player: refreshed },
        lookup: { tag: TAG, state: "tracking" },
        origin: ORIGIN,
      } as PlayerLoaderData,
    });
    expect(tags).toContainEqual({ title: "New name (#2PP) · Clash Lens" });
  });

  it("never names a different player than the link asks for", () => {
    const tags = meta({
      loaderData: {
        requestedTag: TAG,
        player: playerNamed("Someone else", "#8QQ"),
        lookup: null,
        origin: ORIGIN,
      } as PlayerLoaderData,
    });
    expect(tags).toContainEqual({ title: "Player #2PP · Clash Lens" });
  });

  it("gives a plain title for an invalid tag or a failed page", async () => {
    expect(meta({ loaderData: await loadFor("not-a-tag") })).toEqual([
      { title: "Player not found · Clash Lens" },
    ]);
    expect(meta({})).toEqual([{ title: "Player not found · Clash Lens" }]);
  });

  it("escapes special characters in names in the served HTML", async () => {
    mocks.getPlayer.mockResolvedValue(playerNamed(`<b>Nova</b> & "Co" ✦`));
    const html = await headHtml(TAG);
    expect(html).toContain(
      "<title>&lt;b&gt;Nova&lt;/b&gt; &amp; &quot;Co&quot; ✦ (#2PP) · Clash Lens</title>",
    );
    expect(html).toContain(
      '<meta property="og:title" content="&lt;b&gt;Nova&lt;/b&gt; &amp; &quot;Co&quot; ✦ (#2PP)"/>',
    );
    expect(html).not.toContain("<b>Nova");
  });
});
