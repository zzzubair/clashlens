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
  it("rejects missing status and unknown statuses", () => {
    for (const value of [null, {}, { status: "Complete" }])
      expect(() => mapPresentation(value)).toThrow();
  });
});
