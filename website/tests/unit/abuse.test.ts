import { beforeEach, describe, expect, it } from "vitest";

import {
  allowAutomaticRefresh,
  allowPublicRefresh,
  allowSearch,
  clearPublicRefreshLimits,
  rateWindowState,
} from "../../app/server/abuse.server";

const NOW = 1_790_000_000_000;

describe("public request rate windows", () => {
  beforeEach(() => clearPublicRefreshLimits());

  it("names a window none, counting or exhausted", () => {
    expect(rateWindowState(undefined, 6, NOW)).toBe("none");
    expect(rateWindowState({ count: 5, resetAt: NOW + 1 }, 6, NOW)).toBe("counting");
    expect(rateWindowState({ count: 6, resetAt: NOW + 1 }, 6, NOW)).toBe("exhausted");
    expect(rateWindowState({ count: 6, resetAt: NOW }, 6, NOW)).toBe("none");
  });

  it("counts up to the limit, refuses until the reset time, then renews", () => {
    for (let index = 0; index < 6; index += 1) {
      expect(allowPublicRefresh("visitor", NOW + index)).toBe(true);
    }
    expect(allowPublicRefresh("visitor", NOW + 10)).toBe(false);
    expect(allowPublicRefresh("visitor", NOW + 59_999)).toBe(false);
    // The window opened at NOW; refused requests did not extend it.
    expect(allowPublicRefresh("visitor", NOW + 60_000)).toBe(true);
    for (let index = 0; index < 5; index += 1) {
      expect(allowPublicRefresh("visitor", NOW + 60_001)).toBe(true);
    }
    expect(allowPublicRefresh("visitor", NOW + 60_002)).toBe(false);
  });

  it("keeps each kind of request and each visitor separate", () => {
    for (let index = 0; index < 3; index += 1) {
      expect(allowAutomaticRefresh("visitor", NOW)).toBe(true);
    }
    expect(allowAutomaticRefresh("visitor", NOW)).toBe(false);
    expect(allowAutomaticRefresh("other", NOW)).toBe(true);
    expect(allowPublicRefresh("visitor", NOW)).toBe(true);
    for (let index = 0; index < 60; index += 1)
      expect(allowSearch("visitor", NOW)).toBe(true);
    expect(allowSearch("visitor", NOW)).toBe(false);
  });

  it("forgets the longest-tracked visitor once 10,000 are tracked", () => {
    for (let index = 0; index < 6; index += 1) allowPublicRefresh("first", NOW);
    expect(allowPublicRefresh("first", NOW)).toBe(false);
    for (let index = 1; index < 10_000; index += 1)
      allowPublicRefresh(`visitor-${index}`, NOW);
    // Opening one more window drops "first", whose allowance starts again.
    expect(allowPublicRefresh("newcomer", NOW)).toBe(true);
    expect(allowPublicRefresh("first", NOW)).toBe(true);
  });
});
