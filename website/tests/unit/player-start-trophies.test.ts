import { describe, expect, it } from "vitest";
import cases from "../../../testdata/w6-presentation-parity.json";
import { mapDisplayStart } from "../../app/services/player-facts.server";

describe("supplied starting trophies", () => {
  for (const [index, example] of cases
    .filter((c) => c.rule === "display_starts")
    .entries()) {
    it(`reads the original website example ${index}`, () => {
      for (const raw of example.expected as unknown as {
        trophies: number | null;
        source: string;
        calculation: { end_trophies: number; net_change: number } | null;
      }[]) {
        const value = mapDisplayStart(raw);
        expect(value.trophies).toBe(raw.trophies);
        expect(value.calculation?.trophies ?? null).toBe(
          raw.calculation?.end_trophies ?? null,
        );
        expect(value.source).toBe(raw.source);
      }
    });
  }
  it("rejects missing facts and unknown sources instead of calculating a fallback", () => {
    for (const value of [
      undefined,
      {},
      { trophies: 5000, source: "guessed", calculation: null },
    ])
      expect(() => mapDisplayStart(value)).toThrow();
  });
});
