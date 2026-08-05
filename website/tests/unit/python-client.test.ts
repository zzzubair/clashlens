import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const TEST_SECRET = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8";

describe("server-only Python client response boundary", () => {
  const savedEnvironment = {
    NODE_ENV: process.env.NODE_ENV,
    CLASHLENS_PYTHON_API_URL: process.env.CLASHLENS_PYTHON_API_URL,
    CLASHLENS_PYTHON_HMAC_SECRET_B64: process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64,
    CLASHLENS_PYTHON_HMAC_SECRET_FILE: process.env.CLASHLENS_PYTHON_HMAC_SECRET_FILE,
  };

  beforeEach(() => {
    vi.resetModules();
    process.env.CLASHLENS_PYTHON_API_URL = "http://python-fixture.test/";
    delete process.env.CLASHLENS_PYTHON_HMAC_SECRET_FILE;
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    for (const [name, value] of Object.entries(savedEnvironment)) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
  });

  it("rejects a kind-correct but structurally malformed leaderboard", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ kind: "tracked-leaderboard", entries: [] }), {
        status: 200,
        headers: { "content-type": "application/json" },
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;

    const { createPythonClient } = await import("../../app/services/python.server");

    await expect(createPythonClient().getTrackedLeaderboard(25)).rejects.toMatchObject({
      status: 502,
      payload: { error: "malformed" },
    });
  });

  it("does not use an environment secret in production", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    process.env.NODE_ENV = "production";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;

    const { createPythonClient } = await import("../../app/services/python.server");

    await expect(createPythonClient().getTrackedLeaderboard()).rejects.toMatchObject({
      status: 503,
      payload: { error: "unavailable" },
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects an oversized search before contacting the private service", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;

    const { createPythonClient } = await import("../../app/services/python.server");

    await expect(
      createPythonClient().searchPlayers("x".repeat(81)),
    ).rejects.toMatchObject({
      status: 400,
      payload: { error: "invalid_input" },
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects refresh status from a different player", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          kind: "refresh-status",
          workId: "work-1",
          tag: "#2PQ",
          state: "queued",
          progressPercent: 0,
          message: "queued",
          publishedAt: null,
          player: null,
        }),
        { status: 200, headers: { "content-type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;

    const { createPythonClient } = await import("../../app/services/python.server");

    await expect(
      createPythonClient().getRefreshStatus("work-1", "#2PP"),
    ).rejects.toMatchObject({
      status: 409,
      payload: { error: "conflict" },
    });
  });
});
