import type {
  AccountSummary,
  ClashLensAccount,
  GroupDeleteResult,
  GroupList,
  PrivateGroup,
  VerificationResult,
} from "../lib/account-contracts";
import {
  mapAccount,
  mapGroupDeleteResult,
  mapGroupResult,
  mapGroups,
  mapSummary,
  mapVerificationResult,
} from "../lib/account-contracts";
import {
  MAX_VERIFICATION_TOKEN_LENGTH,
  normalizeDisplayName,
  normalizeGroupName,
  normalizeSubmittedPlayerTag,
  normalizeUsername,
} from "../lib/account-validation";
import { isCanonicalUuid } from "../lib/validation";
import type { LoginIdentity } from "../server/auth.server";
import { isProvider, isProviderSubject } from "../server/auth.server";
import { isRecord, isString, PythonApiError } from "./python-response.server";
import {
  REQUEST_TIMEOUT_MS,
  requestJson,
  requestJsonRaw,
} from "./python-transport.server";

export interface PythonClient {
  createAccount(
    input: AccountNameInput,
    idempotencyKey: string,
  ): Promise<ClashLensAccount>;
  getAccount(): Promise<ClashLensAccount>;
  updateAccount(
    input: AccountNameInput & { preferences: Record<string, unknown> },
    idempotencyKey: string,
  ): Promise<ClashLensAccount>;
  listGroups(): Promise<GroupList>;
  createGroup(input: GroupInput, idempotencyKey: string): Promise<PrivateGroup>;
  updateGroup(
    groupId: string,
    input: GroupInput,
    idempotencyKey: string,
  ): Promise<PrivateGroup>;
  deleteGroup(groupId: string, idempotencyKey: string): Promise<GroupDeleteResult>;
  getAccountSummary(): Promise<AccountSummary>;
  setDefaultPlayer(tag: string, idempotencyKey: string): Promise<void>;
  linkProvider(
    provider: "google" | "discord",
    providerSubject: string,
    idempotencyKey: string,
  ): Promise<{ providers: string[] }>;
  unlinkProvider(
    provider: "google" | "discord",
    providerSubject: string,
    idempotencyKey: string,
    endedLogin?: string,
  ): Promise<{ providers: string[] }>;
  verifyPlayerToken(
    tag: string,
    token: string,
    idempotencyKey: string,
  ): Promise<VerificationResult>;
}

export interface PythonClientOptions {
  accountReadTimeoutMs?: number;
}

export interface AccountNameInput {
  username: string;
  displayName: string;
}

/** Players join a group one at a time (services/group-players.server.ts). */
export interface GroupInput {
  name: string;
}

function boundedAccountReadTimeout(value: number | undefined): number {
  if (value === undefined) return REQUEST_TIMEOUT_MS;
  if (!Number.isInteger(value) || value < 50 || value > REQUEST_TIMEOUT_MS) {
    throw new Error("account read timeout must be a bounded integer");
  }
  return value;
}

export function createPythonClient(
  identity?: LoginIdentity,
  options: PythonClientOptions = {},
): PythonClient {
  const accountReadTimeoutMs = boundedAccountReadTimeout(options.accountReadTimeoutMs);
  if (identity !== undefined) {
    if (!isProvider(identity.provider) || !isProviderSubject(identity.providerSubject)) {
      throw new Error("account client requires a bounded provider identity");
    }
  }
  return {
    createAccount: (input, idempotencyKey) =>
      createAccount(input, idempotencyKey, identity),
    getAccount: () => getAccount(identity, accountReadTimeoutMs),
    updateAccount: (input, idempotencyKey) =>
      updateAccount(input, idempotencyKey, identity),
    listGroups: () => listGroups(identity),
    createGroup: (input, idempotencyKey) => createGroup(input, idempotencyKey, identity),
    updateGroup: (groupId, input, idempotencyKey) =>
      updateGroup(groupId, input, idempotencyKey, identity),
    deleteGroup: (groupId, idempotencyKey) =>
      deleteGroup(groupId, idempotencyKey, identity),
    getAccountSummary: () => getAccountSummary(identity),
    setDefaultPlayer: (tag, idempotencyKey) =>
      setDefaultPlayer(tag, idempotencyKey, identity),
    linkProvider: (provider, providerSubject, idempotencyKey) =>
      changeProviderIdentity("link", provider, providerSubject, idempotencyKey, identity),
    unlinkProvider: (provider, providerSubject, idempotencyKey, endedLogin) =>
      changeProviderIdentity(
        "unlink",
        provider,
        providerSubject,
        idempotencyKey,
        identity,
        endedLogin,
      ),
    verifyPlayerToken: (tag, token, idempotencyKey) =>
      verifyPlayerToken(tag, token, idempotencyKey, identity),
  };
}

