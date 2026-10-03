import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  requireLogin: vi.fn(),
  requestJson: vi.fn(),
}));

vi.mock("../../app/server/auth-guard.server", () => ({
  requireLogin: mocks.requireLogin,
}));

vi.mock("../../app/services/python.server", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../app/services/python.server")>();
  return { ...actual, requestJson: mocks.requestJson };
});

import { loader } from "../../app/routes/account.groups.$groupId";
import { PythonApiError } from "../../app/services/python.server";
import { mapGroupComparison } from "../../app/lib/group-comparison";

const ORIGIN = "https://clashlens.example";
const GROUP_ID = "6c1e3f8a-2a44-4b7d-9c0e-1f2a3b4c5d6e";
const IDENTITY = { provider: "google", providerSubject: "11223344556677889900" } as const;
const DAYS = [
  "2026-08-03T05:00:00+00:00",
  "2026-08-04T05:00:00+00:00",
  "2026-08-05T05:00:00+00:00",
];

function player(overrides: Record<string, unknown> = {}) {
  return {
    tag: "#2PP",
    name: "Prof",
    you: false,
    in_group: true,
    status: "tracking",
    trophies: 5300,
    observed_at: "2026-08-06T12:00:00+00:00",
    age_seconds: 60,
    freshness: "fresh",
    today: { net: 64, gained: 80, lost: 16, attacks: 2, defenses: 1 },
    day_results: [
      { start: DAYS[0], state: "retired", net: null },
      { start: DAYS[1], state: "partial", net: 10 },
      { start: DAYS[2], state: "correcting", net: 40 },
    ],
    counted_days: 1,
    counted_attacks: 2,
    net: 40,
    net_per_day: 40.0,
    vs_group: null,
    attack: { count: 2, stars: 5, destruction: 180, three_stars: 1, trophies: 56 },
    defense: {
      count: 1,
      stars: 1,
      destruction: 45,
      trophies: 16,
      star_counts: { "0": 0, "1": 1, "2": 0, "3": 0 },
    },
    ...overrides,
  };
}

function payload(players = [player()]) {
  return {
    kind: "group-comparison",
    group_id: GROUP_ID,
    name: "Rivals",
    days: 3,
    day_starts: DAYS,
    today_start: "2026-08-06T05:00:00+00:00",
    generated_at: "2026-08-06T12:00:00+00:00",
    players,
  };
}

function load(path: string) {
  return loader({
    request: new Request(`${ORIGIN}${path}`),
    params: { groupId: path.split("/")[3]?.split("?")[0] },
  } as never) as unknown as Promise<{
    data: Record<string, unknown>;
    init: { status?: number };
  }>;
}

describe("group comparison", () => {
  beforeEach(() => {
    mocks.requireLogin.mockReset().mockResolvedValue(IDENTITY);
    mocks.requestJson.mockReset();
  });

  it("keeps missing results empty instead of zero", () => {
    const comparison = mapGroupComparison(payload());
    expect(comparison?.players[0].days.map((day) => day.net)).toEqual([null, 10, 40]);
    expect(comparison?.players[0].days[0].state).toBe("retired");
    expect(comparison?.players[0].vsGroup).toBeNull();
  });

  it("keeps a player waiting for their Season reset without trophies", () => {
    const waiting = player({ trophies: null, season_reset_pending: true });
    const players = mapGroupComparison(
      payload([waiting, player({ tag: "#8PY" })]),
    )?.players;
    expect(players?.map((p) => [p.trophies, p.seasonResetPending])).toEqual([
      [null, true],
      [5300, false],
    ]);
  });

  it("rejects a response whose days do not match the requested window", () => {
    expect(mapGroupComparison({ ...payload(), days: 7 })).toBeNull();
    expect(mapGroupComparison(payload([player({ day_results: [] })]))).toBeNull();
    expect(mapGroupComparison(payload([player({ status: "inactive" })]))).toBeNull();
  });

  it("reads the signed-in account's group for a supported window only", async () => {
    mocks.requestJson.mockResolvedValue(payload());
    const result = await load(`/account/groups/${GROUP_ID}?days=3`);
    expect(result.data.comparison).not.toBeNull();
    expect(mocks.requestJson).toHaveBeenCalledWith(
      `/v1/account/groups/${GROUP_ID}/comparison?days=3`,
      "GET",
      undefined,
      undefined,
      undefined,
      IDENTITY,
    );
    mocks.requestJson.mockResolvedValue({
      ...payload(),
      days: 7,
      day_starts: [...DAYS, ...DAYS, DAYS[0]],
    });
    const fallback = await load(`/account/groups/${GROUP_ID}?days=999&sort=__proto__`);
    expect(fallback.data.sort).toBe("trophies");
    expect(mocks.requestJson).toHaveBeenLastCalledWith(
      `/v1/account/groups/${GROUP_ID}/comparison?days=7`,
      "GET",
      undefined,
      undefined,
      undefined,
      IDENTITY,
    );
  });

  it("shows another account's group as not found", async () => {
    mocks.requestJson.mockRejectedValue(
      new PythonApiError(404, { error: "group_not_found" }),
    );
    const result = await load(`/account/groups/${GROUP_ID}`);
    expect(result.init.status).toBe(404);
    expect(result.data).toMatchObject({ notFound: true, comparison: null });
  });

  it("explains a group too large to compare", async () => {
    mocks.requestJson.mockRejectedValue(
      new PythonApiError(422, {
        error: "group_too_large",
        detail: "34 players; compare at most 20",
      }),
    );
    const result = await load(`/account/groups/${GROUP_ID}`);
    expect(result.data.tooLarge).toBe("34 players; compare at most 20");
  });

  it("does not call Python for a malformed group ID", async () => {
    const result = await load("/account/groups/not-a-group");
    expect(result.init.status).toBe(404);
    expect(mocks.requestJson).not.toHaveBeenCalled();
  });
});
