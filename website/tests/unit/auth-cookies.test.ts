import { createHmac } from "node:crypto";

import { describe, expect, it } from "vitest";

import {
  LOGIN_COOKIE_LIFETIME_SECONDS,
  LOGIN_COOKIE_NAME,
  OAUTH_COOKIE_NAME,
  OAUTH_TRANSACTION_LIFETIME_SECONDS,
  buildClearCookieHeader,
  buildSetCookieHeader,
  cookieAttributes,
  createLoginCookieValue,
  createOAuthTransactionCookieValue,
  parseLoginCookieValue,
  parseOAuthTransactionCookieValue,
} from "../../app/server/auth-cookies.server";
import type { LoginIdentity } from "../../app/server/auth-cookies.server";
import { createOAuthTransaction } from "../../app/server/google-oidc.server";

const TEST_SECRET = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8";
const KEY = Buffer.from(TEST_SECRET, "base64url");
const OTHER_KEY = Buffer.alloc(32, 0x42);
const IDENTITY = { provider: "google", providerSubject: "11223344556677889900" } as const;

function oauthTransaction(now = 1_750_000) {
  return createOAuthTransaction("/account", now, (size) => Buffer.alloc(size, 0x2a));
}

describe("login cookie values", () => {
  it("signs a canonical provider+subject payload with a fixed 24-hour lifetime", () => {
    const value = createLoginCookieValue(IDENTITY, KEY, 1_750_000);
    const [payloadPart, signaturePart] = value.split(".");
    const payload = Buffer.from(payloadPart, "base64url").toString("utf8");
    expect(JSON.parse(payload)).toEqual({
      v: 1,
      p: "google",
      s: "11223344556677889900",
      i: 1_750_000,
      e: 1_750_000 + LOGIN_COOKIE_LIFETIME_SECONDS,
    });
    const expected = createHmac("sha256", KEY)
      .update(Buffer.from(payloadPart, "base64url"))
      .digest("base64url");
    expect(signaturePart).toBe(expected);
    expect(value.length).toBeLessThan(512);
  });

  it("round-trips a fresh cookie and rejects tampered values", () => {
    const value = createLoginCookieValue(IDENTITY, KEY, 1_750_000);
    expect(parseLoginCookieValue(value, KEY, 1_750_100)).toEqual(IDENTITY);

    const [payloadPart, signaturePart] = value.split(".");
    const tamperedPayload = Buffer.from(
      JSON.stringify({ ...IDENTITY, providerSubject: "attacker-subject" }),
    ).toString("base64url");
    expect(
      parseLoginCookieValue(`${tamperedPayload}.${signaturePart}`, KEY, 1_750_100),
    ).toBeNull();
    const tamperedSignature = createHmac("sha256", OTHER_KEY)
      .update(Buffer.from(payloadPart, "base64url"))
      .digest("base64url");
    expect(
      parseLoginCookieValue(`${payloadPart}.${tamperedSignature}`, KEY, 1_750_100),
    ).toBeNull();
  });

  it("rejects a cookie signed with a different key", () => {
    const value = createLoginCookieValue(IDENTITY, KEY, 1_750_000);
    expect(parseLoginCookieValue(value, OTHER_KEY, 1_750_100)).toBeNull();
  });

  it("rejects expired cookies at the exact expiry second", () => {
    const value = createLoginCookieValue(IDENTITY, KEY, 1_750_000);
    expect(
      parseLoginCookieValue(value, KEY, 1_750_000 + LOGIN_COOKIE_LIFETIME_SECONDS - 1),
    ).toEqual(IDENTITY);
    expect(
      parseLoginCookieValue(value, KEY, 1_750_000 + LOGIN_COOKIE_LIFETIME_SECONDS),
    ).toBeNull();
  });

  it("rejects values with a non-canonical lifetime or future issuance", () => {
    createLoginCookieValue(IDENTITY, KEY, 1_750_000);
    const make = (payload: Buffer) =>
      `${payload.toString("base64url")}.${createHmac("sha256", KEY)
        .update(payload)
        .digest("base64url")}`;
    const shifted = make(
      Buffer.from(
        JSON.stringify({
          v: 1,
          p: "google",
          s: IDENTITY.providerSubject,
          i: 1_750_000,
          e: 1_750_000 + 3600,
        }),
      ),
    );
    expect(parseLoginCookieValue(shifted, KEY, 1_750_100)).toBeNull();
    const future = make(
      Buffer.from(
        JSON.stringify({
          v: 1,
          p: "google",
          s: IDENTITY.providerSubject,
          i: 1_751_000,
          e: 1_751_000 + LOGIN_COOKIE_LIFETIME_SECONDS,
        }),
      ),
    );
    expect(parseLoginCookieValue(future, KEY, 1_750_100)).toBeNull();
  });

  it("rejects malformed, junk, and oversized values safely", () => {
    expect(parseLoginCookieValue(undefined, KEY, 1_750_100)).toBeNull();
    expect(parseLoginCookieValue(null, KEY, 1_750_100)).toBeNull();
    expect(parseLoginCookieValue("", KEY, 1_750_100)).toBeNull();
    expect(parseLoginCookieValue("not-a-cookie", KEY, 1_750_100)).toBeNull();
    expect(parseLoginCookieValue("a.b.c", KEY, 1_750_100)).toBeNull();
    expect(parseLoginCookieValue("!!!.!!!", KEY, 1_750_100)).toBeNull();
    expect(parseLoginCookieValue("x".repeat(5000), KEY, 1_750_100)).toBeNull();
    expect(parseLoginCookieValue("{}", KEY, 1_750_100)).toBeNull();
    expect(parseLoginCookieValue("e30=.signature", KEY, 1_750_100)).toBeNull();
  });

  it("rejects validly signed values with wrong field shapes", () => {
    createLoginCookieValue(IDENTITY, KEY, 1_750_000);
    const make = (payload: Buffer) =>
      `${payload.toString("base64url")}.${createHmac("sha256", KEY)
        .update(payload)
        .digest("base64url")}`;
    const invalidSubjects = ["", "a b", "x".repeat(129), 42];
    for (const subject of invalidSubjects) {
      const value = make(
        Buffer.from(
          JSON.stringify({
            v: 1,
            p: "google",
            s: subject,
            i: 1_750_000,
            e: 1_750_000 + LOGIN_COOKIE_LIFETIME_SECONDS,
          }),
        ),
      );
      expect(parseLoginCookieValue(value, KEY, 1_750_100)).toBeNull();
    }
    const wrongVersion = make(
      Buffer.from(
        JSON.stringify({
          v: 2,
          p: "google",
          s: IDENTITY.providerSubject,
          i: 1_750_000,
          e: 1_750_000 + LOGIN_COOKIE_LIFETIME_SECONDS,
        }),
      ),
    );
    expect(parseLoginCookieValue(wrongVersion, KEY, 1_750_100)).toBeNull();
  });

  it("requires a 32-byte key and a bounded identity at creation", () => {
    expect(() => createLoginCookieValue(IDENTITY, Buffer.alloc(16), 1_750_000)).toThrow(
      "login cookie key must be exactly 32 bytes",
    );
    expect(() =>
      createLoginCookieValue(
        { provider: "google", providerSubject: "bad subject" },
        KEY,
        1_750_000,
      ),
    ).toThrow("bounded provider subject");
    expect(() =>
      createLoginCookieValue(
        { provider: "github", providerSubject: "x" } as unknown as LoginIdentity,
        KEY,
        1_750_000,
      ),
    ).toThrow("bounded provider subject");
  });
});

