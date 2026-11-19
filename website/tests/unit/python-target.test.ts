import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const TEST_SECRET = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8";
const CREW = "00000000-0000-4000-8000-000000000001";
const KEY = "00000000-0000-4000-8000-000000000002";
const IDENTITY = { provider: "google", providerSubject: "owner-subject" } as const;

describe("private API addresses", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    vi.resetModules();
    vi.stubEnv("NODE_ENV", "test");
    vi.stubEnv("CLASHLENS_PYTHON_API_URL", "http://python-fixture.test/");
    vi.stubEnv("CLASHLENS_PYTHON_HMAC_SECRET_B64", TEST_SECRET);
    vi.stubEnv("CLASHLENS_PYTHON_HMAC_SECRET_FILE", undefined);
    const answer = new Response("{}", {
      status: 200,
      headers: { "content-type": "application/json" },
    });
    fetchMock = vi.fn().mockResolvedValue(answer);
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("sends a request only to the exact address it signed", async () => {
    const { requestJsonRaw } = await import("../../app/services/python-transport.server");
    await requestJsonRaw("/v1/players/%232PP/seasons/2026-10?days=7", { method: "GET" });
    expect(String(fetchMock.mock.calls[0]![0])).toBe(
      "http://python-fixture.test/v1/players/%232PP/seasons/2026-10?days=7",
    );

    for (const target of [
      "/v1/players/%232PP/seasons/..",
      "/v1/players/%232PP/seasons/.",
      "/v1/analytics/armies/seasons/..?category=army-types",
      "/v1/account/crews/x/members/%2e%2e",
    ]) {
      await expect(requestJsonRaw(target, { method: "GET" })).rejects.toMatchObject({
        status: 400,
        payload: { error: "invalid_input" },
      });
    }
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("refuses a crew member or invite that is not one", async () => {
    const crews = await import("../../app/services/crews.server");
    for (const write of [
      () => crews.changeCrewMember(IDENTITY, CREW, "..", "admin", KEY),
      () => crews.changeCrewMember(IDENTITY, CREW, ".", "member", KEY),
      () => crews.revokeCrewInvite(IDENTITY, CREW, "../../crews", KEY),
      () => crews.revokeCrewInvite(IDENTITY, CREW, "not-an-invite", KEY),
    ]) {
      expect(write).toThrow(expect.objectContaining({ status: 400 }));
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
