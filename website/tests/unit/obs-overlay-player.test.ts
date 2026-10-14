import { beforeEach, expect, it, vi } from "vitest";

import type {
  PlayerPage,
  RankedBattleEvent,
  RankedDaySummary,
} from "../../app/lib/contracts";
import type { OverlayPlayer } from "../../app/lib/obs-overlay";

const mocks = vi.hoisted(() => ({
  getPlayer: vi.fn(),
  searchLeaderboard: vi.fn(),
}));
vi.mock("../../app/services/python.server", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../app/services/python.server")>();
  return { ...actual, createPythonClient: () => mocks };
});
import { loader } from "../../app/routes/obs-overlay.player";

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
    battlesComplete,
    offenseEvents: [event("a", 40), event("aa", 40), event("aaa", 40)],
    defenseEvents: [event("d", -40), event("dd", -40)],
  } as unknown as RankedDaySummary;
  return {
    kind: "player-page",
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
