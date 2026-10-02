import { afterEach, beforeEach, expect, it, vi } from "vitest";

beforeEach(() => {
  vi.resetModules();
  vi.stubEnv("NODE_ENV", "test");
  vi.stubEnv("CLASHLENS_PYTHON_API_URL", "http://python-fixture.test/");
  vi.stubEnv(
    "CLASHLENS_PYTHON_HMAC_SECRET_B64",
    "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8",
  );
  vi.stubEnv("CLASHLENS_PYTHON_HMAC_SECRET_FILE", "");
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

it("signs leaderboard search and preserves a match's whole-board rank", async () => {
  const fetch = vi.fn().mockResolvedValue(
    Response.json({
      exact_tag: null,
      has_more: false,
      results: [{ name: "Nova", tag: "#2PP", rank: 103, trophies: 6000 }],
    }),
  );
  vi.stubGlobal("fetch", fetch);
  const { createPythonClient } = await import("../../app/services/python.server");
  await expect(createPythonClient().searchLeaderboard("Nova")).resolves.toEqual({
    exactTag: null,
    hasMore: false,
    results: [{ name: "Nova", tag: "#2PP", rank: 103, trophies: 6000 }],
  });
  expect(String(fetch.mock.calls[0][0])).toBe(
    "http://python-fixture.test/v1/leaderboards/live/search?q=Nova",
  );
  const headers = new Headers(fetch.mock.calls[0][1].headers);
  expect(headers.get("X-ClashLens-Signature")).toBeTruthy();
});

it.each([0, -1, 1.5, "103", null])("rejects invalid rank %s", async (rank) => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      Response.json({
        exact_tag: null,
        has_more: false,
        results: [{ name: "Nova", tag: "#2PP", rank, trophies: 6000 }],
      }),
    ),
  );
  const { createPythonClient } = await import("../../app/services/python.server");
  await expect(createPythonClient().searchLeaderboard("Nova")).rejects.toMatchObject({
    status: 502,
  });
});

it("rejects an exact tag pointing to another result", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(
      Response.json({
        exact_tag: "#2PY",
        has_more: false,
        results: [{ name: "Nova", tag: "#2PP", rank: 103, trophies: 6000 }],
      }),
    ),
  );
  const { createPythonClient } = await import("../../app/services/python.server");
  await expect(createPythonClient().searchLeaderboard("2py")).rejects.toMatchObject({
    status: 502,
  });
});

it.each(["", " ", "x".repeat(81)])(
  "rejects invalid input without a private request",
  async (query) => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    const { createPythonClient } = await import("../../app/services/python.server");
    await expect(createPythonClient().searchLeaderboard(query)).rejects.toMatchObject({
      status: 400,
    });
    expect(fetch).not.toHaveBeenCalled();
  },
);
