import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const TEST_SECRET = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8";
const CLIENT_SECRET = "GOCSPX-test-secret-value";
const CLIENT_ID = "1234567890-abc.apps.googleusercontent.com";

const ENV_KEYS = [
  "NODE_ENV",
  "CLASHLENS_PUBLIC_ORIGIN",
  "CLASHLENS_LOGIN_ENABLED",
  "CLASHLENS_GOOGLE_CLIENT_ID",
  "CLASHLENS_GOOGLE_CLIENT_SECRET",
  "CLASHLENS_GOOGLE_CLIENT_SECRET_FILE",
  "CLASHLENS_LOGIN_SECRET_B64",
  "CLASHLENS_LOGIN_SECRET_FILE",
  "CLASHLENS_GOOGLE_ISSUER_URL",
] as const;

const savedEnvironment = Object.fromEntries(
  ENV_KEYS.map((key) => [key, process.env[key]]),
);

let tempDirectory: string | undefined;

function writeTempFile(name: string, content: string): string {
  if (tempDirectory === undefined) {
    tempDirectory = mkdtempSync(join(tmpdir(), "clashlens-config-"));
  }
  const path = join(tempDirectory, name);
  writeFileSync(path, content);
  return path;
}

async function loadConfig() {
  const { loadWebsiteConfig } = await import("../../app/server/config.server");
  return loadWebsiteConfig();
}

