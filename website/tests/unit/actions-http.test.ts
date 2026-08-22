import { describe, expect, it } from "vitest";

import {
  DEFAULT_FORM_LIMITS,
  MAX_CALLBACK_PARAMS,
  freshIdempotencyKey,
  isAccountNotFoundError,
  isIdempotencyKey,
  isSameOrigin,
  mapAccountNameError,
  parseBoundedFormData,
  parseCallbackParams,
  parseCookieHeader,
  readLoginIdentity,
  validateAccountNames,
} from "../../app/server/actions.server";
import {
  LOGIN_COOKIE_NAME,
  createLoginCookieValue,
} from "../../app/server/auth-cookies.server";
import { loadWebsiteConfig, type WebsiteConfig } from "../../app/server/config.server";
import { PythonApiError } from "../../app/services/python.server";

const TEST_SECRET = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8";
const IDENTITY = { provider: "google", providerSubject: "11223344556677889900" } as const;
const NOW_SECONDS = Math.floor(Date.now() / 1000);

function testConfig(overrides: Record<string, string> = {}): WebsiteConfig {
  return loadWebsiteConfig({
    NODE_ENV: "test",
    CLASHLENS_LOGIN_ENABLED: "true",
    CLASHLENS_PUBLIC_ORIGIN: "https://clashlens.example",
    CLASHLENS_GOOGLE_CLIENT_ID: "test-client.apps.googleusercontent.com",
    CLASHLENS_GOOGLE_CLIENT_SECRET: "test-client-secret",
    CLASHLENS_DISCORD_CLIENT_ID: "1234567890123456789",
    CLASHLENS_DISCORD_CLIENT_SECRET: "discord-test-secret",
    CLASHLENS_LOGIN_SECRET_B64: TEST_SECRET,
    ...overrides,
  });
}

function requestWithCookie(cookie: string | undefined): Request {
  return new Request("https://clashlens.example/account", {
    headers: cookie === undefined ? {} : { cookie },
  });
}

describe("parseCookieHeader bounds", () => {
  it("returns an empty map for missing, empty, oversized, and pair-overflow headers", () => {
    expect(parseCookieHeader(null)).toEqual(new Map());
    expect(parseCookieHeader(undefined)).toEqual(new Map());
    expect(parseCookieHeader("")).toEqual(new Map());
    expect(parseCookieHeader("; ; ;")).toEqual(new Map());
    expect(parseCookieHeader(`a=1;${"x".repeat(8192)}`).size).toBe(0);
    const sixtyFivePairs = Array.from({ length: 65 }, (_, i) => `k${i}=v`).join(";");
    expect(parseCookieHeader(sixtyFivePairs).size).toBe(0);
  });

  it("parses simple pairs and lets the first occurrence of a duplicate name win", () => {
    const cookies = parseCookieHeader("a=1; b=2");
    expect([...cookies.entries()]).toEqual([
      ["a", "1"],
      ["b", "2"],
    ]);
    expect(parseCookieHeader("a=1; a=2").get("a")).toBe("1");
  });

  it("trims whitespace around names and values and keeps values with '=' inside", () => {
    const cookies = parseCookieHeader("  a  = 1 ; b=c=d ");
    expect(cookies.get("a")).toBe("1");
    expect(cookies.get("b")).toBe("c=d");
  });

  it("skips malformed pairs instead of failing the whole header", () => {
    const cookies = parseCookieHeader("junk; =novalue; good=1");
    expect(cookies.has("junk")).toBe(false);
    expect(cookies.has("")).toBe(false);
    expect(cookies.get("good")).toBe("1");
  });

  it("drops pairs whose name or value exceeds the byte bounds", () => {
    expect(parseCookieHeader(`n=${"v".repeat(4097)}`).size).toBe(0);
    expect(parseCookieHeader(`${"n".repeat(129)}=v`).size).toBe(0);
    const mixed = parseCookieHeader(`${"n".repeat(129)}=v; ok=1`);
    expect(mixed.has("ok")).toBe(true);
    expect(mixed.size).toBe(1);
  });
});

