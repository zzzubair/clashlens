import { describe, expect, it } from "vitest";
import cases from "../../../testdata/w6-presentation-parity.json";
import { dayPresentation } from "../../app/lib/player-lookup-text";
import { mapPresentation } from "../../app/services/player-facts.server";

describe("Python's day labels", () => {
  const labels = {
    in_progress: "In progress",
    verified: "Verified",
    calculated: "Calculated",
    uncertain: "Uncertain",
    not_enrolled: "Not enrolled",
  };
  for (const [index, example] of cases
    .filter((c) => c.rule === "day_presentation")
    .entries()) {
    it(`renders the supplied day status ${index}`, () => {
      const presentation = mapPresentation(example.expected);
      expect(dayPresentation({ presentation, state: "Complete" }).status).toBe(
        labels[presentation.status],
      );
    });
  }
  it("gives every cause of an end reading that could not be judged, not one guess", () => {
    const base = cases.find((c) => c.rule === "day_presentation")!.expected as Record<
      string,
      unknown
    >;
    const presentation = mapPresentation({
      ...base,
      status: "calculated",
      is_current: false,
      reason_codes: ["end_reading_unverified"],
    });
    const day = dayPresentation({ presentation, state: "Complete" });
    expect(day.status).toBe("Calculated");
    expect(day.reasons).toEqual([
      "No trophy reading after this day could confirm its end: a battle may still have been landing, an attack's trophies may have shown late, part of a battle log could not be read, or the weekly raise to 5,000 hid the automatic defense loss.",
    ]);
  });
  it("rejects missing status and unknown statuses", () => {
    for (const value of [null, {}, { status: "Complete" }])
      expect(() => mapPresentation(value)).toThrow();
  });
});
