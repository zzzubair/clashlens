import { afterEach, beforeEach, expect, it, vi } from "vitest";

import {
  allowPublicRefresh,
  clearPublicRefreshLimits,
} from "../../app/server/abuse.server";
import {
  getPlayerLookup,
  startPlayerLookup,
} from "../../app/services/player-lookup.server";

beforeEach(() => {
  clearPublicRefreshLimits();
  vi.stubEnv("CLASHLENS_PYTHON_API_URL", "http://python-fixture.test/");
  vi.stubEnv(
    "CLASHLENS_PYTHON_HMAC_SECRET_B64",
    "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8",
  );
  vi.stubEnv("CLASHLENS_TRUST_PROXY", "false");
});

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  clearPublicRefreshLimits();
});

it("shares the existing six-request allowance with Refresh while status reads stay free", async () => {
  const fetch = vi.fn().mockResolvedValue(
    new Response(JSON.stringify({ tag: "#LQQP", state: "checking" }), {
      headers: { "Content-Type": "application/json" },
    }),
  );
  // Each fetch gets a fresh readable response body.
  fetch.mockImplementation(
    async () => new Response(JSON.stringify({ tag: "#LQQP", state: "checking" })),
  );
  vi.stubGlobal("fetch", fetch);
  for (let index = 0; index < 5; index += 1)
    expect(allowPublicRefresh("local-public-client")).toBe(true);
  const request = new Request("http://localhost/players/%23LQQP");
  await expect(startPlayerLookup(request, "#LQQP")).resolves.toMatchObject({
    state: "checking",
  });
  await expect(startPlayerLookup(request, "#LQQP")).rejects.toMatchObject({
    status: 429,
  });
  await expect(getPlayerLookup("#LQQP")).resolves.toMatchObject({ state: "checking" });
  expect(fetch).toHaveBeenCalledTimes(2);
});

it.each([
  { tag: "#2PP", state: "tracking" },
  { tag: "#LQQP", state: "invented" },
])("rejects a mismatched or malformed lookup response", async (payload) => {
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue(new Response(JSON.stringify(payload))),
  );
  await expect(getPlayerLookup("#LQQP")).rejects.toMatchObject({ status: 502 });
});