function requireIdentity(
  identity: LoginIdentity | undefined,
): asserts identity is LoginIdentity {
  if (identity === undefined) {
    throw new PythonApiError(403, { error: "forbidden" });
  }
}

function jsonBody(value: unknown): Buffer {
  let serialized: string | undefined;
  try {
    serialized = JSON.stringify(value);
  } catch {
    throw new PythonApiError(422, { error: "invalid_request" });
  }
  if (serialized === undefined) {
    throw new PythonApiError(422, { error: "invalid_request" });
  }
  return Buffer.from(serialized, "utf8");
}

function mappedOrMalformed<T>(mapped: T | null): T {
  if (mapped === null) {
    throw new PythonApiError(502, { error: "malformed" });
  }
  return mapped;
}

async function createAccount(
  input: AccountNameInput,
  idempotencyKey: string,
  identity: LoginIdentity | undefined,
): Promise<ClashLensAccount> {
  requireIdentity(identity);
  if (!isCanonicalUuid(idempotencyKey)) {
    throw new PythonApiError(400, { error: "invalid_input" });
  }
  const username = normalizeUsername(input.username);
  const displayName = normalizeDisplayName(input.displayName);
  if (username === null || displayName === null) {
    throw new PythonApiError(422, { error: "invalid_request" });
  }
  const payload = await requestJson<unknown>("/v1/account", {
    method: "POST",
    body: jsonBody({ username, display_name: displayName }),
    requestId: idempotencyKey,
    identity: identity,
  });
  return mappedOrMalformed(mapAccount(payload));
}

async function getAccount(
  identity: LoginIdentity | undefined,
  timeoutMs = REQUEST_TIMEOUT_MS,
): Promise<ClashLensAccount> {
  requireIdentity(identity);
  const payload = await requestJson<unknown>("/v1/account", {
    method: "GET",
    identity: identity,
    timeoutMs: timeoutMs,
  });
  return mappedOrMalformed(mapAccount(payload));
}

async function updateAccount(
  input: AccountNameInput & { preferences: Record<string, unknown> },
  idempotencyKey: string,
  identity: LoginIdentity | undefined,
): Promise<ClashLensAccount> {
  requireIdentity(identity);
  if (!isCanonicalUuid(idempotencyKey)) {
    throw new PythonApiError(400, { error: "invalid_input" });
  }
  const username = normalizeUsername(input.username);
  const displayName = normalizeDisplayName(input.displayName);
  const preferences = input.preferences;
  if (
    username === null ||
    displayName === null ||
    typeof preferences !== "object" ||
    preferences === null ||
    Array.isArray(preferences) ||
    Buffer.byteLength(JSON.stringify(preferences), "utf8") > 4096
  ) {
    throw new PythonApiError(422, { error: "invalid_request" });
  }
  const payload = await requestJson<unknown>("/v1/account", {
    method: "PATCH",
    body: jsonBody({ username, display_name: displayName, preferences }),
    requestId: idempotencyKey,
    identity: identity,
  });
  return mappedOrMalformed(mapAccount(payload));
}

async function listGroups(identity: LoginIdentity | undefined): Promise<GroupList> {
  requireIdentity(identity);
  const payload = await requestJson<unknown>("/v1/account/groups", {
    method: "GET",
    identity: identity,
  });
  return mappedOrMalformed(mapGroups(payload));
}

async function createGroup(
  input: GroupInput,
  idempotencyKey: string,
  identity: LoginIdentity | undefined,
): Promise<PrivateGroup> {
  requireIdentity(identity);
  if (!isCanonicalUuid(idempotencyKey)) {
    throw new PythonApiError(400, { error: "invalid_input" });
  }
  const name = normalizeGroupName(input.name);
  if (name === null) {
    throw new PythonApiError(422, { error: "invalid_request" });
  }
  const payload = await requestJson<unknown>("/v1/account/groups", {
    method: "POST",
    body: jsonBody({ name }),
    requestId: idempotencyKey,
    identity: identity,
  });
  return mappedOrMalformed(mapGroupResult(payload));
}

async function updateGroup(
  groupId: string,
  input: GroupInput,
  idempotencyKey: string,
  identity: LoginIdentity | undefined,
): Promise<PrivateGroup> {
  requireIdentity(identity);
  if (!isCanonicalUuid(idempotencyKey) || !isCanonicalUuid(groupId)) {
    throw new PythonApiError(400, { error: "invalid_input" });
  }
  const name = normalizeGroupName(input.name);
  if (name === null) {
    throw new PythonApiError(422, { error: "invalid_request" });
  }
  const payload = await requestJson<unknown>(`/v1/account/groups/${groupId}`, {
    method: "PATCH",
    body: jsonBody({ name }),
    requestId: idempotencyKey,
    identity: identity,
  });
  return mappedOrMalformed(mapGroupResult(payload));
}

