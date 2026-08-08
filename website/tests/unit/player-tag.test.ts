import { describe, expect, it } from "vitest";

import { canonicalPlayerPath, normalizePlayerTag } from "../../app/lib/player-tag";

describe("player tag normalization", () => {
  it("trims and uppercases a valid Clash player tag", () => {
    expect(normalizePlayerTag("  #2pp  ")).toBe("#2PP");
  });

  it("rejects a tag with characters outside the Clash tag alphabet", () => {
    expect(normalizePlayerTag("#ABC")).toBeNull();
    expect(normalizePlayerTag("2PP")).toBeNull();
    expect(normalizePlayerTag("#2P")).toBeNull();
    expect(normalizePlayerTag(`#${"2".repeat(16)}`)).toBeNull();
  });

  it("rejects oversized input before applying tag normalization", () => {
    expect(normalizePlayerTag(`#${"2".repeat(1_000_000)}`)).toBeNull();
  });

  it("builds an escaped canonical route", () => {
    expect(canonicalPlayerPath("#2pp")).toBe("/players/%232PP");
  });
});