describe("readLoginIdentity", () => {
  it("returns the exact signed identity for a valid cookie and nothing else", () => {
    const config = testConfig();
    const cookie = createLoginCookieValue(IDENTITY, config.loginSecret, NOW_SECONDS);
    const identity = readLoginIdentity(
      requestWithCookie(`${LOGIN_COOKIE_NAME}=${cookie}`),
      config,
    );
    expect(identity).toEqual(IDENTITY);
    expect(Object.keys(identity as object).sort()).toEqual([
      "provider",
      "providerSubject",
    ]);
  });

  it("returns null for a missing, tampered, or expired cookie", () => {
    const config = testConfig();
    const cookie = createLoginCookieValue(IDENTITY, config.loginSecret, NOW_SECONDS);
    const [, signaturePart] = cookie.split(".");
    const forged = `${Buffer.from(
      JSON.stringify({
        v: 1,
        p: "google",
        s: "attacker-subject",
        i: NOW_SECONDS,
        e: NOW_SECONDS + 86_400,
      }),
    ).toString("base64url")}.${signaturePart}`;
    expect(readLoginIdentity(requestWithCookie(undefined), config)).toBeNull();
    expect(
      readLoginIdentity(requestWithCookie(`${LOGIN_COOKIE_NAME}=${forged}`), config),
    ).toBeNull();
    const expired = createLoginCookieValue(
      IDENTITY,
      config.loginSecret,
      NOW_SECONDS - 86_401,
    );
    expect(
      readLoginIdentity(requestWithCookie(`${LOGIN_COOKIE_NAME}=${expired}`), config),
    ).toBeNull();
    expect(
      readLoginIdentity(requestWithCookie("clashlens_login=junk"), config),
    ).toBeNull();
  });

  it("returns null when login is disabled or the secret is not exactly 32 bytes", () => {
    const disabled = testConfig({ CLASHLENS_LOGIN_ENABLED: "false" });
    expect(
      readLoginIdentity(requestWithCookie("clashlens_login=x"), disabled),
    ).toBeNull();
    const shortSecret: WebsiteConfig = {
      ...testConfig(),
      loginSecret: Buffer.alloc(16),
    };
    expect(
      readLoginIdentity(requestWithCookie("clashlens_login=x"), shortSecret),
    ).toBeNull();
  });
});

describe("isSameOrigin", () => {
  const origin = new URL("https://clashlens.example");

  it("accepts the exact Origin and rejects any other Origin", () => {
    const same = new Request("https://clashlens.example/account/setup", {
      method: "POST",
      headers: { Origin: "https://clashlens.example" },
    });
    expect(isSameOrigin(same, origin)).toBe(true);
    for (const hostile of [
      "https://evil.example",
      "http://clashlens.example",
      "https://clashlens.example.evil.test",
      "https://CLASHLENS.EXAMPLE",
      "https://clashlens.example:8443",
    ]) {
      const request = new Request("https://clashlens.example/account/setup", {
        method: "POST",
        headers: { Origin: hostile },
      });
      expect(isSameOrigin(request, origin)).toBe(false);
    }
  });

  it("falls back to the Referer origin only when Origin is absent", () => {
    const byReferer = new Request("https://clashlens.example/account/setup", {
      method: "POST",
      headers: { Referer: "https://clashlens.example/account/setup" },
    });
    expect(isSameOrigin(byReferer, origin)).toBe(true);
    const hostileReferer = new Request("https://clashlens.example/account/setup", {
      method: "POST",
      headers: { Referer: "https://evil.example/account/setup" },
    });
    expect(isSameOrigin(hostileReferer, origin)).toBe(false);
    const malformed = new Request("https://clashlens.example/account/setup", {
      method: "POST",
      headers: { Referer: "not a url" },
    });
    expect(isSameOrigin(malformed, origin)).toBe(false);
  });

  it("rejects requests with neither Origin nor Referer", () => {
    expect(
      isSameOrigin(
        new Request("https://clashlens.example/account/setup", { method: "POST" }),
        origin,
      ),
    ).toBe(false);
  });
});

