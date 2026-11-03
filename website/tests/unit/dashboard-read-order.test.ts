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
    getPlayer: vi.fn(waiting),
    getTrackedLeaderboard: vi.fn(waiting),
    getPlayerSeasons: vi.fn(waiting),
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
