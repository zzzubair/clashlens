import { describe, expect, it } from "vitest";
import { leftLine, parseLeftLegendI } from "../../app/lib/left-legend";

describe("a player who left Legend I", () => {
  it("reads the Reset they left at and the total they left on", () => {
    const left = parseLeftLegendI({
      left_at: "2026-10-12T05:00:00+00:00",
      last_day_number: 7,
      trophies: 4684,
    });
    expect(left).toEqual({
      leftAt: "2026-10-12T05:00:00+00:00",
      lastDayNumber: 7,
      trophies: 4684,
    });
    expect(leftLine(left!)).toBe(
      "Left Legend I at the Mon 12 Oct Reset · last 4,684 trophies",
    );
    expect(leftLine({ ...left!, trophies: null })).toBe(
      "Left Legend I at the Mon 12 Oct Reset",
    );
  });

  it("drops an answer it cannot read", () => {
    expect(parseLeftLegendI(undefined)).toBeNull();
    expect(
      parseLeftLegendI({ left_at: "soon", last_day_number: 7, trophies: 1 }),
    ).toBeNull();
    expect(
      parseLeftLegendI({
        left_at: "2026-10-12T05:00:00Z",
        last_day_number: -1,
        trophies: 1,
      }),
    ).toBeNull();
  });
});
