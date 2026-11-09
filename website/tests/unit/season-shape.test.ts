import { describe, expect, it } from "vitest";
import cases from "../../../testdata/w6-presentation-parity.json";
import { mapShape } from "../../app/services/player-facts.server";
import { gapLabel, logGaps, runLines } from "../../app/lib/season-shape";

describe("supplied Season membership", () => {
  for (const [index, example] of cases.filter((c) => c.rule === "shape_of").entries()) {
    it(`renders the shared membership answer ${index} without rebuilding it`, () => {
      const raw = example.expected as {
        last_legend_day_start: string | null;
        kinds: string[];
      };
      const shape = mapShape(
        raw,
        raw.last_legend_day_start ? [{ start: raw.last_legend_day_start }] : [],
      );
      expect(shape!.kinds).toEqual(raw.kinds);
      expect(runLines(shape!)).toBeInstanceOf(Array);
      for (const gap of logGaps(shape!))
        expect(gapLabel(shape!, gap)).toMatch(/Not in Legend I|not signed up/);
    });
  }
  it("keeps no Season context distinct from an empty Season", () => {
    expect(mapShape(null, [])).toBeNull();
    expect(() => mapShape(undefined, [])).toThrow();
  });
  it("rejects an unknown membership kind", () => {
    const raw = cases.find((c) => c.rule === "shape_of")!.expected;
    expect(() =>
      mapShape({ ...(raw as Record<string, unknown>), kinds: ["invented"] }, []),
    ).toThrow();
  });
});
