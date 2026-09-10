import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  createPythonClient: vi.fn(),
}));

vi.mock("../../app/services/python.server", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../app/services/python.server")>();
  return { ...actual, createPythonClient: mocks.createPythonClient };
});

import type {
  HistoricalSeasonSummary,
  SummarizedSeasonRef,
} from "../../app/lib/contracts";
import { PythonApiError } from "../../app/services/python.server";
import { loader as playerLoader } from "../../app/routes/player";

const TAG = "#2PP";
const SEASON = "1785714000";

const SUMMARY: HistoricalSeasonSummary = {
  kind: "player-season-summary",
  tag: TAG,
  seasonId: SEASON,
  seasonStart: "2026-05-01T05:00:00+00:00",
  seasonEnd: "2026-05-29T05:00:00+00:00",
  startTrophies: 6000,
  endTrophies: 6280,
  finalRank: null,
  attackCount: 56,
  attackGain: 840,
  defenseCount: 28,
  defenseLoss: 560,
  netTrophyChange: 280,
  attackStars: { "0": 0, "1": 0, "2": 28, "3": 28 },
  defenseStars: { "0": 0, "1": 28, "2": 0, "3": 0 },
  attackStarsUnknown: 0,
  defenseStarsUnknown: 0,
  daysObserved: 28,
  daysMissing: [],
  coverageState: "partial",
  unresolvedFlags: [],
  dailyEntries: [],
  publishedAt: "2026-05-29T06:00:00+00:00",
};

const SEASONS: SummarizedSeasonRef[] = [
  { seasonId: SEASON, coverageState: "partial", daysObserved: 28, daysMissing: 0 },
];

function requestFor(season: string | null) {
  const target = season === null ? "/players/%232PP" : `/players/%232PP?season=${season}`;
  return new Request(`https://clashlens.example${target}`);
}

describe("player route historical independence", () => {
  beforeEach(() => {
    mocks.createPythonClient.mockReset();
  });

  it("returns the compact season even when the current profile is unavailable", async () => {
    mocks.createPythonClient.mockReturnValue({
      getPlayer: vi.fn().mockRejectedValue(new PythonApiError(404, { error: "missing" })),
      getPlayerSeasons: vi.fn().mockResolvedValue(SEASONS),
      getPlayerSeason: vi.fn().mockResolvedValue(SUMMARY),
    });
    const data = await playerLoader({
      request: requestFor(SEASON),
      params: { tag: "#2PP" },
    } as never);
    expect(data.player).toBeNull();
    expect(data.error).not.toBeNull();
    expect(data.selectedSeason).toBe(SEASON);
    expect(data.historical).toMatchObject({ seasonId: SEASON, attackCount: 56 });
    expect(data.historicalError).toBeNull();
  });

  it("reports an unavailable season without substituting live detail", async () => {
    mocks.createPythonClient.mockReturnValue({
      getPlayer: vi.fn().mockRejectedValue(new PythonApiError(404, { error: "missing" })),
      getPlayerSeasons: vi.fn().mockResolvedValue([]),
      getPlayerSeason: vi
        .fn()
        .mockRejectedValue(new PythonApiError(404, { error: "season_not_found" })),
    });
    const data = await playerLoader({
      request: requestFor("no-such-season"),
      params: { tag: "#2PP" },
    } as never);
    expect(data.player).toBeNull();
    expect(data.historical).toBeNull();
    expect(data.historicalError).not.toBeNull();
  });

  it.each(["", ".data"])(
    "reads the canonical page without a redirect loop (suffix %s)",
    async (suffix) => {
      const getPlayerSeason = vi.fn();
      mocks.createPythonClient.mockReturnValue({
        getPlayer: vi
          .fn()
          .mockRejectedValue(new PythonApiError(404, { error: "missing" })),
        getPlayerSeasons: vi.fn().mockResolvedValue([]),
        getPlayerSeason,
      });
      const data = await playerLoader({
        request: new Request(requestFor(null).url + suffix),
        params: { tag: "#2PP" },
      } as never);
      expect(data.selectedSeason).toBeNull();
      expect(data.historical).toBeNull();
      expect(data.historicalError).toBeNull();
      expect(getPlayerSeason).not.toHaveBeenCalled();
    },
  );
});
