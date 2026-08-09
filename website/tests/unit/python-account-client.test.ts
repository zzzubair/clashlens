import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const TEST_SECRET = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8";
const IDEMPOTENCY_KEY = "3be934b5-68fa-4741-8c7b-e03592e4ad70";
const IDENTITY = { provider: "google", providerSubject: "11223344556677889900" } as const;
const SUBJECT_B64URL = Buffer.from(IDENTITY.providerSubject, "utf8").toString(
  "base64url",
);
const GROUP_ID = "6c1e3f8a-2a44-4b7d-9c0e-1f2a3b4c5d6e";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function decodeBody(request: RequestInit): string {
  return new TextDecoder().decode(request.body as Uint8Array);
}

async function importClient() {
  const { createPythonClient } = await import("../../app/services/python.server");
  return createPythonClient(IDENTITY);
}

describe("server-only Python account client", () => {
  const savedEnvironment = {
    NODE_ENV: process.env.NODE_ENV,
    CLASHLENS_PYTHON_API_URL: process.env.CLASHLENS_PYTHON_API_URL,
    CLASHLENS_PYTHON_HMAC_SECRET_B64: process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64,
    CLASHLENS_PYTHON_HMAC_SECRET_FILE: process.env.CLASHLENS_PYTHON_HMAC_SECRET_FILE,
  };

  beforeEach(() => {
    vi.resetModules();
    process.env.CLASHLENS_PYTHON_API_URL = "http://python-fixture.test/";
    process.env.NODE_ENV = "test";
    process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64 = TEST_SECRET;
    delete process.env.CLASHLENS_PYTHON_HMAC_SECRET_FILE;
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    for (const [name, value] of Object.entries(savedEnvironment)) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
  });

  it("creates an account with the exact body, Google identity, and idempotency request ID", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse(
        {
          username: "nova88",
          display_name: "Nova",
          preferences: {},
          providers: ["google"],
        },
        201,
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = await importClient();

    await expect(
      client.createAccount({ username: "Nova88", displayName: "Nova" }, IDEMPOTENCY_KEY),
    ).resolves.toEqual({
      username: "nova88",
      displayName: "Nova",
      preferences: {},
      providers: ["google"],
    });

    const [url, init] = fetchMock.mock.calls[0] as [URL, RequestInit];
    expect(url).toEqual(new URL("/v1/account", "http://python-fixture.test/"));
    expect(init.method).toBe("POST");
    expect(decodeBody(init)).toBe('{"username":"nova88","display_name":"Nova"}');
    const headers = new Headers(init.headers);
    expect(headers.get("content-type")).toBe("application/json");
    expect(headers.get("X-ClashLens-Provider")).toBe(
      Buffer.from("google", "utf8").toString("base64url"),
    );
    expect(headers.get("X-ClashLens-Provider-Subject")).toBe(SUBJECT_B64URL);
    expect(headers.get("X-ClashLens-Request-Id")).toBe(IDEMPOTENCY_KEY);
  });

  it("rejects invalid account names before contacting the service", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const client = await importClient();

    await expect(
      client.createAccount({ username: "X!", displayName: "Nova" }, IDEMPOTENCY_KEY),
    ).rejects.toMatchObject({ status: 422, payload: { error: "invalid_request" } });
    await expect(
      client.createAccount({ username: "nova88", displayName: " " }, IDEMPOTENCY_KEY),
    ).rejects.toMatchObject({ status: 422, payload: { error: "invalid_request" } });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects a non-canonical idempotency UUID before signing or fetch", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const client = await importClient();

    await expect(
      client.createAccount({ username: "nova88", displayName: "Nova" }, "not-a-uuid"),
    ).rejects.toMatchObject({ status: 400, payload: { error: "invalid_input" } });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("propagates safe Python conflicts without echoing secrets", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ error: "username_unavailable" }, 409));
    vi.stubGlobal("fetch", fetchMock);
    const client = await importClient();

    await expect(
      client.createAccount({ username: "nova88", displayName: "Nova" }, IDEMPOTENCY_KEY),
    ).rejects.toMatchObject({ status: 409, payload: { error: "username_unavailable" } });
  });

  it("reads and updates the account with signed identity headers", async () => {
    const account = {
      username: "nova88",
      display_name: "Nova",
      preferences: { theme: "dark" },
      providers: ["google"],
    };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse(account))
      .mockResolvedValueOnce(jsonResponse({ ...account, display_name: "Nova Star" }));
    vi.stubGlobal("fetch", fetchMock);
    const client = await importClient();

    await expect(client.getAccount()).resolves.toEqual({
      username: "nova88",
      displayName: "Nova",
      preferences: { theme: "dark" },
      providers: ["google"],
    });
    await expect(
      client.updateAccount(
        {
          username: "nova88",
          displayName: "Nova Star",
          preferences: { theme: "dark" },
        },
        IDEMPOTENCY_KEY,
      ),
    ).resolves.toMatchObject({ displayName: "Nova Star" });

    const [getUrl, getInit] = fetchMock.mock.calls[0] as [URL, RequestInit];
    expect(getUrl).toEqual(new URL("/v1/account", "http://python-fixture.test/"));
    expect(getInit.method).toBe("GET");
    expect(new Headers(getInit.headers).get("X-ClashLens-Provider-Subject")).toBe(
      SUBJECT_B64URL,
    );
    const [patchUrl, patchInit] = fetchMock.mock.calls[1] as [URL, RequestInit];
    expect(patchUrl).toEqual(new URL("/v1/account", "http://python-fixture.test/"));
    expect(patchInit.method).toBe("PATCH");
    expect(decodeBody(patchInit)).toBe(
      '{"username":"nova88","display_name":"Nova Star","preferences":{"theme":"dark"}}',
    );
  });

  it("uses a shorter bounded timeout for an optional navigation client", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        username: "nova88",
        display_name: "Nova",
        preferences: {},
        providers: ["google"],
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const timeoutSpy = vi
      .spyOn(AbortSignal, "timeout")
      .mockReturnValue(new AbortController().signal);
    const { createPythonClient } = await import("../../app/services/python.server");

    await createPythonClient(IDENTITY, { accountReadTimeoutMs: 250 }).getAccount();

    expect(timeoutSpy).toHaveBeenCalledWith(250);
    timeoutSpy.mockRestore();
  });

  it("rejects oversized preferences before contacting the service", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const client = await importClient();

    await expect(
      client.updateAccount(
        {
          username: "nova88",
          displayName: "Nova",
          preferences: { padding: "x".repeat(5000) },
        },
        IDEMPOTENCY_KEY,
      ),
    ).rejects.toMatchObject({ status: 422, payload: { error: "invalid_request" } });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("lists, adds, and removes saved tags with exact payloads", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ players: [{ tag: "#2PP", name: "Nova" }] }))
      .mockResolvedValueOnce(jsonResponse({ tag: "#2PP", saved: true }))
      .mockResolvedValueOnce(jsonResponse({ tag: "#2PP", saved: false }));
    vi.stubGlobal("fetch", fetchMock);
    const client = await importClient();

    await expect(client.listSavedTags()).resolves.toEqual([
      { tag: "#2PP", name: "Nova" },
    ]);
    await expect(client.addSavedTag("2PP", IDEMPOTENCY_KEY)).resolves.toEqual({
      tag: "#2PP",
      saved: true,
    });
    await expect(client.removeSavedTag("#2PP", IDEMPOTENCY_KEY)).resolves.toEqual({
      tag: "#2PP",
      saved: false,
    });

    const [addUrl, addInit] = fetchMock.mock.calls[1] as [URL, RequestInit];
    expect(addUrl).toEqual(
      new URL("/v1/account/saved-tags", "http://python-fixture.test/"),
    );
    expect(addInit.method).toBe("POST");
    expect(decodeBody(addInit)).toBe('{"tag":"#2PP"}');
    const [removeUrl, removeInit] = fetchMock.mock.calls[2] as [URL, RequestInit];
    expect(removeUrl).toEqual(
      new URL("/v1/account/saved-tags/%232PP", "http://python-fixture.test/"),
    );
    expect(removeInit.method).toBe("DELETE");
  });

  it("rejects invalid saved tags before contacting the service", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const client = await importClient();

    await expect(client.addSavedTag("!!!", IDEMPOTENCY_KEY)).rejects.toMatchObject({
      status: 422,
      payload: { error: "invalid_tag" },
    });
    await expect(client.removeSavedTag("!!!", IDEMPOTENCY_KEY)).rejects.toMatchObject({
      status: 422,
      payload: { error: "invalid_tag" },
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("lists, creates, updates, and deletes groups with exact payloads", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(
        jsonResponse({
          groups: [{ group_id: GROUP_ID, name: "Favorites", tags: ["#2PP"] }],
        }),
      )
      .mockResolvedValueOnce(
        jsonResponse(
          { group_id: GROUP_ID, name: "Favorites", tags: ["#2PP", "#2QQ"] },
          201,
        ),
      )
      .mockResolvedValueOnce(
        jsonResponse({ group_id: GROUP_ID, name: "Favorites", tags: ["#2PP"] }),
      )
      .mockResolvedValueOnce(jsonResponse({ deleted: true, group_id: GROUP_ID }));
    vi.stubGlobal("fetch", fetchMock);
    const client = await importClient();

    await expect(client.listGroups()).resolves.toEqual([
      { groupId: GROUP_ID, name: "Favorites", tags: ["#2PP"] },
    ]);
    await expect(
      client.createGroup({ name: "Favorites", tags: ["2QQ", "2PP"] }, IDEMPOTENCY_KEY),
    ).resolves.toMatchObject({ groupId: GROUP_ID });
    await expect(
      client.updateGroup(GROUP_ID, { name: "Favorites", tags: ["2PP"] }, IDEMPOTENCY_KEY),
    ).resolves.toMatchObject({ groupId: GROUP_ID });
    await expect(client.deleteGroup(GROUP_ID, IDEMPOTENCY_KEY)).resolves.toEqual({
      groupId: GROUP_ID,
      deleted: true,
    });

    const [createUrl, createInit] = fetchMock.mock.calls[1] as [URL, RequestInit];
    expect(createUrl).toEqual(
      new URL("/v1/account/groups", "http://python-fixture.test/"),
    );
    expect(createInit.method).toBe("POST");
    expect(decodeBody(createInit)).toBe('{"name":"Favorites","tags":["#2PP","#2QQ"]}');
    const [updateUrl, updateInit] = fetchMock.mock.calls[2] as [URL, RequestInit];
    expect(updateUrl).toEqual(
      new URL(`/v1/account/groups/${GROUP_ID}`, "http://python-fixture.test/"),
    );
    expect(updateInit.method).toBe("PATCH");
    expect(decodeBody(updateInit)).toBe('{"name":"Favorites","tags":["#2PP"]}');
    const [deleteUrl, deleteInit] = fetchMock.mock.calls[3] as [URL, RequestInit];
    expect(deleteUrl).toEqual(
      new URL(`/v1/account/groups/${GROUP_ID}`, "http://python-fixture.test/"),
    );
    expect(deleteInit.method).toBe("DELETE");
  });

  it("rejects non-canonical group IDs and invalid group input locally", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const client = await importClient();

    await expect(client.deleteGroup("not-a-uuid", IDEMPOTENCY_KEY)).rejects.toMatchObject(
      {
        status: 400,
        payload: { error: "invalid_input" },
      },
    );
    await expect(
      client.createGroup({ name: " ", tags: [] }, IDEMPOTENCY_KEY),
    ).rejects.toMatchObject({ status: 422, payload: { error: "invalid_request" } });
    await expect(
      client.createGroup({ name: "Favorites", tags: ["!!!"] }, IDEMPOTENCY_KEY),
    ).rejects.toMatchObject({ status: 422, payload: { error: "invalid_request" } });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("reads the account summary", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        username: "nova88",
        display_name: "Nova",
        verified_players: [{ tag: "#2PP", name: "Nova" }],
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = await importClient();

    await expect(client.getAccountSummary()).resolves.toEqual({
      username: "nova88",
      displayName: "Nova",
      verifiedPlayers: [{ tag: "#2PP", name: "Nova" }],
    });
  });

  it("reads a public user anonymously with no provider identity", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      jsonResponse({
        username: "nova88",
        display_name: "Nova",
        verified_players: [{ tag: "#2PP", name: null }],
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const { createPythonClient } = await import("../../app/services/python.server");
    const client = createPythonClient();

    await expect(client.getPublicUser("Nova88")).resolves.toEqual({
      username: "nova88",
      displayName: "Nova",
      verifiedPlayers: [{ tag: "#2PP", name: null }],
    });
    const [url, init] = fetchMock.mock.calls[0] as [URL, RequestInit];
    expect(url).toEqual(new URL("/v1/users/nova88", "http://python-fixture.test/"));
    expect(init.method).toBe("GET");
    expect(new Headers(init.headers).get("X-ClashLens-Provider")).toBe("");
    expect(new Headers(init.headers).get("X-ClashLens-Provider-Subject")).toBe("");
  });

  it("rejects an invalid public username before contacting the service", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const { createPythonClient } = await import("../../app/services/python.server");
    const client = createPythonClient();

    await expect(client.getPublicUser("X!")).rejects.toMatchObject({
      status: 404,
      payload: { error: "user_not_found" },
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("verifies a player token with the exact token body and maps outcomes", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ status: "linked", tag: "#2PP" }))
      .mockResolvedValueOnce(jsonResponse({ status: "in_progress" }, 202))
      .mockResolvedValueOnce(jsonResponse({ status: "invalid_token", tag: "#2PP" }, 401))
      .mockResolvedValueOnce(
        jsonResponse(
          {
            status: "support_required",
            tag: "#2PP",
            verification_request_id: IDEMPOTENCY_KEY,
          },
          409,
        ),
      );
    vi.stubGlobal("fetch", fetchMock);
    const client = await importClient();

    await expect(
      client.verifyPlayerToken("#2PP", "player-token-123", IDEMPOTENCY_KEY),
    ).resolves.toEqual({ status: "linked", tag: "#2PP" });
    await expect(
      client.verifyPlayerToken("#2PP", "player-token-123", IDEMPOTENCY_KEY),
    ).resolves.toEqual({ status: "in_progress" });
    await expect(
      client.verifyPlayerToken("#2PP", "player-token-123", IDEMPOTENCY_KEY),
    ).resolves.toEqual({ status: "invalid_token", tag: "#2PP" });
    await expect(
      client.verifyPlayerToken("#2PP", "player-token-123", IDEMPOTENCY_KEY),
    ).resolves.toEqual({
      status: "support_required",
      tag: "#2PP",
      verificationRequestId: IDEMPOTENCY_KEY,
    });

    const [url, init] = fetchMock.mock.calls[0] as [URL, RequestInit];
    expect(url).toEqual(
      new URL("/v1/players/%232PP/verifytoken", "http://python-fixture.test/"),
    );
    expect(init.method).toBe("POST");
    expect(decodeBody(init)).toBe('{"token":"player-token-123"}');
    expect(new Headers(init.headers).get("X-ClashLens-Request-Id")).toBe(IDEMPOTENCY_KEY);
  });

  it("throws for protocol-level verification errors but maps outcome statuses", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ error: "service_unavailable" }, 503))
      .mockResolvedValueOnce(
        jsonResponse({ status: "verification_unavailable", tag: "#2PP" }, 503),
      )
      .mockResolvedValueOnce(jsonResponse({ error: "invalid_request" }, 422));
    vi.stubGlobal("fetch", fetchMock);
    const client = await importClient();

    await expect(
      client.verifyPlayerToken("#2PP", "token", IDEMPOTENCY_KEY),
    ).rejects.toMatchObject({ status: 503, payload: { error: "service_unavailable" } });
    await expect(
      client.verifyPlayerToken("#2PP", "token", IDEMPOTENCY_KEY),
    ).resolves.toEqual({ status: "verification_unavailable", tag: "#2PP" });
    await expect(
      client.verifyPlayerToken("#2PP", "token", IDEMPOTENCY_KEY),
    ).rejects.toMatchObject({ status: 422, payload: { error: "invalid_request" } });
  });

  it("never leaks the verification token in thrown errors", async () => {
    const secretToken = "supersecret-token-9876543210";
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ error: "invalid_request" }, 422));
    vi.stubGlobal("fetch", fetchMock);
    const client = await importClient();

    const oversized = "x".repeat(513);
    await expect(
      client.verifyPlayerToken("#2PP", oversized, IDEMPOTENCY_KEY),
    ).rejects.toMatchObject({ status: 422, payload: { error: "invalid_request" } });
    await expect(
      client.verifyPlayerToken("#2PP", "has space", IDEMPOTENCY_KEY),
    ).rejects.toMatchObject({ status: 422, payload: { error: "invalid_request" } });

    try {
      await client.verifyPlayerToken("#2PP", secretToken, IDEMPOTENCY_KEY);
      throw new Error("expected verification to fail");
    } catch (error) {
      expect(JSON.stringify(error)).not.toContain(secretToken);
      expect(JSON.stringify(error)).not.toContain("x".repeat(50));
    }
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("rejects malformed account payloads as safe 502 errors", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(jsonResponse({ username: "nova88" }))
      .mockResolvedValueOnce(jsonResponse({ players: [{ tag: "not-a-tag" }] }))
      .mockResolvedValueOnce(jsonResponse({ status: "linked" }));
    vi.stubGlobal("fetch", fetchMock);
    const client = await importClient();

    await expect(client.getAccount()).rejects.toMatchObject({
      status: 502,
      payload: { error: "malformed" },
    });
    await expect(client.listSavedTags()).rejects.toMatchObject({
      status: 502,
      payload: { error: "malformed" },
    });
    await expect(
      client.verifyPlayerToken("#2PP", "token", IDEMPOTENCY_KEY),
    ).rejects.toMatchObject({ status: 502, payload: { error: "malformed" } });
  });

  it("rejects account operations without a Google identity", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const { createPythonClient } = await import("../../app/services/python.server");
    const client = createPythonClient();

    await expect(client.getAccount()).rejects.toMatchObject({
      status: 403,
      payload: { error: "forbidden" },
    });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("rejects an unbounded Google identity at client creation", async () => {
    vi.stubGlobal("fetch", vi.fn());
    const { createPythonClient } = await import("../../app/services/python.server");
    expect(() =>
      createPythonClient({ provider: "google", providerSubject: "bad subject" }),
    ).toThrow("account client requires a bounded Google identity");
  });
});
