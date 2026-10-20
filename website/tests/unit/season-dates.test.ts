import { describe, expect, it } from "vitest";
import { seasonDates } from "../../app/lib/season-end";

describe("Season names", () => {
  it("names a Season by its first and last Legend days", () => {
    expect(seasonDates("1788757200")).toBe("7 Sep – 4 Oct 2026");
    // The next Season starts the day after, so the two share no date.
    expect(seasonDates(1788757200 + 28 * 86_400)).toBe("5 Oct – 1 Nov 2026");
    expect(seasonDates(Date.parse("2025-12-29T05:00:00Z") / 1000)).toBe(
      "29 Dec 2025 – 25 Jan 2026",
    );
    // Its closing Reset is on 1 Jan, but its last Legend day is still 2026.
    expect(seasonDates(Date.parse("2026-12-04T05:00:00Z") / 1000)).toBe(
      "4 Dec – 31 Dec 2026",
    );
    expect(seasonDates("not a season")).toBe("Past Season");
  });
});
