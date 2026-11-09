const reads = vi.hoisted(() => ({
  getPlayer: vi.fn(),
  getTrackedLeaderboard: vi.fn(),
  getPlayerSeasons: vi.fn(),
}));
vi.mock("../../app/services/players.server", () => ({
  getPlayer: reads.getPlayer,
  getPlayerSeasons: reads.getPlayerSeasons,
}));
vi.mock("../../app/services/leaderboard.server", () => ({
  getTrackedLeaderboard: reads.getTrackedLeaderboard,
}));
import { expect, it, vi } from "vitest";

import type { LinkedPlayerCard } from "../../app/lib/account-contracts";
import { defaultSettings } from "../../app/lib/dashboard";
import { readCards, type CardContext } from "../../app/services/dashboard.server";
import type { PythonClient } from "../../app/services/python.server";

const TAG = "#LQ2V8PJ0";
const IDENTITY = { provider: "google", providerSubject: "11223344556677889900" } as const;

/**
 * A client whose player page never answers, so any other read seen must have
 * started without waiting for it.
 */
function slowPageContext() {
  const waiting = () => new Promise<never>(() => undefined);
  const client = {
    getPlayer: reads.getPlayer.mockReset().mockImplementation(waiting),
    getTrackedLeaderboard: reads.getTrackedLeaderboard
      .mockReset()
      .mockImplementation(waiting),
    getPlayerSeasons: reads.getPlayerSeasons.mockReset().mockImplementation(waiting),
  };
  const ctx: CardContext = {
    client: client as unknown as PythonClient,
    selected: { tag: TAG, name: "Nova", state: "tracking" } as LinkedPlayerCard,
    settings: defaultSettings(),
  };
  return { client, ctx };
}

it("reads the Cutoffs card's live lines without waiting for the player page", async () => {
  const { client, ctx } = slowPageContext();
  readCards(["cutoffs"], ctx, IDENTITY);
  await vi.waitFor(() =>
    expect(client.getTrackedLeaderboard).toHaveBeenCalledTimes(
      ctx.settings.cutoffs.length,
    ),
  );
  ctx.settings.cutoffs.forEach((top, index) =>
    expect(client.getTrackedLeaderboard).toHaveBeenNthCalledWith(
      index + 1,
      1,
      "live",
      top - 1,
      undefined,
    ),
  );
  expect(client.getPlayer).toHaveBeenCalledWith(TAG);
});

it("reads the Rivals card's live board without waiting for the player page", async () => {
  const { client, ctx } = slowPageContext();
  readCards(["rivals-watch"], ctx, IDENTITY);
  await vi.waitFor(() =>
    expect(client.getTrackedLeaderboard).toHaveBeenCalledWith(
      7,
      "live",
      0,
      undefined,
      TAG,
    ),
  );
});

it("reads the Season graph's Season list without waiting for the player page", async () => {
  const { client, ctx } = slowPageContext();
  readCards(["season-graph"], ctx, IDENTITY);
  await vi.waitFor(() => expect(client.getPlayerSeasons).toHaveBeenCalledWith(TAG));
});

it("drops a failed Season list read when the player page is missing", async () => {
  const { client, ctx } = slowPageContext();
  client.getPlayer.mockResolvedValue(null as never);
  client.getPlayerSeasons.mockRejectedValue(new Error("down") as never);
  const cards = readCards(["season-graph"], ctx, IDENTITY);
  await expect(cards["season-graph"]).resolves.toBeNull();
});

it("reads other players' pages for Rivals and Watchlist once each, without their battles", async () => {
  const entry = (rank: number) => ({
    tag: rank === 4 ? TAG : `#P${rank}`,
    name: `P${rank}`,
    rank,
    trophies: 5400 - rank,
    freshness: { observedAt: "2026-11-07T12:00:00Z" },
  });
  const board = { totalEntries: 7, entries: [1, 2, 3, 4, 5, 6, 7].map(entry) };
  const client = {
    getPlayer: reads.getPlayer.mockReset().mockImplementation(async () => null),
    getTrackedLeaderboard: reads.getTrackedLeaderboard
      .mockReset()
      .mockImplementation(async () => board),
    listGroups: vi.fn(async () => ({
      groups: [{ groupId: "mates", name: "Mates", tags: ["#P1", "#Q9"], players: [] }],
      season: "1793595600",
    })),
  };
  const ctx: CardContext = {
    client: client as unknown as PythonClient,
    selected: { tag: TAG, name: "Nova", state: "tracking" } as LinkedPlayerCard,
    settings: { ...defaultSettings(), groupWindow: "season" },
  };
  const cards = readCards(["rivals-watch", "group-watch"], ctx, IDENTITY);
  await Promise.all(Object.values(cards));
  const calls = client.getPlayer.mock.calls.map((call) => call.join(" "));
  expect(calls.sort()).toEqual([
    "#LQ2V8PJ0",
    "#P1 false",
    "#P2 false",
    "#P3 false",
    "#P5 false",
    "#P6 false",
    "#P7 false",
    "#Q9 false",
  ]);
});
