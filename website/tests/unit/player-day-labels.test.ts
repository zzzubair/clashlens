import { describe, expect, it } from "vitest";
import { presentDay, type DayEvidence } from "../../app/lib/player-lookup-text";

// Every ended day carries one of three labels. Verified: the Reset reading
// matched every part of the calculation. Calculated: the battles add up with
// no gap, but a reading has not confirmed every part yet. Uncertain: no
// number, a disputed or 9th battle, or a possible gap without all 8 of each.
const COMPLETE: DayEvidence = {
  net: 26,
  state: "Complete",
  confidence: "exact",
  coverage: "complete",
  codes: [],
  attackGain: 310,
  defenseLoss: 284,
  attacks: 8,
  defenses: 8,
};

const status = (day: Partial<DayEvidence>) =>
  presentDay({ ...COMPLETE, ...day }, false).status;

describe("ended day labels", () => {
  it("verifies a complete day whose reading matched exactly", () => {
    expect(status({})).toBe("Verified");
  });

  it("calls a complete day with a calculated part, or no saved confidence, calculated", () => {
    expect(status({ confidence: "inferred" })).toBe("Calculated");
    expect(status({ confidence: null })).toBe("Calculated");
  });

  it("calls a day with every battle and no reading calculated", () => {
    expect(
      status({
        state: "Partial",
        confidence: "partial",
        codes: ["missing_end_baseline"],
      }),
    ).toBe("Calculated");
    expect(
      status({
        state: "Partial",
        confidence: "partial",
        coverage: "partial",
        codes: ["missing_start_battle_log_baseline", "missing_start_baseline"],
      }),
    ).toBe("Calculated");
  });

  it("calls a possible gap without all 8 of each uncertain", () => {
    expect(
      status({
        state: "Partial",
        confidence: "partial",
        coverage: "partial",
        codes: ["missing_start_battle_log_baseline"],
        attacks: 7,
      }),
    ).toBe("Uncertain");
  });

  it("calls a 9th battle, a disputed battle or no number uncertain", () => {
    expect(status({ codes: ["attack_count_exceeds_eight"], attacks: 9 })).toBe(
      "Uncertain",
    );
    expect(status({ state: "Partial", codes: ["perspective_disagreement"] })).toBe(
      "Uncertain",
    );
    expect(
      status({ net: null, state: "Partial", codes: ["missing_start_baseline"] }),
    ).toBe("Uncertain");
  });

  it("gives every cause of an end reading that could not be judged, not one guess", () => {
    const day = presentDay(
      {
        ...COMPLETE,
        state: "Partial",
        confidence: "partial",
        codes: ["end_reading_unverified"],
      },
      false,
    );
    expect(day.status).toBe("Calculated");
    expect(day.reasons).toEqual([
      "No trophy reading after this day could confirm its end: a battle may still have been landing, an attack's trophies may have shown late, part of a battle log could not be read, or the weekly raise to 5,000 hid the automatic defense loss.",
    ]);
  });

  it("explains an ended day whose final evidence was never processed", () => {
    const day = presentDay(
      {
        ...COMPLETE,
        net: 12,
        state: "Live",
        coverage: "partial",
        codes: [],
        attacks: 0,
        defenses: 0,
      },
      false,
    );
    expect(day.status).toBe("Uncertain");
    expect(day.reasons).toEqual([
      "Final evidence for this day has not been processed yet.",
    ]);
  });
});
