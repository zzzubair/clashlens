import { describe, expect, it } from "vitest";

import { dayBar, dayLabel, dayRange, rowScale } from "../../app/components/DayScale";

describe("daily bars' own scale per row", () => {
  it("lets the row's biggest gain or loss reach the band's edge, from zero in the middle", () => {
    // Gains up to +112 and one loss of 150, as a player with one bad day.
    const scale = rowScale([112, 80, 35, -20, -150]);
    expect(scale).toBe(150);
    expect(dayBar(-150, scale)).toBe("50%");
    expect(dayBar(75, scale)).toBe("25%");
  });

  it("gives a quiet row the same full height as a busy one", () => {
    // A player whose best day was +12 fills the band as one with +300 does.
    expect(dayBar(12, rowScale([12, 4, null]))).toBe("50%");
    expect(dayBar(300, rowScale([300, -40]))).toBe("50%");
  });

  it("keeps an empty row dividing", () => {
    expect(rowScale([])).toBe(1);
    expect(rowScale([null, 0])).toBe(1);
    expect(dayBar(0, rowScale([]))).toBe("0%");
  });

  it("names the days once, the month once when they share it", () => {
    expect(dayRange(["2026-10-22T05:00:00Z", "2026-10-28T05:00:00Z"])).toBe("22–28 Oct");
    expect(dayRange(["2026-09-29T05:00:00Z", "2026-10-05T05:00:00Z"])).toBe(
      "29 Sep – 5 Oct",
    );
    expect(dayRange([])).toBeNull();
    expect(dayLabel("2026-09-29T05:00:00Z")).toBe("29 Sep");
  });
});
