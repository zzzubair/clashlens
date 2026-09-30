import { afterEach, beforeEach, expect, it, vi } from "vitest";

import {
  allowPublicRefresh,
  clearPublicRefreshLimits,
} from "../../app/server/abuse.server";
import {
  getPlayerLookup,
  startPlayerLookup,
} from "../../app/services/player-lookup.server";
import {
  clientAddressContext,
  createClientAddressContext,
} from "../../app/server/client-address.server";

const VISITOR = "198.51.100.9";

beforeEach(() => {
  clearPublicRefreshLimits();
  vi.stubEnv("CLASHLENS_PYTHON_API_URL", "http://python-fixture.test/");
  vi.stubEnv(
    "CLASHLENS_PYTHON_HMAC_SECRET_B64",
    "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8",
  );
  vi.stubEnv("CLASHLENS_TRUST_PROXY", "true");
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
    expect(allowPublicRefresh(VISITOR)).toBe(true);
  await expect(startPlayerLookup(VISITOR, "#LQQP")).resolves.toMatchObject({
    state: "checking",
  });
  await expect(startPlayerLookup(VISITOR, "#LQQP")).rejects.toMatchObject({
    status: 429,
  });
  await expect(getPlayerLookup("#LQQP")).resolves.toMatchObject({ state: "checking" });
  expect(fetch).toHaveBeenCalledTimes(2);
});

it.each(["X-Forwarded-For", "CF-Connecting-IP"])(
  "cannot evade lookup limits by forging %s on a direct connection",
  async (header) => {
    const fetch = vi
      .fn()
      .mockImplementation(
        async () => new Response(JSON.stringify({ tag: "#LQQP", state: "checking" })),
      );
    vi.stubGlobal("fetch", fetch);
    const getContext = createClientAddressContext({
      CLASHLENS_TRUSTED_PROXY_IP: "127.0.0.2",
    });
    for (let index = 0; index < 7; index++) {
      const request = new Request("http://localhost/players/%23LQQP", {
        headers: { [header]: `203.0.113.${index + 1}` },
      });
      const identity = getContext(request, { address: VISITOR }).get(
        clientAddressContext,
      );
      const lookup = startPlayerLookup(identity, "#LQQP");
      if (index < 6) await expect(lookup).resolves.toMatchObject({ state: "checking" });
      else await expect(lookup).rejects.toMatchObject({ status: 429 });
    }
    expect(fetch).toHaveBeenCalledTimes(6);
  },
);

it("shares the trusted Cloudflare visitor's allowance and keeps other visitors separate", async () => {
  const fetch = vi
    .fn()
    .mockImplementation(
      async () => new Response(JSON.stringify({ tag: "#LQQP", state: "checking" })),
    );
  vi.stubGlobal("fetch", fetch);
  const getContext = createClientAddressContext({
    CLASHLENS_TRUSTED_PROXY_IP: "127.0.0.2",
  });
  for (let index = 0; index < 6; index++) expect(allowPublicRefresh(VISITOR)).toBe(true);
  for (const visitor of [VISITOR, "198.51.100.10"]) {
    const request = new Request("http://localhost/players/%23LQQP", {
      headers: { "CF-Connecting-IP": visitor, "X-Forwarded-For": "203.0.113.1" },
    });
    const identity = getContext(request, { address: "127.0.0.2" }).get(
      clientAddressContext,
    );
    const lookup = startPlayerLookup(identity, "#LQQP");
    if (visitor === VISITOR) await expect(lookup).rejects.toMatchObject({ status: 429 });
    else await expect(lookup).resolves.toMatchObject({ state: "checking" });
  }
  expect(fetch).toHaveBeenCalledOnce();
});

it("does not start a lookup when the server cannot establish a visitor address", async () => {
  const fetch = vi.fn();
  vi.stubGlobal("fetch", fetch);
  await expect(startPlayerLookup(undefined, "#LQQP")).rejects.toMatchObject({
    status: 503,
  });
  expect(fetch).not.toHaveBeenCalled();
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
