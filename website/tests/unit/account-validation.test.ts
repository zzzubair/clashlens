import { describe, expect, it } from "vitest";

import {
  isInappropriateName,
  MAX_GROUP_TAGS,
  normalizeDisplayName,
  normalizeGroupName,
  normalizeTagList,
  normalizeUsername,
  RESERVED_USERNAMES,
} from "../../app/lib/account-validation";

describe("username early validation", () => {
  it("accepts the Python username shape", () => {
    expect(normalizeUsername("nova")).toBe("nova");
    expect(normalizeUsername("  Nova_88  ")).toBe("nova_88");
    expect(normalizeUsername("a1_b2c3")).toBe("a1_b2c3");
  });

  it("rejects too short, too long, and non-letter-start usernames", () => {
    expect(normalizeUsername("ab")).toBeNull();
    expect(normalizeUsername("a")).toBeNull();
    expect(normalizeUsername("1nova")).toBeNull();
    expect(normalizeUsername("_nova")).toBeNull();
    expect(normalizeUsername("nova-name")).toBeNull();
    expect(normalizeUsername("nova name")).toBeNull();
    expect(normalizeUsername("nova!")).toBeNull();
    expect(normalizeUsername("n".repeat(33))).toBeNull();
    expect(normalizeUsername("Ünïcode")).toBeNull();
  });

  it("rejects every reserved username", () => {
    for (const reserved of RESERVED_USERNAMES) {
      expect(normalizeUsername(reserved), reserved).toBeNull();
      expect(normalizeUsername(reserved.toUpperCase()), reserved).toBeNull();
    }
  });
});

describe("display and group name early validation", () => {
  it("accepts one-to-eighty normalized characters", () => {
    expect(normalizeDisplayName("Nova")).toBe("Nova");
    expect(normalizeDisplayName("  Nova One  ")).toBe("Nova One");
    expect(normalizeDisplayName("Míra")).toBe("Míra");
    expect(normalizeGroupName("My clan team")).toBe("My clan team");
    expect(normalizeDisplayName("n".repeat(80))).toHaveLength(80);
  });

  it("rejects empty, oversized, and control-character names", () => {
    expect(normalizeDisplayName("")).toBeNull();
    expect(normalizeDisplayName("   ")).toBeNull();
    expect(normalizeDisplayName("n".repeat(81))).toBeNull();
    expect(normalizeDisplayName("bad\u0000name")).toBeNull();
    expect(normalizeDisplayName("bad\u0007name")).toBeNull();
    expect(normalizeDisplayName("bad\u202ename")).toBeNull();
    expect(normalizeGroupName("")).toBeNull();
    expect(normalizeGroupName("n".repeat(81))).toBeNull();
  });
});

describe("strict inappropriate-name early feedback", () => {
  it("rejects plain profanity in usernames", () => {
    expect(isInappropriateName("fuck")).toBe(true);
    expect(isInappropriateName("shithead")).toBe(true);
    expect(isInappropriateName("killer_rapist")).toBe(true);
  });

  it("rejects common disguised spellings after normalization", () => {
    expect(isInappropriateName("fuck3r")).toBe(true);
    expect(isInappropriateName("sh1t")).toBe(true);
    expect(isInappropriateName("f@ggot")).toBe(true);
    expect(isInappropriateName("wh0re")).toBe(true);
    expect(isInappropriateName("pr0n")).toBe(true);
    expect(isInappropriateName("g00k")).toBe(true);
    expect(isInappropriateName("a55hole")).toBe(true);
    expect(isInappropriateName("kys_now")).toBe(true);
  });

  it("rejects disguised spellings in display and group names", () => {
    expect(isInappropriateName("Nova the Fvcker")).toBe(true);
    expect(isInappropriateName("my sh*t group")).toBe(true);
  });

  it("does not reject ordinary names", () => {
    expect(isInappropriateName("Nova")).toBe(false);
    expect(isInappropriateName("Clash Heroes")).toBe(false);
    expect(isInappropriateName("legend_pusher_88")).toBe(false);
    expect(isInappropriateName("Míra")).toBe(false);
  });

  it("applies the filter to normalized usernames", () => {
    // The filter operates on the submitted value, before username normalization.
    expect(isInappropriateName("  Fvck3r  ")).toBe(true);
  });
});

describe("player tag list normalization", () => {
  it("normalizes, deduplicates, and sorts tags", () => {
    expect(normalizeTagList([" #2pp ", "#2PL", "#2pp"])).toEqual(["#2PL", "#2PP"]);
  });

  it("rejects an invalid tag in the list", () => {
    expect(normalizeTagList(["#2PP", "not-a-tag"])).toBeNull();
  });

  it("rejects a list over the group limit", () => {
    const many = Array.from({ length: MAX_GROUP_TAGS + 1 }, (_, index) => `#2${index}PP`);
    expect(normalizeTagList(many)).toBeNull();
  });

  it("accepts an empty list", () => {
    expect(normalizeTagList([])).toEqual([]);
  });
});
