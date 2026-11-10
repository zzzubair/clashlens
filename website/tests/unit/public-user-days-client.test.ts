import { describe, expect, it, vi } from "vitest";
import { CALENDAR } from "../fixtures/player-route-data";

const reply = vi.hoisted(() => ({ value: null as unknown }));
vi.mock("../../app/services/python-transport.server", () => ({
  requestJson: async () => reply.value,
}));

const { getPublicUserDays } = await import("../../app/services/public-user-days.server");

// As Python sends it: one heat-map letter a day, in the same order as the days.
function payload(kinds: unknown) {
  return {
    kind: "public-user-days",
    calendar: CALENDAR,
    season_start: CALENDAR.season_start,
    players: [
      {
        tag: "#2PP",
        reset_position: 12,
        season_weeks: [],
        days: [
          [1, 8, 8, -20, false, true],
          [2, 0, 0, 0, false, true],
          [3, 2, 8, null, false, true],
        ],
        heatmap_day_numbers: [1, 2, 3],
        heatmap_day_kinds: kinds,
      },
    ],
  };
}

describe("public profile days boundary", () => {
  it("reads each day's heat-map cell from its letter", async () => {
    reply.value = payload("psm");
    const days = await getPublicUserDays("owner");
    expect(days.players[0]!.days.map((day) => [day.day, day.kind])).toEqual([
      [1, "played"],
      [2, "shield"],
      [3, "missing"],
    ]);
  });

  it.each([
    ["one letter short", "ps"],
    ["an unknown letter", "psx"],
    ["the older object form", { "1": "played", "2": "shield", "3": "missing" }],
  ])("rejects %s", async (_label, kinds) => {
    reply.value = payload(kinds);
    await expect(getPublicUserDays("owner")).rejects.toMatchObject({ status: 502 });
  });
});
