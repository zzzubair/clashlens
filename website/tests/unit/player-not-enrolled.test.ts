import { describe, expect, it } from "vitest";
import { presentDay } from "../../app/lib/player-lookup-text";

const DAY = {
  net: null,
  state: "Partial",
  coverage: "partial",
  codes: ["missing_start_baseline", "not_enrolled"],
  attackGain: null,
  defenseLoss: null,
  attacks: 0,
  defenses: 0,
};

describe("days before a late joiner signed up", () => {
  it("say the player was not enrolled instead of a missing result", () => {
    expect(presentDay(DAY, false)).toEqual({
      status: "Not enrolled",
      reasons: ["The player had not signed up for this Season yet."],
      battleNet: null,
    });
  });

  it("leave other incomplete days unchanged", () => {
    expect(presentDay({ ...DAY, codes: ["missing_start_baseline"] }, false).status).toBe(
      "Result unknown",
    );
  });
});