describe("OAuth transaction cookie values", () => {
  it("signs every bounded transaction field with the ten-minute lifetime", () => {
    const transaction = oauthTransaction();
    const value = createOAuthTransactionCookieValue(transaction, KEY);
    const payload = JSON.parse(
      Buffer.from(value.split(".")[0], "base64url").toString("utf8"),
    );
    expect(payload).toEqual({
      v: 1,
      pr: "google",
      st: transaction.state,
      no: transaction.nonce,
      ve: transaction.codeVerifier,
      ch: transaction.codeChallenge,
      rp: "/account",
      in: "login",
      i: 1_750_000,
      e: 1_750_000 + OAUTH_TRANSACTION_LIFETIME_SECONDS,
    });
    expect(value.length).toBeLessThan(1024);
    expect(parseOAuthTransactionCookieValue(value, KEY, 1_750_100)).toEqual(transaction);
  });

  it("round-trips a Discord transaction with its provider bound", () => {
    const transaction = createOAuthTransaction(
      "/account/providers",
      1_750_000,
      (size) => Buffer.alloc(size, 0x2a),
      "link",
      "discord",
    );
    const value = createOAuthTransactionCookieValue(transaction, KEY);
    expect(parseOAuthTransactionCookieValue(value, KEY, 1_750_100)).toEqual(transaction);
    expect(parseOAuthTransactionCookieValue(value, KEY, 1_750_100)?.provider).toBe(
      "discord",
    );
  });

  it("rejects a validly signed transaction whose provider is unknown or missing", () => {
    const make = (payload: Record<string, unknown>) => {
      const bytes = Buffer.from(JSON.stringify(payload));
      return `${bytes.toString("base64url")}.${createHmac("sha256", KEY)
        .update(bytes)
        .digest("base64url")}`;
    };
    const base = oauthTransaction();
    const signedBody = (provider: unknown) =>
      make({
        v: 1,
        pr: provider,
        st: base.state,
        no: base.nonce,
        ve: base.codeVerifier,
        ch: base.codeChallenge,
        rp: "/account",
        in: "login",
        i: base.issuedAt,
        e: base.expiresAt,
      });
    expect(
      parseOAuthTransactionCookieValue(signedBody("github"), KEY, 1_750_100),
    ).toBeNull();
    expect(
      parseOAuthTransactionCookieValue(signedBody(undefined), KEY, 1_750_100),
    ).toBeNull();
  });

  it("rejects tampered, expired, and cross-key transaction cookies", () => {
    const transaction = oauthTransaction();
    const value = createOAuthTransactionCookieValue(transaction, KEY);
    const [, signaturePart] = value.split(".");
    const forged = `${Buffer.from(
      JSON.stringify({
        v: 1,
        st: "A".repeat(32),
        no: transaction.nonce,
        ve: transaction.codeVerifier,
        ch: transaction.codeChallenge,
        rp: "/account",
        i: 1_750_000,
        e: 1_750_000 + OAUTH_TRANSACTION_LIFETIME_SECONDS,
      }),
    ).toString("base64url")}.${signaturePart}`;
    expect(parseOAuthTransactionCookieValue(forged, KEY, 1_750_100)).toBeNull();
    expect(parseOAuthTransactionCookieValue(value, OTHER_KEY, 1_750_100)).toBeNull();
    expect(
      parseOAuthTransactionCookieValue(
        value,
        KEY,
        1_750_000 + OAUTH_TRANSACTION_LIFETIME_SECONDS,
      ),
    ).toBeNull();
  });

  it("rejects validly signed transactions with implausible fields", () => {
    const make = (payload: unknown) => {
      const bytes = Buffer.from(JSON.stringify(payload));
      return `${bytes.toString("base64url")}.${createHmac("sha256", KEY)
        .update(bytes)
        .digest("base64url")}`;
    };
    const base = oauthTransaction();
    const badVerifier = make({
      v: 1,
      st: base.state,
      no: base.nonce,
      ve: "short",
      ch: base.codeChallenge,
      rp: "/account",
      i: base.issuedAt,
      e: base.expiresAt,
    });
    expect(parseOAuthTransactionCookieValue(badVerifier, KEY, 1_750_100)).toBeNull();
    const badLifetime = make({
      v: 1,
      st: base.state,
      no: base.nonce,
      ve: base.codeVerifier,
      ch: base.codeChallenge,
      rp: "/account",
      i: base.issuedAt,
      e: base.issuedAt + 60,
    });
    expect(parseOAuthTransactionCookieValue(badLifetime, KEY, 1_750_100)).toBeNull();
    const badReturnPath = make({
      v: 1,
      st: base.state,
      no: base.nonce,
      ve: base.codeVerifier,
      ch: base.codeChallenge,
      rp: "https://evil.example/account",
      i: base.issuedAt,
      e: base.expiresAt,
    });
    expect(parseOAuthTransactionCookieValue(badReturnPath, KEY, 1_750_100)).toBeNull();
    const futureIssued = make({
      v: 1,
      st: base.state,
      no: base.nonce,
      ve: base.codeVerifier,
      ch: base.codeChallenge,
      rp: "/account",
      i: 1_751_000,
      e: 1_751_000 + OAUTH_TRANSACTION_LIFETIME_SECONDS,
    });
    expect(parseOAuthTransactionCookieValue(futureIssued, KEY, 1_750_100)).toBeNull();
  });

  it("rejects a validly signed cookie whose payload is not a record", () => {
    const bytes = Buffer.from(JSON.stringify(["array"]));
    const value = `${bytes.toString("base64url")}.${createHmac("sha256", KEY)
      .update(bytes)
      .digest("base64url")}`;
    expect(parseOAuthTransactionCookieValue(value, KEY, 1_750_100)).toBeNull();
  });
});

describe("cookie header attributes", () => {
  it("sets HttpOnly, SameSite=Lax, and Path=/ on every cookie", () => {
    expect(cookieAttributes(false)).toBe("Path=/; HttpOnly; SameSite=Lax");
    expect(cookieAttributes(true)).toBe("Path=/; HttpOnly; SameSite=Lax; Secure");
    expect(buildSetCookieHeader(LOGIN_COOKIE_NAME, "value", 3600, true)).toBe(
      "clashlens_login=value; Max-Age=3600; Path=/; HttpOnly; SameSite=Lax; Secure",
    );
    expect(buildClearCookieHeader(OAUTH_COOKIE_NAME, false)).toBe(
      "clashlens_oauth=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax",
    );
  });

  it("keeps cookie values well below the 4 KB browser limit", () => {
    const longReturnPath = `/${"players/".repeat(20)}`; // bounded at 200 characters
    const transaction = createOAuthTransaction(longReturnPath.slice(0, 200), 1_750_000);
    const value = createOAuthTransactionCookieValue(transaction, KEY);
    expect(Buffer.byteLength(value, "utf8")).toBeLessThan(4096);
  });
});