describe("parseBoundedFormData", () => {
  function formRequest(
    body: string | Uint8Array<ArrayBuffer>,
    headers: Record<string, string> = {},
  ): Request {
    return new Request("https://clashlens.example/account/setup", {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded", ...headers },
      body,
    });
  }

  it("parses a bounded urlencoded body into a duplicate-free string map", async () => {
    const result = await parseBoundedFormData(
      formRequest("username=nova88&displayName=Nova&tag=%23ABC123&space=a+b"),
    );
    expect(result).toEqual({
      username: "nova88",
      displayName: "Nova",
      tag: "#ABC123",
      space: "a b",
    });
  });

  it("accepts content-type parameters and an empty body", async () => {
    const withCharset = formRequest("a=1", {
      "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
    });
    expect(await parseBoundedFormData(withCharset)).toEqual({ a: "1" });
    expect(await parseBoundedFormData(formRequest(""))).toEqual({});
  });

  it("rejects any other content type and requests without a body", async () => {
    const json = new Request("https://clashlens.example/account/setup", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: "{}",
    });
    expect(await parseBoundedFormData(json)).toBeNull();
    const get = new Request("https://clashlens.example/account/setup");
    expect(await parseBoundedFormData(get)).toBeNull();
  });

  it("rejects duplicate field names and empty field names", async () => {
    expect(await parseBoundedFormData(formRequest("a=1&a=2"))).toBeNull();
    expect(await parseBoundedFormData(formRequest("=1"))).toBeNull();
  });

  it("rejects field counts, names, and values beyond the limits", async () => {
    const tooMany = Array.from(
      { length: DEFAULT_FORM_LIMITS.maxFields + 1 },
      (_, i) => `f${i}=1`,
    ).join("&");
    expect(await parseBoundedFormData(formRequest(tooMany))).toBeNull();
    expect(await parseBoundedFormData(formRequest(`${"n".repeat(65)}=1`))).toBeNull();
    expect(
      await parseBoundedFormData(formRequest(`a=${"v".repeat(16 * 1024 + 1)}`)),
    ).toBeNull();
    const overTotal = Array.from(
      { length: DEFAULT_FORM_LIMITS.maxFields },
      (_, i) => `f${i}=${"v".repeat(4096)}`,
    ).join("&");
    expect(await parseBoundedFormData(formRequest(overTotal))).toBeNull();
  });

  it("rejects oversized or malformed declared content lengths", async () => {
    expect(
      await parseBoundedFormData(formRequest("a=1", { "content-length": "abc" })),
    ).toBeNull();
    expect(
      await parseBoundedFormData(
        formRequest("a=1", { "content-length": "999999999999" }),
      ),
    ).toBeNull();
  });

  it("rejects invalid UTF-8 and malformed percent escapes", async () => {
    const invalidUtf8 = new Uint8Array([0x61, 0x3d, 0xff, 0xfe]);
    expect(await parseBoundedFormData(formRequest(invalidUtf8))).toBeNull();
    expect(await parseBoundedFormData(formRequest("a=%"))).toBeNull();
    expect(await parseBoundedFormData(formRequest("a=%ZZ"))).toBeNull();
    expect(await parseBoundedFormData(formRequest("a=%E0%A4%A"))).toBeNull();
  });

  it("honors custom limits", async () => {
    const limits = {
      maxFields: 2,
      maxFieldNameBytes: 8,
      maxFieldValueBytes: 16,
      maxTotalBytes: 64,
    };
    expect(await parseBoundedFormData(formRequest("a=1&b=2"), limits)).toEqual({
      a: "1",
      b: "2",
    });
    expect(await parseBoundedFormData(formRequest("a=1&b=2&c=3"), limits)).toBeNull();
    expect(await parseBoundedFormData(formRequest("longnamex=1"), limits)).toBeNull();
    expect(
      await parseBoundedFormData(formRequest("a=value-too-long-now"), limits),
    ).toBeNull();
  });
});

describe("idempotency keys", () => {
  it("creates fresh canonical UUIDs that differ each call", () => {
    const first = freshIdempotencyKey();
    const second = freshIdempotencyKey();
    expect(isIdempotencyKey(first)).toBe(true);
    expect(isIdempotencyKey(second)).toBe(true);
    expect(first).not.toBe(second);
  });

  it("accepts only canonical lowercase UUID strings", () => {
    const valid = "3be934b5-68fa-4741-8c7b-e03592e4ad70";
    expect(isIdempotencyKey(valid)).toBe(true);
    expect(isIdempotencyKey(valid.toUpperCase())).toBe(false);
    expect(isIdempotencyKey(valid.replace("3", "g"))).toBe(false);
    expect(isIdempotencyKey("not-a-uuid")).toBe(false);
    expect(isIdempotencyKey(42)).toBe(false);
    expect(isIdempotencyKey(null)).toBe(false);
    expect(isIdempotencyKey(undefined)).toBe(false);
  });
});

describe("parseCallbackParams", () => {
  it("parses a bounded callback query with no duplicate names", () => {
    expect(parseCallbackParams("")).toEqual({});
    expect(parseCallbackParams("?code=abc&state=xyz")).toEqual({
      code: "abc",
      state: "xyz",
    });
    expect(parseCallbackParams("code=abc")).toEqual({ code: "abc" });
    expect(parseCallbackParams("?code=a%20b")).toEqual({ code: "a b" });
  });

  it("rejects duplicate parameter names outright", () => {
    expect(parseCallbackParams("state=a&state=b")).toBeNull();
  });

  it("rejects parameter counts and sizes beyond the bounds", () => {
    const tooMany = Array.from(
      { length: MAX_CALLBACK_PARAMS + 1 },
      (_, i) => `k${i}=1`,
    ).join("&");
    expect(parseCallbackParams(tooMany)).toBeNull();
    const exactlyAtLimit = Array.from(
      { length: MAX_CALLBACK_PARAMS },
      (_, i) => `k${i}=1`,
    ).join("&");
    expect(parseCallbackParams(exactlyAtLimit)).not.toBeNull();
    expect(parseCallbackParams(`=${"v"}`)).toBeNull();
    expect(parseCallbackParams(`${"k".repeat(8193)}=1`)).toBeNull();
    const overTotal = `${"a".repeat(8192)}=x&b=y`;
    expect(parseCallbackParams(overTotal)).toBeNull();
  });
});

