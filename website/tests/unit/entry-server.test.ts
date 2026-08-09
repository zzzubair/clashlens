import { afterEach, describe, expect, it, vi } from "vitest";

const originalEnvironment = { ...process.env };

afterEach(() => {
  process.env = { ...originalEnvironment };
  vi.resetModules();
});

describe("production server entry", () => {
  it("loads without login-only configuration when login is disabled", async () => {
    process.env.NODE_ENV = "production";
    process.env.CLASHLENS_LOGIN_ENABLED = "false";
    delete process.env.CLASHLENS_PUBLIC_ORIGIN;
    delete process.env.CLASHLENS_GOOGLE_CLIENT_ID;
    delete process.env.CLASHLENS_GOOGLE_CLIENT_SECRET;
    delete process.env.CLASHLENS_GOOGLE_CLIENT_SECRET_FILE;
    delete process.env.CLASHLENS_LOGIN_SECRET_B64;
    delete process.env.CLASHLENS_LOGIN_SECRET_FILE;
    vi.resetModules();

    await expect(import("../../app/entry.server")).resolves.toBeDefined();
  });

  it("rejects startup before listening when enabled login configuration is incomplete", async () => {
    process.env.NODE_ENV = "production";
    process.env.CLASHLENS_LOGIN_ENABLED = "true";
    process.env.CLASHLENS_PUBLIC_ORIGIN = "https://clashlens.example";
    process.env.CLASHLENS_GOOGLE_CLIENT_ID = "test-client.apps.googleusercontent.com";
    delete process.env.CLASHLENS_GOOGLE_CLIENT_SECRET;
    delete process.env.CLASHLENS_GOOGLE_CLIENT_SECRET_FILE;
    delete process.env.CLASHLENS_LOGIN_SECRET_B64;
    delete process.env.CLASHLENS_LOGIN_SECRET_FILE;
    vi.resetModules();

    await expect(import("../../app/entry.server")).rejects.toThrow(/client-secret file/);
  });
});
