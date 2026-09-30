import { randomUUID } from "node:crypto";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ requestRefresh: vi.fn() }));
vi.mock("../../app/services/python.server", () => ({
  createPythonClient: () => ({ requestRefresh: mocks.requestRefresh }),
}));
vi.mock("../../app/server/config.server", () => ({
  getWebsiteConfig: () => ({ publicOrigin: new URL("https://clashlens.example") }),
}));

import { action } from "../../app/routes/refresh";
import { clearPublicRefreshLimits } from "../../app/server/abuse.server";
import { createClientAddressContext } from "../../app/server/client-address.server";

const PROXY = "127.0.0.2";
const VISITOR = "198.51.100.9";
const configuredContext = createClientAddressContext({
  CLASHLENS_TRUSTED_PROXY_IP: PROXY,
});

async function refresh(
  headers: Record<string, string> = {},
  peer = VISITOR,
  getContext = configuredContext,
) {
  const key = randomUUID();
  const request = new Request(
    `https://clashlens.example/resources/players/%232PP/refresh?address=${key}`,
    {
      method: "POST",
      headers: { Origin: "https://clashlens.example", ...headers },
      body: new URLSearchParams({ idempotencyKey: key, address: key }),
    },
  );
  const result = await action({
    request,
    params: { tag: "#2PP" },
    context: getContext(request, { address: peer }),
  } as never);
  return result.init?.status;
}

describe("Refresh address trust", () => {
  beforeEach(() => {
    clearPublicRefreshLimits();
    mocks.requestRefresh.mockReset().mockResolvedValue({ state: "queued" });
    // The legacy deployment flag must not enable unbounded header trust.
    vi.stubEnv("CLASHLENS_TRUST_PROXY", "true");
  });
  afterEach(() => vi.unstubAllEnvs());

  it("does not accept a header when the server cannot establish a socket peer", async () => {
    expect(await refresh({ "CF-Connecting-IP": VISITOR }, "")).toBe(503);
    expect(mocks.requestRefresh).not.toHaveBeenCalled();
  });

  it.each([
    "X-Forwarded-For",
    "Forwarded",
    "X-Real-IP",
    "CF-Connecting-IP",
    "True-Client-IP",
  ])("cannot evade six requests by forging %s on a direct connection", async (header) => {
    for (let index = 0; index < 8; index++) {
      expect(
        await refresh({
          [header]:
            header === "Forwarded"
              ? `for=203.0.113.${index + 1}`
              : `203.0.113.${index + 1}`,
          Cookie: `clashlens_login=forged-${index}`,
          "X-Forwarded-Host": `host-${index}.example`,
        }),
      ).toBe(index < 6 ? 202 : 429);
    }
    expect(mocks.requestRefresh).toHaveBeenCalledTimes(6);
  });

  it("trusts only the configured proxy and single visitor header", async () => {
    for (let index = 0; index < 7; index++) {
      expect(
        await refresh(
          {
            "CF-Connecting-IP": VISITOR,
            "X-Forwarded-For": `203.0.113.${index + 1}, ${VISITOR}`,
            Forwarded: `for=203.0.113.${index + 1}`,
            "X-Real-IP": `203.0.113.${index + 1}`,
          },
          PROXY,
        ),
      ).toBe(index < 6 ? 202 : 429);
    }
    expect(await refresh({ "CF-Connecting-IP": "198.51.100.10" }, PROXY)).toBe(202);
    expect(await refresh({}, "198.51.100.11")).toBe(202);
  });

  it("falls back to the socket peer for missing or malformed trusted headers", async () => {
    for (const value of [
      "",
      "invalid",
      "192.0.2.1:80",
      "192.0.2.1, 192.0.2.2",
      "::1%lo",
      "unknown",
      "203.0.113.1, 203.0.113.2",
    ]) {
      const status = await refresh(value ? { "CF-Connecting-IP": value } : {}, PROXY);
      expect(status).toBe(value.startsWith("203.") ? 429 : 202);
    }
  });

  it("trusts no headers by default and supports an explicitly replaced custom header", async () => {
    for (let index = 0; index < 7; index++) {
      expect(
        await refresh(
          { "CF-Connecting-IP": `203.0.113.${index + 1}` },
          PROXY,
          createClientAddressContext({}),
        ),
      ).toBe(index < 6 ? 202 : 429);
    }
    const custom = createClientAddressContext({
      CLASHLENS_TRUSTED_PROXY_IP: PROXY,
      CLASHLENS_CLIENT_IP_HEADER: "X-Verified-Visitor",
    });
    expect(await refresh({ "X-Verified-Visitor": VISITOR }, PROXY, custom)).toBe(202);
  });

  it("keeps equivalent IPv6 and mapped IPv4 addresses in one bucket", async () => {
    for (let index = 0; index < 7; index++) {
      expect(
        await refresh(
          { "CF-Connecting-IP": index % 2 ? "2001:db8::1" : "2001:0DB8:0:0:0:0:0:1" },
          "::ffff:127.0.0.2",
        ),
      ).toBe(index < 6 ? 202 : 429);
    }
  });

  it.each([
    { CLASHLENS_TRUSTED_PROXY_IP: "*" },
    { CLASHLENS_TRUSTED_PROXY_IP: "127.0.0.0/8" },
    { CLASHLENS_CLIENT_IP_HEADER: "bad header" },
  ])("rejects unsafe configuration before listening", (env) => {
    expect(() => createClientAddressContext(env)).toThrow("Invalid trusted proxy");
  });
});
