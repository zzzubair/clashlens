import { afterEach, beforeEach, expect, it, vi } from "vitest";

beforeEach(() => {
  vi.resetModules();
  vi.stubEnv("NODE_ENV", "test");
  vi.stubEnv("CLASHLENS_PYTHON_API_URL", "http://python-fixture.test/");
  vi.stubEnv(
    "CLASHLENS_PYTHON_HMAC_SECRET_B64",
    "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8",
  );
  vi.stubEnv("CLASHLENS_PYTHON_HMAC_SECRET_FILE", "");
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

it("signs leaderboard search and preserves a match's whole-board rank", async () => {
  const fetch = vi.fn().mockResolvedValue(
    Response.json({
      exact_tag: null,
      has_more: false,
      results: [{ name: "Nova", tag: "#2PP", rank: 103, trophies: 6000 }],
    }),
  );
  vi.stubGlobal("fetch", fetch);
  const { createPythonClient } = await import("../../app/services/python.server");
  await expect(createPythonClient().searchLeaderboard("Nova")).resolves.toEqual({
    exactTag: null,
    hasMore: false,
    results: [{ name: "Nova", tag: "#2PP", rank: 103, trophies: 6000 }],
  });
  expect(String(fetch.mock.calls[0][0])).toBe(
    "http://python-fixture.test/v1/leaderboards/live/search?q=Nova",
  );
  const headers = new Headers(fetch.mock.calls[0][1].headers);
  expect(headers.get("X-ClashLens-Signature")).toBeTruthy();
});

it.each([0, -1, 1.5, "103", null])("rejects invalid rank %s", async (rank) => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      Response.json({
        exact_tag: null,
        has_more: false,
        results: [{ name: "Nova", tag: "#2PP", rank, trophies: 6000 }],
      }),
    ),
  );
  const { createPythonClient } = await import("../../app/services/python.server");
  await expect(createPythonClient().searchLeaderboard("Nova")).rejects.toMatchObject({
    status: 502,
  });
});

it("rejects an exact tag pointing to another result", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      Response.json({
        exact_tag: "#2PY",
        has_more: false,
        results: [{ name: "Nova", tag: "#2PP", rank: 103, trophies: 6000 }],
      }),
    ),
  );
  const { createPythonClient } = await import("../../app/services/python.server");
  await expect(createPythonClient().searchLeaderboard("2py")).rejects.toMatchObject({
    status: 502,
  });
});

it.each(["", " ", "x".repeat(81)])(
  "rejects invalid input without a private request",
  async (query) => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    const { createPythonClient } = await import("../../app/services/python.server");
    await expect(createPythonClient().searchLeaderboard(query)).rejects.toMatchObject({
      status: 400,
    });
    expect(fetch).not.toHaveBeenCalled();
  },
);

const liveEntry = (position: number, tag: string) => ({
  position,
  tag,
  name: "Nova",
  clan: null,
  trophies: 6000,
  observed_at: "2026-08-06T11:59:00+00:00",
  age_seconds: 60,
  freshness: "fresh",
  confidence: "eligible",
  public_confidence: "high",
  official_rank: null,
});

const livePage = (entries: unknown[]) =>
  Response.json({
    kind: "live",
    generated_at: "2026-08-06T12:00:00+00:00",
    source_observations: {
      oldest_observed_at: "2026-08-06T11:59:00+00:00",
      newest_observed_at: "2026-08-06T11:59:00+00:00",
      stale_count: 0,
    },
    tracked_population: 4,
    total_entries: 4,
    page: 2,
    page_size: 2,
    page_count: 2,
    has_previous: true,
    has_next: false,
    coverage: { state: "partial", tracked_players: 4, measured_percent: 100, note: "" },
    provenance: {
      source: "current accepted profiles",
      observed_at: "2026-08-06T11:59:00+00:00",
      freshness: "fresh",
      confidence: "partial",
      coverage: "partial",
      version: "tracked-trophies-md5-v1",
    },
    quality_states: ["partial"],
    entries,
  });

const neighborEntries = [
  liveEntry(2, "#2PY"),
  liveEntry(3, "#2PP"),
  liveEntry(4, "#2PL"),
];

it("keeps a selected player's neighbor from the previous page", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(livePage(neighborEntries)));
  const { createPythonClient } = await import("../../app/services/python.server");
  const board = await createPythonClient().getTrackedLeaderboard(
    2,
    "live",
    2,
    undefined,
    "#2PP",
  );
  expect(board.page).toBe(2);
  expect(board.entries.map((entry) => entry.rank)).toEqual([2, 3, 4]);
});

it.each([
  ["without a selected player", neighborEntries, undefined],
  ["without the selected player's row", neighborEntries, "#2PQ"],
  ["with a gap in ranks", [liveEntry(2, "#2PY"), liveEntry(4, "#2PP")], "#2PP"],
])("rejects extra rows %s", async (_, entries, focusTag) => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(livePage(entries)));
  const { createPythonClient } = await import("../../app/services/python.server");
  await expect(
    createPythonClient().getTrackedLeaderboard(2, "live", 2, undefined, focusTag),
  ).rejects.toMatchObject({ status: 502 });
});
