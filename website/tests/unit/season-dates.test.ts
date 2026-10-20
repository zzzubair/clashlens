import { describe, expect, it } from "vitest";
import { seasonDates } from "../../app/lib/season-end";

describe("Season names", () => {
  it("names a Season by its dates, Reset to Reset", () => {
    expect(seasonDates("1788757200")).toBe("7 Sep – 5 Oct 2026");
    expect(seasonDates(Date.parse("2025-12-29T05:00:00Z") / 1000)).toBe(
      "29 Dec 2025 – 26 Jan 2026",
    );
    expect(seasonDates("not a season")).toBe("Past Season");
  });
});
