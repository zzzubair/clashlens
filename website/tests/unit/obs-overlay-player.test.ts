import { EMPTY_PLAYER_FACTS, VERIFIED } from "../fixtures/player-route-data";
vi.mock("../../app/services/leaderboard-search.server", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../app/services/leaderboard-search.server")>();
  return {
    ...actual,
    searchLeaderboard: async (...args: Parameters<typeof actual.searchLeaderboard>) =>
      (() => mocks)().searchLeaderboard(...args),
  };
});
vi.mock("../../app/services/players.server", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../app/services/players.server")>();
  return {
    ...actual,
    getPlayer: async (...args: Parameters<typeof actual.getPlayer>) =>
      (() => mocks)().getPlayer(...args),
  };
});
import { beforeEach, expect, it, vi } from "vitest";

import type {
  PlayerPage,
  RankedBattleEvent,
  RankedDaySummary,
} from "../../app/lib/contracts";
import type { OverlayPlayer } from "../../app/lib/obs-overlay";
import { loader } from "../../app/routes/obs-overlay.player";
import { PythonApiError } from "../../app/services/python-response.server";

const mocks = vi.hoisted(() => ({
  getPlayer: vi.fn(),
  searchLeaderboard: vi.fn(),
}));
vi.mock("../../app/services/python.server", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../app/services/python.server")>();
  return { ...actual, createPythonClient: () => mocks };
});

const event = (id: string, trophyChange: number): RankedBattleEvent => ({
  battleId: id,
  battleTimestamp: "2026-10-08T06:00:00Z",
  opponent: { tag: "#9PP", name: "Rival" },
  destructionPercentage: 100,
  stars: 3,
  trophyChange,
  perspectiveDisagreement: false,
});

// A Legend day in progress: 3 attacks recorded for +120, 2 defenses for −80.
function playerWith(tag: string, battlesComplete: boolean): PlayerPage {
  const today = {
    dayNumber: 4,
    state: "Live",
    offense: { attacks: 3, threeStars: 3, trophyGain: 120 },
    defense: { defenses: 2, threeStarsAgainst: 2, trophyLoss: 80 },
    trophyChange: null,
    presentation: { ...VERIFIED, battle_net_complete: battlesComplete ? 40 : null },
    battlesComplete,
    offenseEvents: [event("a", 40), event("aa", 40), event("aaa", 40)],
    defenseEvents: [event("d", -40), event("dd", -40)],
  } as unknown as RankedDaySummary;
  return {
    kind: "player-page",
    ...EMPTY_PLAYER_FACTS,
    tag,
    trackingState: "tracking",
    profile: {
      name: "Nova",
      trophies: 5040,
      freshness: { observedAt: "2026-10-08T12:00:00Z" },
    },
    season: { dayCount: 28 },
    currentDay: today,
  } as unknown as PlayerPage;
}

const overlayFor = async (tag: string) =>
  (
    (await loader({ params: { tag } } as Parameters<typeof loader>[0])) as unknown as {
      data: OverlayPlayer;
    }
  ).data;

beforeEach(() => {
  vi.resetAllMocks();
  mocks.searchLeaderboard.mockResolvedValue({ results: [] });
});

it("leaves the day's totals unknown while a battle may be missing", async () => {
  mocks.getPlayer.mockResolvedValue(playerWith("#2PY", false));
  const overlay = await overlayFor("#2PY");
  expect(overlay.day).toMatchObject({ gained: null, lost: null, net: null });
  expect(overlay.day?.battles).toHaveLength(5);
});

it("gives the day's totals once every battle so far is recorded", async () => {
  mocks.getPlayer.mockResolvedValue(playerWith("#2PL", true));
  const overlay = await overlayFor("#2PL");
  expect(overlay.day).toMatchObject({
    number: 4,
    of: 28,
    gained: 120,
    lost: 80,
    net: 40,
  });
});

it("reads the Rank alongside the player page", async () => {
  let answer!: (page: PlayerPage) => void;
  mocks.getPlayer.mockReturnValue(new Promise((resolve) => (answer = resolve)));
  mocks.searchLeaderboard.mockResolvedValue({ results: [{ tag: "#2PQ", rank: 12 }] });
  const overlay = overlayFor("#2PQ");
  await vi.waitFor(() => expect(mocks.searchLeaderboard).toHaveBeenCalledWith("#2PQ"));
  answer(playerWith("#2PQ", true));
  expect((await overlay).rank).toBe(12);
});

it("ignores a failed Rank read for a player Clash Lens has not seen", async () => {
  mocks.getPlayer.mockRejectedValue(new PythonApiError(404, { error: "not_found" }));
  mocks.searchLeaderboard.mockRejectedValue(new PythonApiError(503, { error: "down" }));
  expect(await overlayFor("#2PR")).toEqual({ tag: "#2PR", state: "missing" });
});
