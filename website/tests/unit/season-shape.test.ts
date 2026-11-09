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
  it("words a Monday exit and a Wednesday sign-up as before", () => {
    // Days 1-7 in Legend I, dropped at the 12 October Reset, promoted back at
    // the 19 October Reset, signed up Wednesday 21 October (shared example 6).
    const raw = cases.filter((c) => c.rule === "shape_of")[6].expected as {
      last_legend_day_start: string;
      kinds: string[];
    };
    const shape = mapShape(raw, [{ start: raw.last_legend_day_start }])!;
    expect(runLines(shape)).toEqual([
      "Days 1–7 · dropped to Legend II at the Mon 12 Oct Reset",
      "From Day 15 · promoted at the Mon 19 Oct Reset · signed up Wed 21 Oct (Day 17)",
    ]);
    expect(logGaps(shape).map((gap) => gapLabel(shape, gap))).toEqual([
      "Mon 12 Oct – Sun 18 Oct · Not in Legend I, not tracked",
      "Mon 19 Oct – Tue 20 Oct · In Legend I, not signed up yet · 5,000",
    ]);
  });
  it("words a drop that lasts to now as before", () => {
    // Days 1-7 in Legend I, dropped at the 12 October Reset, still out on
    // Day 24 (shared example 10).
    const raw = cases.filter((c) => c.rule === "shape_of")[10].expected as {
      last_legend_day_start: string;
      kinds: string[];
      outside_now: boolean;
    };
    expect(raw.outside_now).toBe(true);
    const shape = mapShape(raw, [{ start: raw.last_legend_day_start }])!;
    expect(runLines(shape)).toEqual([
      "Days 1–7 · dropped to Legend II at the Mon 12 Oct Reset",
    ]);
    expect(logGaps(shape).map((gap) => gapLabel(shape, gap))).toEqual([
      "Mon 12 Oct – now · Not in Legend I, not tracked",
    ]);
  });
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