describe("server-only website login configuration", () => {
  beforeEach(() => {
    vi.resetModules();
    for (const key of ENV_KEYS) delete process.env[key];
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PUBLIC_ORIGIN = "http://127.0.0.1:3000";
    process.env.CLASHLENS_LOGIN_ENABLED = "true";
    process.env.CLASHLENS_GOOGLE_CLIENT_ID = CLIENT_ID;
    process.env.CLASHLENS_GOOGLE_CLIENT_SECRET = CLIENT_SECRET;
    process.env.CLASHLENS_LOGIN_SECRET_B64 = TEST_SECRET;
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    for (const [key, value] of Object.entries(savedEnvironment)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  });

  afterAll(() => {
    if (tempDirectory !== undefined) rmSync(tempDirectory, { recursive: true });
  });

  it("loads a complete local development login configuration", async () => {
    const config = await loadConfig();
    expect(config.loginEnabled).toBe(true);
    expect(config.production).toBe(false);
    expect(config.publicOrigin.toString()).toBe("http://127.0.0.1:3000/");
    expect(config.googleClientId).toBe(CLIENT_ID);
    expect(config.googleClientSecret).toBe(CLIENT_SECRET);
    expect(config.loginSecret).toHaveLength(32);
    expect(config.googleIssuerUrl.toString()).toBe("https://accounts.google.com/");
    expect(config.cookieSecure).toBe(false);
  });

  it("enables the Secure cookie flag for an https origin", async () => {
    process.env.CLASHLENS_PUBLIC_ORIGIN = "https://clashlens.example";
    const config = await loadConfig();
    expect(config.cookieSecure).toBe(true);
  });

  it("accepts true and 1 as the login-enabled marker and nothing else", async () => {
    process.env.CLASHLENS_LOGIN_ENABLED = "1";
    expect((await loadConfig()).loginEnabled).toBe(true);
    process.env.CLASHLENS_LOGIN_ENABLED = "TRUE";
    expect((await loadConfig()).loginEnabled).toBe(false);
  });

  it("loads production configuration from protected secret files", async () => {
    process.env.NODE_ENV = "production";
    process.env.CLASHLENS_PUBLIC_ORIGIN = "https://clashlens.example";
    delete process.env.CLASHLENS_GOOGLE_CLIENT_SECRET;
    delete process.env.CLASHLENS_LOGIN_SECRET_B64;
    process.env.CLASHLENS_GOOGLE_CLIENT_SECRET_FILE = writeTempFile(
      "google-client-secret",
      `${CLIENT_SECRET}\n`,
    );
    process.env.CLASHLENS_LOGIN_SECRET_FILE = writeTempFile(
      "login-secret",
      `${TEST_SECRET}\n`,
    );
    const config = await loadConfig();
    expect(config.production).toBe(true);
    expect(config.cookieSecure).toBe(true);
    expect(config.googleClientSecret).toBe(CLIENT_SECRET);
    expect(config.loginSecret).toHaveLength(32);
  });

  it("fails startup when production login lacks the protected secret files", async () => {
    process.env.NODE_ENV = "production";
    process.env.CLASHLENS_PUBLIC_ORIGIN = "https://clashlens.example";
    delete process.env.CLASHLENS_GOOGLE_CLIENT_SECRET;
    delete process.env.CLASHLENS_LOGIN_SECRET_B64;
    await expect(loadConfig()).rejects.toThrow(/client-secret file/);
  });

  it("fails startup when production login uses an http public origin", async () => {
    process.env.NODE_ENV = "production";
    process.env.CLASHLENS_PUBLIC_ORIGIN = "http://127.0.0.1:3000";
    await expect(loadConfig()).rejects.toThrow(/https public origin/);
  });

  it("fails startup when production login sets the Google issuer override", async () => {
    process.env.NODE_ENV = "production";
    process.env.CLASHLENS_PUBLIC_ORIGIN = "https://clashlens.example";
    delete process.env.CLASHLENS_GOOGLE_CLIENT_SECRET;
    delete process.env.CLASHLENS_LOGIN_SECRET_B64;
    process.env.CLASHLENS_GOOGLE_CLIENT_SECRET_FILE = writeTempFile(
      "issuer-test-client-secret",
      `${CLIENT_SECRET}\n`,
    );
    process.env.CLASHLENS_LOGIN_SECRET_FILE = writeTempFile(
      "issuer-test-login-secret",
      `${TEST_SECRET}\n`,
    );
    process.env.CLASHLENS_GOOGLE_ISSUER_URL = "https://issuer.example";
    await expect(loadConfig()).rejects.toThrow(/outside production/);
  });

  it("honors a local http Google issuer override outside production", async () => {
    process.env.CLASHLENS_GOOGLE_ISSUER_URL = "http://127.0.0.1:4010";
    const config = await loadConfig();
    expect(config.googleIssuerUrl.toString()).toBe("http://127.0.0.1:4010/");
  });

  it("rejects a remote http issuer override and a remote http origin", async () => {
    process.env.CLASHLENS_GOOGLE_ISSUER_URL = "http://issuer.example";
    await expect(loadConfig()).rejects.toThrow(/local test origins/);
    delete process.env.CLASHLENS_GOOGLE_ISSUER_URL;
    process.env.CLASHLENS_PUBLIC_ORIGIN = "http://clashlens.example";
    await expect(loadConfig()).rejects.toThrow(/local test origins/);
  });

  it("does not require login secrets when login is disabled", async () => {
    process.env.CLASHLENS_LOGIN_ENABLED = "false";
    delete process.env.CLASHLENS_GOOGLE_CLIENT_SECRET;
    delete process.env.CLASHLENS_LOGIN_SECRET_B64;
    const config = await loadConfig();
    expect(config.loginEnabled).toBe(false);
    expect(config.loginSecret).toHaveLength(0);
  });

  it("rejects malformed public origins", async () => {
    process.env.CLASHLENS_PUBLIC_ORIGIN = "https://clashlens.example/path";
    await expect(loadConfig()).rejects.toThrow(/path, query, or fragment/);
    process.env.CLASHLENS_PUBLIC_ORIGIN = "https://clashlens.example?x=1";
    await expect(loadConfig()).rejects.toThrow(/path, query, or fragment/);
    process.env.CLASHLENS_PUBLIC_ORIGIN = "not a url";
    await expect(loadConfig()).rejects.toThrow(/malformed public origin/);
    delete process.env.CLASHLENS_PUBLIC_ORIGIN;
    await expect(loadConfig()).rejects.toThrow(/missing public origin/);
  });

  it("rejects malformed Google client IDs and secrets", async () => {
    process.env.CLASHLENS_GOOGLE_CLIENT_ID = "bad id!";
    await expect(loadConfig()).rejects.toThrow(/Google client ID/);
    process.env.CLASHLENS_GOOGLE_CLIENT_ID = CLIENT_ID;
    process.env.CLASHLENS_GOOGLE_CLIENT_SECRET = "bad\u0007secret";
    await expect(loadConfig()).rejects.toThrow(/malformed Google client secret/);
    delete process.env.CLASHLENS_GOOGLE_CLIENT_SECRET;
    await expect(loadConfig()).rejects.toThrow(/missing Google client secret/);
  });

  it("rejects a login secret that is not exactly 32 bytes", async () => {
    process.env.CLASHLENS_LOGIN_SECRET_B64 = Buffer.from("short").toString("base64url");
    await expect(loadConfig()).rejects.toThrow(/exactly 32 bytes/);
  });

  it("rejects a protected secret file that is not readable or is malformed", async () => {
    process.env.CLASHLENS_GOOGLE_CLIENT_SECRET_FILE = "/nonexistent/secret-file";
    delete process.env.CLASHLENS_GOOGLE_CLIENT_SECRET;
    await expect(loadConfig()).rejects.toThrow(/not readable/);
    process.env.CLASHLENS_GOOGLE_CLIENT_SECRET_FILE = writeTempFile("empty-secret", "\n");
    await expect(loadConfig()).rejects.toThrow(/malformed protected secret file/);
  });
});