async function deleteGroup(
  groupId: string,
  idempotencyKey: string,
  identity: LoginIdentity | undefined,
): Promise<GroupDeleteResult> {
  requireIdentity(identity);
  if (!isCanonicalUuid(idempotencyKey) || !isCanonicalUuid(groupId)) {
    throw new PythonApiError(400, { error: "invalid_input" });
  }
  const payload = await requestJson<unknown>(`/v1/account/groups/${groupId}`, {
    method: "DELETE",
    requestId: idempotencyKey,
    identity: identity,
  });
  return mappedOrMalformed(mapGroupDeleteResult(payload));
}

async function getAccountSummary(
  identity: LoginIdentity | undefined,
): Promise<AccountSummary> {
  requireIdentity(identity);
  const payload = await requestJson<unknown>("/v1/account/summary", {
    method: "GET",
    identity: identity,
  });
  return mappedOrMalformed(mapSummary(payload));
}

/** Choose the verified player the Discord bot uses when none is named. */
async function setDefaultPlayer(
  tag: string,
  idempotencyKey: string,
  identity: LoginIdentity | undefined,
): Promise<void> {
  requireIdentity(identity);
  if (!isCanonicalUuid(idempotencyKey)) {
    throw new PythonApiError(400, { error: "invalid_input" });
  }
  await requestJson<unknown>("/v1/account/default-player", {
    method: "POST",
    body: jsonBody({ tag }),
    requestId: idempotencyKey,
    identity: identity,
  });
}

/**
 * Link or unlink one provider identity. The subject comes from the fresh
 * OAuth authorization that just completed and crosses this boundary once;
 * it is never persisted by the website.
 */
async function changeProviderIdentity(
  action: "link" | "unlink",
  provider: "google" | "discord",
  providerSubject: string,
  idempotencyKey: string,
  identity: LoginIdentity | undefined,
  endedLogin?: string,
): Promise<{ providers: string[] }> {
  requireIdentity(identity);
  if (!isCanonicalUuid(idempotencyKey)) {
    throw new PythonApiError(400, { error: "invalid_input" });
  }
  if (!isProvider(provider) || !isProviderSubject(providerSubject)) {
    throw new PythonApiError(422, { error: "invalid_request" });
  }
  const payload = await requestJson<unknown>(`/v1/account/providers/${provider}`, {
    method: action === "link" ? "POST" : "DELETE",
    body: jsonBody({
      provider_subject: providerSubject,
      ...(endedLogin === undefined ? {} : { session: endedLogin }),
    }),
    requestId: idempotencyKey,
    identity: identity,
  });
  if (
    !isRecord(payload) ||
    !Array.isArray(payload.providers) ||
    !payload.providers.every(isString)
  ) {
    throw new PythonApiError(502, { error: "malformed" });
  }
  return { providers: [...payload.providers] };
}

/** Exact Python token rule: 1-512 printable ASCII characters, no whitespace. */
function isVerificationToken(value: unknown): value is string {
  return (
    typeof value === "string" &&
    value.length >= 1 &&
    value.length <= MAX_VERIFICATION_TOKEN_LENGTH &&
    [...value].every((character) => {
      const code = character.charCodeAt(0);
      return code >= 0x21 && code <= 0x7e;
    })
  );
}

async function verifyPlayerToken(
  tag: string,
  token: string,
  idempotencyKey: string,
  identity: LoginIdentity | undefined,
): Promise<VerificationResult> {
  requireIdentity(identity);
  if (!isCanonicalUuid(idempotencyKey)) {
    throw new PythonApiError(400, { error: "invalid_input" });
  }
  const normalized = normalizeSubmittedPlayerTag(tag);
  if (normalized === null) {
    throw new PythonApiError(422, { error: "invalid_tag" });
  }
  if (!isVerificationToken(token)) {
    throw new PythonApiError(422, { error: "invalid_request" });
  }
  const { status, payload } = await requestJsonRaw(
    `/v1/players/${encodeURIComponent(normalized)}/verifytoken`,
    {
      method: "POST",
      body: jsonBody({ token }),
      requestId: idempotencyKey,
      identity: identity,
    },
  );
  if (status >= 200 && status < 300) {
    return mappedOrMalformed(mapVerificationResult(payload));
  }
  if (isRecord(payload) && typeof payload.status === "string") {
    return mappedOrMalformed(mapVerificationResult(payload));
  }
  throw new PythonApiError(status, payload);
}