describe("validateAccountNames", () => {
  it("normalizes valid names without field errors", () => {
    const result = validateAccountNames({
      username: "  Nova88 ",
      displayName: "  Nova  ",
    });
    expect(result).toEqual({
      username: "nova88",
      displayName: "Nova",
      fieldErrors: {},
    });
  });

  it("rejects reserved, malformed, and inappropriate usernames with generic messages", () => {
    expect(
      validateAccountNames({ username: "admin", displayName: "Nova" }).username,
    ).toBeNull();
    expect(
      validateAccountNames({ username: "admin", displayName: "Nova" }).fieldErrors
        .username,
    ).toMatch(/reserved/);
    expect(
      validateAccountNames({ username: "8nova", displayName: "Nova" }).username,
    ).toBeNull();
    expect(
      validateAccountNames({ username: "no space", displayName: "Nova" }).username,
    ).toBeNull();
    const inappropriate = validateAccountNames({
      username: "FuckYou",
      displayName: "Nova",
    });
    expect(inappropriate.username).toBe("fuckyou");
    expect(inappropriate.fieldErrors.username).toBe("Choose a different username.");
  });

  it("strictly filters leetspeak and symbols in display names without echoing values", () => {
    const leet = validateAccountNames({ username: "nova88", displayName: "p3n1s" });
    expect(leet.fieldErrors.displayName).toBe("Choose a different display name.");
    expect(leet.fieldErrors.username).toBeUndefined();
    const symbols = validateAccountNames({ username: "nova88", displayName: "a$$hole" });
    expect(symbols.fieldErrors.displayName).toBe("Choose a different display name.");
  });

  it("rejects empty, oversized, and control-character names", () => {
    expect(
      validateAccountNames({ username: "", displayName: "Nova" }).username,
    ).toBeNull();
    expect(
      validateAccountNames({ username: "nova88", displayName: "" }).displayName,
    ).toBeNull();
    expect(
      validateAccountNames({ username: "nova88", displayName: "x".repeat(81) })
        .displayName,
    ).toBeNull();
    expect(
      validateAccountNames({ username: "nova88", displayName: "bad\u0000name" })
        .displayName,
    ).toBeNull();
    expect(
      validateAccountNames({ username: "nova88", displayName: "bad\u007fname" })
        .displayName,
    ).toBeNull();
  });
});

describe("mapAccountNameError and isAccountNotFoundError", () => {
  it("maps only the documented Python outcomes", () => {
    expect(
      mapAccountNameError(new PythonApiError(409, { error: "account_exists" })),
    ).toEqual({ kind: "account_exists" });
    expect(
      mapAccountNameError(new PythonApiError(403, { error: "account_not_found" })),
    ).toEqual({ kind: "account_not_found" });
    expect(
      mapAccountNameError(new PythonApiError(404, { error: "account_not_found" })),
    ).toEqual({ kind: "account_not_found" });
    expect(
      mapAccountNameError(new PythonApiError(409, { error: "username_unavailable" })),
    ).toMatchObject({ kind: "field", status: 409 });
    expect(mapAccountNameError(new PythonApiError(422, {}))).toMatchObject({
      kind: "field",
      status: 422,
    });
  });

  it("turns every other failure into a general safe website error", () => {
    for (const error of [
      new PythonApiError(403, { error: "forbidden" }),
      new PythonApiError(400, { error: "invalid_input" }),
      new PythonApiError(503, { error: "unavailable" }),
      new Error("boom"),
      "boom",
      null,
      undefined,
      {},
      { payload: { error: "account_not_found" } },
      { status: "403", payload: { error: "account_not_found" } },
    ]) {
      expect(mapAccountNameError(error)).toMatchObject({ kind: "general" });
    }
  });

  it("accepts only the documented account_not_found responses", () => {
    expect(
      isAccountNotFoundError(new PythonApiError(403, { error: "account_not_found" })),
    ).toBe(true);
    expect(
      isAccountNotFoundError(new PythonApiError(404, { error: "account_not_found" })),
    ).toBe(true);
    expect(
      isAccountNotFoundError(new PythonApiError(400, { error: "account_not_found" })),
    ).toBe(false);
    expect(isAccountNotFoundError(new PythonApiError(403, { error: "forbidden" }))).toBe(
      false,
    );
    expect(
      isAccountNotFoundError(new PythonApiError(404, { error: "user_not_found" })),
    ).toBe(false);
    expect(isAccountNotFoundError(new PythonApiError(403, null))).toBe(false);
    expect(isAccountNotFoundError(new Error("boom"))).toBe(false);
    expect(isAccountNotFoundError("boom")).toBe(false);
  });
});
