import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { action as refreshAction } from "../../app/routes/refresh";
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
  vi.stubEnv("CLASHLENS_PUBLIC_ORIGIN", "https://clashlens.example");
});

async function refresh(trigger = "automatic", peer = VISITOR, tag = "#2PP") {
  const request = new Request(
    `https://clashlens.example/resources/players/${encodeURIComponent(tag)}/refresh`,
    {
      method: "POST",
      headers: { Origin: "https://clashlens.example" },
      body: new URLSearchParams({ idempotencyKey: crypto.randomUUID(), trigger }),
    },
  );
  return refreshAction({
    request,
    params: { tag },
    context: createClientAddressContext({})(request, { address: peer }),
  } as never);
}

function fakeInteractiveApi() {
  const fetch = vi.fn().mockImplementation(async (url: URL) => {
    const tag = decodeURIComponent(url.pathname.split("/")[3]);
    return new Response(
      JSON.stringify(
        url.pathname.endsWith("/lookup")
          ? { tag, state: "checking" }
          : {
              refresh_id: crypto.randomUUID(),
              tag,
              status: "pending",
              outcome: "created",
            },
      ),
    );
  });
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

it("keeps all six manual requests available after six automatic profile refresh attempts", async () => {
  fakeInteractiveApi();
  for (const tag of ["#2PP", "#2PQ", "#2PY", "#2PL", "#2PG", "#2PR"])
    await refresh("automatic", VISITOR, tag);

  await expect(startPlayerLookup(VISITOR, "#LQQP")).resolves.toMatchObject({
    state: "checking",
  });
  for (let index = 0; index < 5; index++)
    expect((await refresh("manual")).init?.status).toBe(202);
  expect((await refresh("manual")).init?.status).toBe(429);
});

it("quietly skips automatic requests after three, isolates visitors, and allows them after a minute", async () => {
  const clock = vi.spyOn(Date, "now").mockReturnValue(1_000_000);
  const fetch = fakeInteractiveApi();
  for (let index = 0; index < 3; index++)
    expect((await refresh()).init?.status).toBe(202);
  for (let index = 0; index < 3; index++) {
    const skipped = await refresh();
    expect(skipped.init?.status).toBe(200);
    expect(skipped.data).toBeNull();
    expect(skipped.init?.headers).toEqual({ "Cache-Control": "no-store" });
  }
  expect(fetch).toHaveBeenCalledTimes(3);
  expect((await refresh("automatic", "198.51.100.10")).init?.status).toBe(202);
  clock.mockReturnValue(1_059_999);
  expect((await refresh()).data).toBeNull();
  clock.mockReturnValue(1_060_000);
  expect((await refresh()).init?.status).toBe(202);
  expect(fetch).toHaveBeenCalledTimes(5);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  clearPublicRefreshLimits();
});

it("shares the Refresh allowance, keeps status reads free, and admits lookups after reset", async () => {
  const clock = vi.spyOn(Date, "now").mockReturnValue(1_000_000);
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

  clock.mockReturnValue(1_059_999);
  await expect(startPlayerLookup(VISITOR, "#LQQJ")).rejects.toMatchObject({
    status: 429,
  });
  clock.mockReturnValue(1_060_000);
  await expect(startPlayerLookup(VISITOR, "#LQQP")).resolves.toMatchObject({
    state: "checking",
  });
  expect(fetch).toHaveBeenCalledTimes(3);
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
