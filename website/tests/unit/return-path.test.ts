import { describe, expect, it } from "vitest";

import {
  createOAuthTransactionCookieValue,
  parseOAuthTransactionCookieValue,
} from "../../app/server/auth-cookies.server";
import {
  createOAuthTransaction,
  MAX_RETURN_PATH_LENGTH,
} from "../../app/server/auth.server";
import { MAX_SEARCH_QUERY_LENGTH } from "../../app/lib/validation";
import {
  accountSetupPath,
  DEFAULT_RETURN_PATH,
  safeReturnPath,
  setupReturnPath,
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

  it("returns a page's data request to the page itself", () => {
    expect(safeReturnPath("/account/groups.data", ORIGIN)).toBe("/account/groups");
    expect(safeReturnPath("/account.data", ORIGIN)).toBe("/account");
    expect(safeReturnPath("/_.data", ORIGIN)).toBe("/");
    expect(safeReturnPath("//evil.example.data", ORIGIN)).toBeNull();
    expect(safeReturnPath("https://evil.example/account.data", ORIGIN)).toBeNull();
    expect(safeReturnPath("/account/groups.data?_routes=root", ORIGIN)).toBe(
      "/account/groups",
    );
    expect(safeReturnPath("/leaderboards/tracked.data?day=12&_routes=root", ORIGIN)).toBe(
      "/leaderboards/tracked?day=12",
    );
  });

  it("rejects external, protocol-relative, and non-path values", () => {
    expect(safeReturnPath("https://evil.example/account", ORIGIN)).toBeNull();
    expect(safeReturnPath("http://evil.example/account", ORIGIN)).toBeNull();
    expect(safeReturnPath("//evil.example/account", ORIGIN)).toBeNull();
    expect(safeReturnPath("javascript:alert(1)", ORIGIN)).toBeNull();
    expect(safeReturnPath("account/profile", ORIGIN)).toBeNull();
  });

  it("keeps an encoded # so a player page is a place to come back to", () => {
    expect(safeReturnPath("/players/%232PP", ORIGIN)).toBe("/players/%232PP");
  });

  it("rejects encoded, backslash, and dot-segment path tricks", () => {
    expect(safeReturnPath("/\\evil.example/account", ORIGIN)).toBeNull();
    expect(safeReturnPath("/%2F%2Fevil.example", ORIGIN)).toBeNull();
    expect(safeReturnPath("/players/%2523", ORIGIN)).toBeNull();
    expect(safeReturnPath("/a/../b", ORIGIN)).toBeNull();
    expect(safeReturnPath("/a/./b", ORIGIN)).toBeNull();
    expect(safeReturnPath("/a/..", ORIGIN)).toBeNull();
  });

  it("keeps a same-site page's query string", () => {
    expect(safeReturnPath("/leaderboards/tracked?day=12", ORIGIN)).toBe(
      "/leaderboards/tracked?day=12",
    );
    expect(safeReturnPath("/search?q=%232PY0LQ&page=2", ORIGIN)).toBe(
      "/search?q=%232PY0LQ&page=2",
    );
    expect(safeReturnPath("/account?next=/admin", ORIGIN)).toBe("/account?next=/admin");
    expect(setupReturnPath("/account/setup?returnPath=%2Fcrews", ORIGIN)).toBe(
      DEFAULT_RETURN_PATH,
    );
    expect(accountSetupPath("/leaderboards/tracked?day=12", ORIGIN)).toBe(
      "/account/setup?returnPath=%2Fleaderboards%2Ftracked%3Fday%3D12",
    );
  });

  it("keeps any query text a browser sends unchanged", () => {
    for (const path of [
      "/search?q=Why?",
      "/search?q=a/b:c@d!e$f(g)h*i+j,k;l=m",
      "/search?q=[x]{y}|z^`\\",
    ])
      expect(safeReturnPath(path, ORIGIN)).toBe(path);
    expect(accountSetupPath("/search?q=Why?", ORIGIN)).toBe(
      "/account/setup?returnPath=%2Fsearch%3Fq%3DWhy%3F",
    );
    expect(setupReturnPath("/search?q=Why?", ORIGIN)).toBe("/search?q=Why?");
  });

  it("rejects unsafe query strings, fragments, spaces, and control characters", () => {
    expect(safeReturnPath("//evil.example?day=1", ORIGIN)).toBeNull();
    expect(safeReturnPath("/\\evil.example?day=1", ORIGIN)).toBeNull();
    expect(safeReturnPath("/account?", ORIGIN)).toBeNull();
    expect(safeReturnPath("/account?day=1#top", ORIGIN)).toBeNull();
    expect(safeReturnPath("/account?day=1 2", ORIGIN)).toBeNull();
    expect(safeReturnPath("/account?day=%zz", ORIGIN)).toBeNull();
    expect(safeReturnPath("/account?q='x'", ORIGIN)).toBeNull();
    expect(safeReturnPath("/account?day=\u0000", ORIGIN)).toBeNull();
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

describe("return paths carrying the longest supported search", () => {
  const KEY = Buffer.alloc(32, 0x24);
  const longest = [
    `/search?${new URLSearchParams({ q: "龍".repeat(MAX_SEARCH_QUERY_LENGTH) })}`,
    `/leaderboards/tracked?${new URLSearchParams({ q: "🐉".repeat(MAX_SEARCH_QUERY_LENGTH / 2), day: "12" })}`,
  ];

  it.each(longest)("survives the guard, sign-in cookie and account setup: %s", (path) => {
    expect(path.length).toBeGreaterThan(200);
    const dataRequest = `${path.replace("?", ".data?")}&_routes=routes%2Fsearch`;
    expect(safeReturnPath(dataRequest, ORIGIN)).toBe(path);

    const now = 1_750_000;
    const cookie = createOAuthTransactionCookieValue(
      createOAuthTransaction(path, now),
      KEY,
    );
    expect(Buffer.byteLength(cookie, "utf8")).toBeLessThan(4096);
    expect(parseOAuthTransactionCookieValue(cookie, KEY, now)?.returnPath).toBe(path);

    expect(setupReturnPath(path, ORIGIN)).toBe(path);
    expect(accountSetupPath(path, ORIGIN)).toBe(
      `/account/setup?returnPath=${encodeURIComponent(path)}`,
    );
    const linking = `/account/verify-player?return=${encodeURIComponent(path)}`;
    expect(safeReturnPath(linking, ORIGIN)).toBe(linking);
  });
});
