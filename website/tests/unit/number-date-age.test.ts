import { expect, it } from "vitest";

import { elapsedUnits, formatAge, shortAge, updatedAgo } from "../../app/lib/age-format";
import { dayRange, utcDate } from "../../app/lib/date-format";
import { dayBar, rowScale } from "../../app/lib/day-bars";
import { legendDayDate } from "../../app/lib/legend-log-data";
import {
  formatCount,
  formatSigned,
  signedAverage,
  signedNumber,
  signedOr,
  signedSide,
} from "../../app/lib/number-format";
import { formatNet } from "../../app/lib/profile-format";
import { rankMovement } from "../../app/lib/rank-movement";

it("keeps the different signs, missing-value words and precision used by each view", () => {
  expect(signedNumber(1234)).toBe("+1,234");
  expect(signedNumber(-1234)).toBe("−1,234");
  expect(signedNumber(0)).toBe("0");
  expect(signedNumber(0, { zero: "±0" })).toBe("±0");
  expect(signedNumber(Number.NaN)).toBe("0");
  expect(signedNumber(Number.NaN, { zero: "±0" })).toBe("±0");
  expect(signedNumber(-0.04, { digits: 1 })).toBe("−0.0");
  expect(signedOr(-0.04, 1)).toBe("0.0");
  expect(signedOr(null)).toBe("–");
  expect(signedOr(Infinity)).toBe("–");
  expect(formatCount(null)).toBe("Unknown");
  expect(formatSigned(null)).toBe("Unknown");
  expect(formatSigned(-1234)).toBe("−1,234");
  expect(signedSide(null, -1)).toBe("Unavailable");
  expect(signedSide(0, -1, 1)).toBe("0");
  expect(signedSide(1.25, -1, 1)).toBe("−1.3");
  expect(signedAverage(0, true, 1)).toBe("0.0");
  expect(signedAverage(1.25, true, 1)).toBe("−1.3");
  expect(formatNet(-1234)).toBe("-1234");
  expect(formatNet(null)).toBe("Unknown");
});

it("keeps UTC labels, September spelling and same-month and cross-month ranges", () => {
  expect(utcDate("2026-09-01T00:30:00+02:00")).toBe("31 Aug");
  expect(utcDate("2026-09-01", { weekday: true })).toBe("Tue 1 Sep");
  expect(legendDayDate("2026-09-01 – 2026-09-02")).toBe("1 Sep 2026");
  expect(legendDayDate("invalid")).toBe("Date unavailable");
  expect(dayRange(["2026-09-22", "2026-09-28"])).toBe("22–28 Sep");
  expect(dayRange(["2026-09-29", "2026-10-05"])).toBe("29 Sep – 5 Oct");
  expect(dayRange([])).toBeNull();
});

it("keeps long and short ages and the overlay's rounding distinct", () => {
  expect(formatAge(59)).toBe("less than 1 minute");
  expect(formatAge(60)).toBe("1 minute");
  expect(shortAge(60)).toBe("1 min");
  expect(formatAge(3600)).toBe("1 hour");
  expect(formatAge(86400)).toBe("1 day");
  expect(elapsedUnits(119).minutes).toBe(1);
  expect(elapsedUnits(119, "round").minutes).toBe(2);
  expect(elapsedUnits(5400, "round").hours).toBe(2);
  expect(updatedAgo(0, 119 * 1000)).toBe("updated 1 min ago");
  expect(updatedAgo(0, 119 * 60000)).toBe("updated 119 min ago");
  expect(updatedAgo(0, 120 * 60000)).toBe("updated 2 h ago");
  expect(updatedAgo(null, 0)).toBeNull();
});

it("keeps gain-bar minimums and unknown, unchanged and falling rank comparisons", () => {
  expect(rowScale([0.5, null])).toBe(1);
  expect(dayBar(0.5, rowScale([0.5], 0))).toBe("50%");
  expect(rankMovement(4, null)).toBeNull();
  expect(rankMovement(4, 4)).toBe(0);
  expect(rankMovement(4, 2)).toBe(-2);
});
