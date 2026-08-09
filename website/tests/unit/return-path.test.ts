import { describe, expect, it } from "vitest";

import {
  DEFAULT_RETURN_PATH,
  MAX_RETURN_PATH_LENGTH,
  safeReturnPath,
} from "../../app/server/return-path.server";

const ORIGIN = new URL("https://clashlens.example");

describe("same-origin return path validation", () => {
  it("accepts plain same-origin account and public paths", () => {
    expect(safeReturnPath("/account", ORIGIN)).toBe("/account");
    expect(safeReturnPath("/account/profile", ORIGIN)).toBe("/account/profile");
    expect(safeReturnPath("/account/saved-players", ORIGIN)).toBe(
      "/account/saved-players",
    );
    expect(safeReturnPath("/users/nova_88", ORIGIN)).toBe("/users/nova_88");
    expect(safeReturnPath("/", ORIGIN)).toBe("/");
  });

  it("rejects external, protocol-relative, and non-path values", () => {
    expect(safeReturnPath("https://evil.example/account", ORIGIN)).toBeNull();
    expect(safeReturnPath("http://evil.example/account", ORIGIN)).toBeNull();
    expect(safeReturnPath("//evil.example/account", ORIGIN)).toBeNull();
    expect(safeReturnPath("javascript:alert(1)", ORIGIN)).toBeNull();
    expect(safeReturnPath("account/profile", ORIGIN)).toBeNull();
  });

  it("rejects encoded, backslash, and dot-segment path tricks", () => {
    expect(safeReturnPath("/\\evil.example/account", ORIGIN)).toBeNull();
    expect(safeReturnPath("/%2F%2Fevil.example", ORIGIN)).toBeNull();
    expect(safeReturnPath("/players/%232PP", ORIGIN)).toBeNull();
    expect(safeReturnPath("/a/../b", ORIGIN)).toBeNull();
    expect(safeReturnPath("/a/./b", ORIGIN)).toBeNull();
    expect(safeReturnPath("/a/..", ORIGIN)).toBeNull();
  });

  it("rejects query strings, fragments, spaces, and control characters", () => {
    expect(safeReturnPath("/account?next=/admin", ORIGIN)).toBeNull();
    expect(safeReturnPath("/account#fragment", ORIGIN)).toBeNull();
    expect(safeReturnPath("/account path", ORIGIN)).toBeNull();
    expect(safeReturnPath("/acc\u0000ount", ORIGIN)).toBeNull();
    expect(safeReturnPath("/acc\u0007ount", ORIGIN)).toBeNull();
    expect(safeReturnPath("/\u202eaccount", ORIGIN)).toBeNull();
    expect(safeReturnPath("/caf\u00e9", ORIGIN)).toBeNull();
  });

  it("rejects missing, empty, and oversized values", () => {
    expect(safeReturnPath(null, ORIGIN)).toBeNull();
    expect(safeReturnPath(undefined, ORIGIN)).toBeNull();
    expect(safeReturnPath("", ORIGIN)).toBeNull();
    expect(safeReturnPath(`/${"a".repeat(MAX_RETURN_PATH_LENGTH)}`, ORIGIN)).toBeNull();
  });

  it("keeps the safe default and honors the exact configured origin", () => {
    expect(DEFAULT_RETURN_PATH).toBe("/account");
    const portOrigin = new URL("https://clashlens.example:8443");
    expect(safeReturnPath("/account", portOrigin)).toBe("/account");
  });
});
