import { describe, expect, it } from "vitest";
import { seasonDates } from "../../app/lib/season-end";
describe("Season names from supplied bounds", () => {
  it.each([
    ["2026-09-07T05:00:00Z", "2026-10-05T05:00:00Z", "7 Sep – 4 Oct 2026"],
    ["2026-10-05T05:00:00Z", "2026-11-02T05:00:00Z", "5 Oct – 1 Nov 2026"],
    ["2025-12-29T05:00:00Z", "2026-01-26T05:00:00Z", "29 Dec 2025 – 25 Jan 2026"],
    ["2026-12-04T05:00:00Z", "2027-01-01T05:00:00Z", "4 Dec – 31 Dec 2026"],
  ])("formats %s to %s", (seasonStart, seasonEnd, label) =>
    expect(seasonDates({ seasonStart, seasonEnd })).toBe(label),
  );
  it("does not infer unknown bounds from a Season id", () =>
    expect(seasonDates(null)).toBe("Past Season"));
});
