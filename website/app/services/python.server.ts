import type {
  ArmyAnalytics,
  BattleArmy,
  PlayerPage,
  RankedBattleEvent,
  RefreshStatus,
  RefreshWork,
  SearchResponse,
  TrackedLeaderboard,
} from "../lib/contracts";
import type {
  AccountSummary,
  ClashLensAccount,
  GroupDeleteResult,
  PrivateGroup,
  PublicUser,
  SavedPlayer,
  SavedTagResult,
  VerificationResult,
} from "../lib/account-contracts";
import {
  mapAccount,
  mapGroupDeleteResult,
  mapGroupResult,
  mapGroups,
  mapPublicUser,
  mapSavedTagResult,
  mapSavedTags,
  mapSummary,
  mapVerificationResult,
} from "../lib/account-contracts";
import {
  MAX_VERIFICATION_TOKEN_LENGTH,
  normalizeDisplayName,
  normalizeGroupName,
  normalizeSubmittedPlayerTag,
  normalizeTagList,
  normalizeUsername,
} from "../lib/account-validation";
import { normalizePlayerTag } from "../lib/player-tag";
import { isCanonicalUuid, MAX_SEARCH_QUERY_LENGTH } from "../lib/validation";
import { isProviderSubject } from "../server/google-oidc.server";
import {
  createProofHeaders,
  decodeSecretValue,
  loadSecretFile,
} from "../server/signer.server";

const DEFAULT_CALLER = "typescript-website";
const DEFAULT_KEY_ID = "current";
const REQUEST_TIMEOUT_MS = 5_000;
const MAX_RESPONSE_BYTES = 2 * 1024 * 1024;

export class PythonApiError extends Error {
  readonly status: number;
  readonly payload: unknown;

  constructor(status: number, payload: unknown) {
    super("private Python service request failed");
    this.name = "PythonApiError";
    this.status = status;
    this.payload = payload;
  }
}

interface ClientConfig {
  baseUrl: URL;
  caller: string;
  keyId: string;
  key: Buffer;
}

let cachedConfig: ClientConfig | undefined;

export interface PythonClient {
  getTrackedLeaderboard(
    limit?: number,
    view?: "live" | "daily",
    offset?: number,
    selector?: { officialSeasonId: string; dayNumber: number },
  ): Promise<TrackedLeaderboard>;
  searchPlayers(query: string, limit?: number): Promise<SearchResponse>;
  getPlayer(tag: string): Promise<PlayerPage>;
  getArmyAnalytics(query: URLSearchParams): Promise<ArmyAnalytics>;
  requestRefresh(tag: string, idempotencyKey: string): Promise<RefreshWork>;
  getRefreshStatus(workId: string, tag: string): Promise<RefreshStatus>;
  createAccount(
    input: AccountNameInput,
    idempotencyKey: string,
  ): Promise<ClashLensAccount>;
  getAccount(): Promise<ClashLensAccount>;
  updateAccount(
    input: AccountNameInput & { preferences: Record<string, unknown> },
    idempotencyKey: string,
  ): Promise<ClashLensAccount>;
  listSavedTags(): Promise<SavedPlayer[]>;
  addSavedTag(tag: string, idempotencyKey: string): Promise<SavedTagResult>;
  removeSavedTag(tag: string, idempotencyKey: string): Promise<SavedTagResult>;
  listGroups(): Promise<PrivateGroup[]>;
  createGroup(input: GroupInput, idempotencyKey: string): Promise<PrivateGroup>;
  updateGroup(
    groupId: string,
    input: GroupInput,
    idempotencyKey: string,
  ): Promise<PrivateGroup>;
  deleteGroup(groupId: string, idempotencyKey: string): Promise<GroupDeleteResult>;
  getAccountSummary(): Promise<AccountSummary>;
  linkProvider(
    provider: "google" | "discord",
    providerSubject: string,
    idempotencyKey: string,
  ): Promise<{ providers: string[] }>;
  unlinkProvider(
    provider: "google" | "discord",
    providerSubject: string,
    idempotencyKey: string,
  ): Promise<{ providers: string[] }>;
  getPublicUser(username: string): Promise<PublicUser>;
  verifyPlayerToken(
    tag: string,
    token: string,
    idempotencyKey: string,
  ): Promise<VerificationResult>;
}

/** A validated browser login identity for one of the two Phase 1 providers. */
export interface LoginProviderIdentity {
  provider: "google" | "discord";
  providerSubject: string;
}

/**
 * Backward-compatible alias for the login identity type. The private Python
 * client signs every account operation with exactly one provider identity.
 */
export type GoogleAccountIdentity = LoginProviderIdentity;

export interface PythonClientOptions {
  accountReadTimeoutMs?: number;
}

export interface AccountNameInput {
  username: string;
  displayName: string;
}

export interface GroupInput {
  name: string;
  tags: string[];
}

/**
 * Create the private Python API client. Pass the validated Google identity
 * (from the login cookie) when account operations are needed; public
 * operations stay anonymous either way.
 */
export function createPythonClient(
  identity?: GoogleAccountIdentity,
  options: PythonClientOptions = {},
): PythonClient {
  const accountOperations = createAccountOperations(
    identity,
    boundedAccountReadTimeout(options.accountReadTimeoutMs),
  );
  return {
    getTrackedLeaderboard,
    searchPlayers,
    getPlayer: getPlayerPage,
    getArmyAnalytics,
    requestRefresh: requestPlayerRefresh,
    getRefreshStatus,
    ...accountOperations,
  };
}

function boundedAccountReadTimeout(value: number | undefined): number {
  if (value === undefined) return REQUEST_TIMEOUT_MS;
  if (!Number.isInteger(value) || value < 50 || value > REQUEST_TIMEOUT_MS) {
    throw new Error("account read timeout must be a bounded integer");
  }
  return value;
}

async function getTrackedLeaderboard(
  limit = 25,
  view: "live" | "daily" = "live",
  offset = 0,
  selector?: { officialSeasonId: string; dayNumber: number },
): Promise<TrackedLeaderboard> {
  const query = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (selector) {
    query.set("official_season_id", selector.officialSeasonId);
    query.set("season_day_number", String(selector.dayNumber));
  }
  const payload = await requestJson<unknown>(
    `/v1/leaderboards/${view === "live" ? "live" : "frozen"}?${query.toString()}`,
    "GET",
    undefined,
    undefined,
  );
  return mapLeaderboard(payload, view);
}

async function searchPlayers(query: string, limit = 50): Promise<SearchResponse> {
  if (
    query.length > MAX_SEARCH_QUERY_LENGTH ||
    !Number.isInteger(limit) ||
    limit < 1 ||
    limit > 50
  ) {
    throw new PythonApiError(400, { error: "invalid_input" });
  }
  const payload = await requestJson<unknown>(
    `/v1/players/search?q=${encodeURIComponent(query)}&limit=${limit}`,
    "GET",
    undefined,
    undefined,
  );
  return mapSearch(payload, query);
}

async function getArmyAnalytics(query: URLSearchParams): Promise<ArmyAnalytics> {
  let payload: unknown;
  try {
    payload = await requestJson<unknown>(
      `/v1/analytics/armies?${query.toString()}`,
      "GET",
      undefined,
      undefined,
    );
  } catch (cause) {
    if (
      cause instanceof PythonApiError &&
      cause.status === 404 &&
      isRecord(cause.payload) &&
      cause.payload.error === "no_completed_legend_days"
    ) {
      // The current season has no completed Legend day yet; keep the previous
      // season reference so the page can link to it honestly.
      throw new NoCompletedLegendDaysError(
        isString(cause.payload.previous_season_id)
          ? cause.payload.previous_season_id
          : null,
      );
    }
    throw cause;
  }
  return mapArmyAnalytics(payload);
}

export class NoCompletedLegendDaysError extends Error {
  readonly previousSeasonId: string | null;

  constructor(previousSeasonId: string | null) {
    super("no completed Legend days this season");
    this.name = "NoCompletedLegendDaysError";
    this.previousSeasonId = previousSeasonId;
  }
}

async function getPlayerPage(tag: string): Promise<PlayerPage> {
  const payload = await requestJson<unknown>(
    `/v1/players/${encodeURIComponent(tag)}`,
    "GET",
    undefined,
    undefined,
  );
  return mapPlayerPage(payload);
}

async function requestPlayerRefresh(
  tag: string,
  idempotencyKey: string,
): Promise<RefreshWork> {
  if (!isCanonicalUuid(idempotencyKey)) {
    throw new PythonApiError(400, { error: "invalid_input" });
  }
  const payload = await requestJson<unknown>(
    `/v1/players/${encodeURIComponent(tag)}/refresh`,
    "POST",
    undefined,
    undefined,
    idempotencyKey,
  );
  return mapRefresh(payload, "refresh-work");
}

async function getRefreshStatus(workId: string, tag: string): Promise<RefreshStatus> {
  const payload = await requestJson<unknown>(
    `/v1/refreshes/${encodeURIComponent(workId)}`,
    "GET",
    undefined,
    undefined,
  );
  const status = mapRefresh(payload, "refresh-status") as RefreshStatus;
  if (status.tag !== tag) {
    throw new PythonApiError(409, { error: "conflict" });
  }
  if (status.state === "complete") {
    return { ...status, player: await getPlayerPage(tag) };
  }
  return status;
}

async function requestJson<T>(
  target: string,
  method: "GET" | "POST" | "PATCH" | "DELETE",
  body: Buffer | undefined,
  expectedKind: string | string[] | undefined,
  requestId?: string,
  identity?: GoogleAccountIdentity,
  allowedStatuses?: readonly number[],
  timeoutMs = REQUEST_TIMEOUT_MS,
): Promise<T> {
  const { status, payload } = await requestJsonRaw(
    target,
    method,
    body,
    requestId,
    identity,
    timeoutMs,
  );
  if (!(status >= 200 && status < 300) && !allowedStatuses?.includes(status)) {
    throw new PythonApiError(status, payload);
  }
  if (expectedKind === undefined) return payload as T;
  const acceptedKinds = Array.isArray(expectedKind) ? expectedKind : [expectedKind];
  if (
    !isRecord(payload) ||
    typeof payload.kind !== "string" ||
    !acceptedKinds.includes(payload.kind)
  ) {
    throw new PythonApiError(502, { error: "unavailable" });
  }
  return payload as T;
}

async function requestJsonRaw(
  target: string,
  method: "GET" | "POST" | "PATCH" | "DELETE",
  body: Buffer | undefined,
  requestId?: string,
  identity?: GoogleAccountIdentity,
  timeoutMs = REQUEST_TIMEOUT_MS,
): Promise<{ status: number; payload: unknown }> {
  const config = getConfig();
  const proof = createProofHeaders({
    key: config.key,
    caller: config.caller,
    keyId: config.keyId,
    method,
    rawTarget: target,
    body,
    provider: identity?.provider,
    providerSubject: identity?.providerSubject,
    requestId,
    lifetimeSeconds: 10,
  });
  let response: Response;
  try {
    response = await fetch(new URL(target, config.baseUrl), {
      method,
      headers: {
        ...proof.headers,
        ...(body === undefined ? {} : { "content-type": "application/json" }),
      },
      body: body === undefined ? undefined : Uint8Array.from(body),
      cache: "no-store",
      signal: AbortSignal.timeout(timeoutMs),
    });
  } catch {
    throw new PythonApiError(503, { error: "unavailable" });
  }
  let payload: unknown;
  try {
    payload = await readPayload(response);
  } catch {
    throw new PythonApiError(502, { error: "malformed" });
  }
  return { status: response.status, payload };
}

async function readPayload(response: Response): Promise<unknown> {
  const contentLength = response.headers.get("content-length");
  if (
    contentLength !== null &&
    (!/^\d+$/.test(contentLength) || Number(contentLength) > MAX_RESPONSE_BYTES)
  ) {
    return { error: "malformed" };
  }
  const bytes = new Uint8Array(await response.arrayBuffer());
  if (bytes.byteLength > MAX_RESPONSE_BYTES) return { error: "malformed" };
  let text: string;
  try {
    text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    return { error: "malformed" };
  }
  if (text === "") return {};
  try {
    return JSON.parse(text);
  } catch {
    return { error: "malformed" };
  }
}

function getConfig(): ClientConfig {
  if (cachedConfig !== undefined) return cachedConfig;
  const rawBaseUrl = process.env.CLASHLENS_PYTHON_API_URL?.trim();
  const configuredCaller = process.env.CLASHLENS_PYTHON_HMAC_CALLER?.trim();
  const configuredKeyId = process.env.CLASHLENS_PYTHON_HMAC_KEY_ID?.trim();
  const production = process.env.NODE_ENV === "production";
  if (production && (!configuredCaller || !configuredKeyId)) {
    throw new PythonApiError(503, { error: "unavailable" });
  }
  const caller = configuredCaller || DEFAULT_CALLER;
  const keyId = configuredKeyId || DEFAULT_KEY_ID;
  if (!rawBaseUrl) throw new PythonApiError(503, { error: "unavailable" });
  let baseUrl: URL;
  try {
    baseUrl = new URL(rawBaseUrl);
    if (
      !/^https?:$/.test(baseUrl.protocol) ||
      baseUrl.pathname !== "/" ||
      baseUrl.search ||
      baseUrl.hash
    ) {
      throw new Error("invalid base URL");
    }
  } catch {
    throw new PythonApiError(503, { error: "unavailable" });
  }
  let key: Buffer;
  try {
    const secretFile = process.env.CLASHLENS_PYTHON_HMAC_SECRET_FILE?.trim();
    const testSecret = process.env.CLASHLENS_PYTHON_HMAC_SECRET_B64;
    if (secretFile) key = loadSecretFile(secretFile);
    else if (process.env.NODE_ENV !== "production" && testSecret) {
      key = decodeSecretValue(testSecret);
    } else throw new Error("missing private API secret");
  } catch {
    throw new PythonApiError(503, { error: "unavailable" });
  }
  cachedConfig = { baseUrl, caller, keyId, key };
  return cachedConfig;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isString(value: unknown): value is string {
  return typeof value === "string";
}

function isUtcTimestamp(value: unknown): value is string {
  return (
    isString(value) && Number.isFinite(Date.parse(value)) && /(?:Z|\+00:00)$/.test(value)
  );
}

function isResetTimestamp(value: unknown): value is string {
  if (!isUtcTimestamp(value)) return false;
  const match = /^(\d{4}-\d{2}-\d{2})T05:00:00(?:Z|\+00:00)$/.exec(value);
  return (
    match !== null &&
    new Date(value).toISOString().slice(0, 19) === `${match[1]}T05:00:00`
  );
}

function isNullableString(value: unknown): value is string | null {
  return value === null || isString(value);
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

function isInteger(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value);
}

function isOneOf<T extends string>(value: unknown, values: readonly T[]): value is T {
  return isString(value) && values.includes(value as T);
}

function isCanonicalPlayerTag(value: unknown): value is string {
  return isString(value) && normalizePlayerTag(value) === value;
}

function mapRefresh(
  payload: unknown,
  kind: "refresh-work" | "refresh-status",
): RefreshWork | RefreshStatus {
  if (
    !isRecord(payload) ||
    !isString(payload.refresh_id) ||
    !isCanonicalUuid(payload.refresh_id) ||
    !isCanonicalPlayerTag(payload.tag) ||
    !isOneOf(payload.status, [
      "pending",
      "leased",
      "waiting_retry",
      "complete",
      "failed",
      "cancelled",
    ] as const) ||
    !isString(payload.outcome)
  ) {
    throw new PythonApiError(502, { error: "malformed" });
  }
  const raw = payload as {
    refresh_id: string;
    tag: string;
    status: "pending" | "leased" | "waiting_retry" | "complete" | "failed" | "cancelled";
    outcome: string;
  };
  const state =
    raw.status === "pending" || raw.status === "waiting_retry"
      ? "queued"
      : raw.status === "leased"
        ? "running"
        : raw.status === "complete"
          ? "complete"
          : "failed";
  const value = {
    kind,
    workId: raw.refresh_id,
    tag: raw.tag,
    state,
    progressPercent: state === "queued" ? 0 : state === "running" ? 50 : 100,
    message: raw.outcome,
    publishedAt: null,
  } as const;
  return kind === "refresh-status"
    ? ({ ...value, kind: "refresh-status", player: null } as RefreshStatus)
    : ({ ...value, kind: "refresh-work" } as RefreshWork);
}

function mapLeaderboard(payload: unknown, view: "live" | "daily"): TrackedLeaderboard {
  if (
    !isRecord(payload) ||
    !isOneOf(payload.kind, ["live", "frozen"] as const) ||
    (view === "live" ? payload.kind !== "live" : payload.kind !== "frozen") ||
    !Array.isArray(payload.entries) ||
    !isUtcTimestamp(payload.generated_at) ||
    !isInteger(payload.tracked_population) ||
    !isInteger(payload.total_entries) ||
    !isInteger(payload.page) ||
    !isInteger(payload.page_size) ||
    !isInteger(payload.page_count) ||
    typeof payload.has_previous !== "boolean" ||
    typeof payload.has_next !== "boolean" ||
    !isSnakeCoverage(payload.coverage) ||
    (payload.kind === "live"
      ? !isLiveLeaderboardProvenance(payload.provenance, payload.source_observations)
      : !isSnakeProvenance(payload.provenance)) ||
    !Array.isArray(payload.quality_states) ||
    !payload.quality_states.every((state) =>
      isOneOf(state, [
        "missing",
        "partial",
        "stale",
        "malformed",
        "unclassified",
        "uncertain",
        "rate-limited",
        "unavailable",
      ] as const),
    )
  )
    throw new PythonApiError(502, { error: "malformed" });
  const expectedPageCount = Math.ceil(payload.total_entries / payload.page_size);
  const firstPosition = (payload.page - 1) * payload.page_size + 1;
  const expectedEntries = Math.max(
    0,
    Math.min(payload.page_size, payload.total_entries - firstPosition + 1),
  );
  if (
    payload.tracked_population < 0 ||
    payload.total_entries < 0 ||
    payload.page < 1 ||
    payload.page_size < 1 ||
    payload.page_count !== expectedPageCount ||
    payload.page > Math.max(1, payload.page_count) ||
    payload.has_previous !== payload.page > 1 ||
    payload.has_next !== payload.page < payload.page_count ||
    payload.entries.length !== expectedEntries
  )
    throw new PythonApiError(502, { error: "malformed" });
  const sourceObservations =
    payload.kind === "live" ? mapSourceObservations(payload.source_observations) : null;
  const entries = payload.entries.map((entry, index) => {
    if (
      !isRecord(entry) ||
      !isInteger(entry.position) ||
      entry.position !== firstPosition + index ||
      !isCanonicalPlayerTag(entry.tag) ||
      !isNullableString(entry.name) ||
      !isNullableString(entry.clan) ||
      !isInteger(entry.trophies) ||
      !isString(entry.observed_at) ||
      !isFiniteNumber(entry.age_seconds) ||
      entry.age_seconds < 0 ||
      !isOneOf(entry.freshness, ["fresh", "stale"] as const) ||
      (payload.kind === "live"
        ? !isOneOf(entry.confidence, [
            "unknown",
            "eligible",
            "ineligible",
            "uncertain",
          ] as const)
        : !isOneOf(entry.confidence, ["exact", "confirmed", "uncertain"] as const)) ||
      !isOneOf(entry.public_confidence, ["high", "partial", "uncertain"] as const) ||
      !(entry.official_rank === null || isInteger(entry.official_rank))
    )
      throw new PythonApiError(502, { error: "malformed" });
    return {
      rank: entry.position ?? index + 1,
      tag: entry.tag,
      name: entry.name ?? "Unknown",
      clan: entry.clan ?? "Unknown",
      trophies: entry.trophies,
      freshness: {
        state: entry.freshness,
        observedAt: entry.observed_at,
        ageSeconds: entry.age_seconds,
      },
      state:
        entry.public_confidence === "uncertain"
          ? "uncertain"
          : entry.freshness === "stale"
            ? "stale"
            : "available",
      confidence: entry.public_confidence,
    };
  });
  const mapSelector = (value: unknown) => {
    if (
      !isRecord(value) ||
      !isString(value.official_season_id) ||
      value.official_season_id.length === 0 ||
      !isInteger(value.season_day_number) ||
      value.season_day_number < 1 ||
      value.season_day_number > 28
    )
      throw new PythonApiError(502, { error: "malformed" });
    return {
      officialSeasonId: value.official_season_id,
      dayNumber: value.season_day_number,
    };
  };
  const daily =
    payload.kind === "frozen"
      ? {
          ...mapSelector(payload),
          resetAt: isResetTimestamp(payload.reset_at)
            ? payload.reset_at
            : (() => {
                throw new PythonApiError(502, { error: "malformed" });
              })(),
          seasonStartAt: isResetTimestamp(payload.season_start_at)
            ? payload.season_start_at
            : (() => {
                throw new PythonApiError(502, { error: "malformed" });
              })(),
          seasonEndAt: isResetTimestamp(payload.season_end_at)
            ? payload.season_end_at
            : (() => {
                throw new PythonApiError(502, { error: "malformed" });
              })(),
          previousSnapshot:
            payload.previous_snapshot === null
              ? null
              : mapSelector(payload.previous_snapshot),
          nextSnapshot:
            payload.next_snapshot === null ? null : mapSelector(payload.next_snapshot),
        }
      : null;
  if (
    daily &&
    (Date.parse(daily.seasonEndAt) - Date.parse(daily.seasonStartAt) !==
      28 * 24 * 60 * 60 * 1000 ||
      Date.parse(daily.resetAt) - Date.parse(daily.seasonStartAt) !==
        daily.dayNumber * 24 * 60 * 60 * 1000)
  )
    throw new PythonApiError(502, { error: "malformed" });
  return {
    kind: "tracked-leaderboard",
    view,
    entries: entries as TrackedLeaderboard["entries"],
    totalTracked: payload.tracked_population,
    totalEntries: payload.total_entries,
    page: payload.page,
    pageSize: payload.page_size,
    pageCount: payload.page_count,
    generatedAt: payload.generated_at,
    hasPrevious: payload.has_previous,
    hasNext: payload.has_next,
    daily,
    coverage: mapSnakeCoverage(payload.coverage),
    provenance:
      payload.kind === "live"
        ? mapLiveLeaderboardProvenance(payload.provenance, payload.source_observations)
        : mapSnakeProvenance(payload.provenance as Record<string, unknown>),
    sourceObservations,
    qualityStates: payload.quality_states as TrackedLeaderboard["qualityStates"],
  };
}

function mapSearch(payload: unknown, submittedQuery: string): SearchResponse {
  if (
    !isRecord(payload) ||
    !isString(payload.query) ||
    !Array.isArray(payload.results) ||
    typeof payload.known_only !== "boolean"
  )
    throw new PythonApiError(502, { error: "malformed" });
  const results = payload.results.map((item) => {
    if (
      !isRecord(item) ||
      !isCanonicalPlayerTag(item.tag) ||
      !isString(item.name) ||
      !isInteger(item.trophies) ||
      !isOneOf(item.freshness, ["fresh", "stale"] as const) ||
      !isFiniteNumber(item.age_seconds) ||
      !isString(item.observed_at) ||
      !isOneOf(item.public_confidence, ["high", "partial", "uncertain"] as const)
    )
      throw new PythonApiError(502, { error: "malformed" });
    return {
      tag: item.tag,
      name: item.name,
      clan: isString(item.clan) ? item.clan : "Unknown",
      trophies: item.trophies,
      freshness: {
        state: item.freshness,
        observedAt: item.observed_at,
        ageSeconds: item.age_seconds,
      },
      state:
        item.public_confidence === "uncertain"
          ? "uncertain"
          : item.freshness === "stale"
            ? "stale"
            : "available",
      context: "Known Clash Lens player",
    };
  });
  return {
    kind: "player-search",
    query: payload.query,
    exactTag: normalizePlayerTag(submittedQuery),
    results: results as SearchResponse["results"],
    knownOnly: payload.known_only,
  };
}

function mapBattleArmy(value: unknown): BattleArmy | null {
  if (value === null) return null;
  if (
    !isRecord(value) ||
    !isOneOf(value.state, ["decoded", "partial", "failed"] as const) ||
    !(value.failure_reason === null || isString(value.failure_reason)) ||
    !Array.isArray(value.components) ||
    !Array.isArray(value.unknown_components) ||
    !isString(value.decoder_version) ||
    !isString(value.catalog_version)
  )
    throw new PythonApiError(502, { error: "malformed" });
  const components = value.components.map((item) => {
    if (
      !isRecord(item) ||
      !isString(item.typed_id) ||
      !isString(item.name) ||
      !isInteger(item.quantity) ||
      item.quantity < 1 ||
      !isString(item.origin)
    )
      throw new PythonApiError(502, { error: "malformed" });
    return {
      typedId: item.typed_id,
      name: item.name,
      quantity: item.quantity,
      origin: item.origin,
    };
  });
  const unknownComponents = value.unknown_components.map((item) => {
    if (
      !isRecord(item) ||
      !isInteger(item.numeric_id) ||
      !isInteger(item.quantity) ||
      !isString(item.section) ||
      !isString(item.origin)
    )
      throw new PythonApiError(502, { error: "malformed" });
    return {
      numericId: item.numeric_id,
      quantity: item.quantity,
      section: item.section,
      origin: item.origin,
    };
  });
  return {
    state: value.state,
    failureReason: value.failure_reason,
    components,
    unknownComponents,
    decoderVersion: value.decoder_version,
    catalogVersion: value.catalog_version,
  };
}

function mapArmyAnalytics(payload: unknown): ArmyAnalytics {
  if (
    !isRecord(payload) ||
    payload.kind !== "army-analytics" ||
    !isRecord(payload.selection) ||
    !Array.isArray(payload.rows) ||
    !isInteger(payload.total_attacks) ||
    !isInteger(payload.usable_army_sample) ||
    !isRecord(payload.army_states) ||
    !Object.values(payload.army_states).every(isInteger) ||
    typeof payload.army_states_sum_confirmed !== "boolean" ||
    !isInteger(payload.unknown_affected_attacks) ||
    !isInteger(payload.unknown_component_occurrences) ||
    !isInteger(payload.perspective_disagreement_count) ||
    !isInteger(payload.missing_trophy_membership_evidence) ||
    !isRecord(payload.cohort_evidence) ||
    !isInteger(payload.cohort_evidence.stale_or_uncertain_cohort_members) ||
    !isInteger(payload.cohort_evidence.streak_excluded_players) ||
    !isInteger(payload.cohort_evidence.shielded_player_days) ||
    !isRecord(payload.collection_coverage) ||
    !isString(payload.collection_coverage.state) ||
    !isInteger(payload.collection_coverage.completed_days) ||
    !isRecord(payload.freshness) ||
    !isString(payload.freshness.state) ||
    !isRecord(payload.reproducibility) ||
    !isString(payload.reproducibility.official_season_id) ||
    !Array.isArray(payload.reproducibility.legend_days) ||
    payload.reproducibility.legend_days.length !== 2 ||
    !payload.reproducibility.legend_days.every(isInteger) ||
    !Array.isArray(payload.reproducibility.snapshot_versions) ||
    !payload.reproducibility.snapshot_versions.every(isInteger) ||
    !isRecord(payload.versions) ||
    !isString(payload.publication_identity)
  )
    throw new PythonApiError(502, { error: "malformed" });
  const selection = payload.selection;
  if (
    !isOneOf(selection.lens, ["offense", "defense"] as const) ||
    !isString(selection.season) ||
    !isInteger(selection.start_day) ||
    !isInteger(selection.end_day) ||
    !isString(selection.population) ||
    !isString(selection.category) ||
    !isString(selection.sort) ||
    !isString(payload.versions.decoder) ||
    !isString(payload.versions.catalog) ||
    !isString(payload.versions.analytics)
  )
    throw new PythonApiError(502, { error: "malformed" });
  const rows = payload.rows.map((row) => {
    if (
      !isRecord(row) ||
      !isString(row.key) ||
      !isString(row.label) ||
      !isInteger(row.usage_count) ||
      !isInteger(row.usage_denominator) ||
      !isFiniteNumber(row.usage_rate) ||
      !Array.isArray(row.star_counts) ||
      row.star_counts.length !== 4 ||
      !row.star_counts.every(isInteger) ||
      !Array.isArray(row.star_rates) ||
      row.star_rates.length !== 4 ||
      !row.star_rates.every(isFiniteNumber) ||
      !isFiniteNumber(row.three_star_rate) ||
      !isFiniteNumber(row.average_stars) ||
      !isFiniteNumber(row.average_destruction) ||
      !isInteger(row.unknown_excluded_attacks)
    )
      throw new PythonApiError(502, { error: "malformed" });
    return {
      key: row.key,
      label: row.label,
      usageCount: row.usage_count,
      usageDenominator: row.usage_denominator,
      usageRate: row.usage_rate,
      starCounts: row.star_counts as [number, number, number, number],
      starRates: row.star_rates as [number, number, number, number],
      threeStarRate: row.three_star_rate,
      averageStars: row.average_stars,
      averageDestruction: row.average_destruction,
      unknownExcludedAttacks: row.unknown_excluded_attacks,
    };
  });
  const armyStates = Object.fromEntries(
    Object.entries(payload.army_states).map(([state, count]) => {
      if (!isInteger(count)) throw new PythonApiError(502, { error: "malformed" });
      return [state, count];
    }),
  );
  return {
    kind: "army-analytics",
    selection: {
      lens: selection.lens,
      season: selection.season,
      startDay: selection.start_day,
      endDay: selection.end_day,
      population: selection.population,
      category: selection.category,
      sort: selection.sort,
    },
    totalAttacks: payload.total_attacks,
    usableArmySample: payload.usable_army_sample,
    armyStates,
    armyStatesSumConfirmed: payload.army_states_sum_confirmed,
    unknownAffectedAttacks: payload.unknown_affected_attacks,
    unknownComponentOccurrences: payload.unknown_component_occurrences,
    perspectiveDisagreementCount: payload.perspective_disagreement_count,
    missingTrophyMembershipEvidence: payload.missing_trophy_membership_evidence,
    cohortEvidence: {
      staleOrUncertainCohortMembers:
        payload.cohort_evidence.stale_or_uncertain_cohort_members,
      streakExcludedPlayers: payload.cohort_evidence.streak_excluded_players,
      shieldedPlayerDays: payload.cohort_evidence.shielded_player_days,
    },
    collectionCoverage: {
      state: payload.collection_coverage.state,
      completedDays: payload.collection_coverage.completed_days,
    },
    freshness: { state: payload.freshness.state },
    reproducibility: {
      officialSeasonId: payload.reproducibility.official_season_id,
      legendDays: payload.reproducibility.legend_days as [number, number],
      snapshotVersions: payload.reproducibility.snapshot_versions,
    },
    versions: {
      decoder: payload.versions.decoder,
      catalog: payload.versions.catalog,
      analytics: payload.versions.analytics,
    },
    publicationIdentity: payload.publication_identity,
    rows,
  };
}

function mapPlayerPage(payload: unknown): PlayerPage {
  if (
    !isRecord(payload) ||
    !isCanonicalPlayerTag(payload.tag) ||
    !isString(payload.name) ||
    !isInteger(payload.trophies) ||
    !isRecord(payload.screen_ready)
  )
    throw new PythonApiError(502, { error: "malformed" });
  const screen = payload.screen_ready;
  const mapEvent = (value: unknown, lens: "offense" | "defense"): RankedBattleEvent => {
    if (
      !isRecord(value) ||
      !isString(value.battle_id) ||
      value.battle_id.length === 0 ||
      !isUtcTimestamp(value.battle_timestamp) ||
      !isRecord(value.opponent) ||
      !isCanonicalPlayerTag(value.opponent.tag) ||
      !isNullableString(value.opponent.name) ||
      !isInteger(value.destruction_percentage) ||
      value.destruction_percentage < 0 ||
      value.destruction_percentage > 100 ||
      !isInteger(value.stars) ||
      value.stars < 0 ||
      value.stars > 3 ||
      !isInteger(value.trophy_change) ||
      (lens === "offense" && value.trophy_change < 0) ||
      (lens === "defense" && value.trophy_change > 0)
    )
      throw new PythonApiError(502, { error: "malformed" });
    return {
      battleId: value.battle_id,
      battleTimestamp: value.battle_timestamp,
      opponent: {
        tag: value.opponent.tag,
        name: value.opponent.name,
      },
      destructionPercentage: value.destruction_percentage,
      stars: value.stars,
      trophyChange: value.trophy_change,
      perspectiveDisagreement: value.perspective_disagreement === true,
      army: mapBattleArmy(value.army ?? null),
    };
  };
  const mapDay = (value: unknown) => {
    if (
      !isRecord(value) ||
      !isString(value.ranked_day_start) ||
      !isNullableString(value.ranked_day_end) ||
      !isOneOf(value.state, ["Live", "Complete", "Partial"] as const) ||
      !(
        value.confidence === null ||
        isOneOf(value.confidence, ["exact", "inferred", "partial", "uncertain"] as const)
      ) ||
      !isRecord(value.completeness) ||
      !isOneOf(value.completeness.state, ["complete", "partial", "uncertain"] as const) ||
      !isString(value.completeness.reason) ||
      !isOneOf(value.public_confidence, ["high", "partial", "uncertain"] as const) ||
      !Array.isArray(value.uncertainty_reasons) ||
      !value.uncertainty_reasons.every(isString) ||
      !(value.season_day_number === null || isInteger(value.season_day_number)) ||
      !Array.isArray(value.offense_events) ||
      value.offense_events.length > 8 ||
      !Array.isArray(value.defense_events) ||
      value.defense_events.length > 8
    )
      throw new PythonApiError(502, { error: "malformed" });
    const valid = [
      value.attack_count,
      value.attack_three_star_count,
      value.attack_gain,
      value.defense_count,
      value.defense_three_star_count,
      value.defense_loss,
      value.net_trophy_change,
    ].every((item) => item === null || isInteger(item));
    if (!valid) throw new PythonApiError(502, { error: "malformed" });
    return {
      dayNumber: value.season_day_number as number | null,
      label: "Ranked day",
      period: isString(value.ranked_day_end)
        ? `${value.ranked_day_start} – ${value.ranked_day_end}`
        : value.ranked_day_start,
      state: value.state,
      offense: {
        attacks: value.attack_count as number | null,
        threeStars: value.attack_three_star_count as number | null,
        trophyGain: value.attack_gain as number | null,
      },
      defense: {
        defenses: value.defense_count as number | null,
        threeStarsAgainst: value.defense_three_star_count as number | null,
        trophyLoss: value.defense_loss as number | null,
      },
      trophyChange: value.net_trophy_change as number | null,
      offenseEvents: value.offense_events.map((event) => mapEvent(event, "offense")),
      defenseEvents: value.defense_events.map((event) => mapEvent(event, "defense")),
      completeness: {
        state: value.completeness.state as "complete" | "partial" | "uncertain",
        reason: value.completeness.reason as string,
      },
      uncertainty: value.uncertainty_reasons as string[],
    };
  };
  if (screen.current_day !== null && screen.current_day !== undefined)
    mapDay(screen.current_day);
  if (!Array.isArray(screen.recent_days))
    throw new PythonApiError(502, { error: "malformed" });
  if (!Array.isArray(screen.season_days))
    throw new PythonApiError(502, { error: "malformed" });
  return {
    kind: "player-page",
    tag: payload.tag,
    profile: {
      tag: payload.tag,
      name: payload.name,
      clan: isString(payload.clan) ? payload.clan : "Unknown",
      trophies: payload.trophies,
      freshness: {
        state:
          payload.freshness === "fresh" || payload.freshness === "stale"
            ? payload.freshness
            : "unknown",
        observedAt: isString(payload.observed_at) ? payload.observed_at : "",
        ageSeconds: isFiniteNumber(payload.age_seconds) ? payload.age_seconds : 0,
      },
      confidence: isOneOf(payload.public_confidence, [
        "high",
        "partial",
        "uncertain",
      ] as const)
        ? payload.public_confidence
        : "uncertain",
      coverage: isSnakeProvenance(screen.provenance)
        ? (screen.provenance.coverage as "complete" | "partial" | "missing" | "unknown")
        : "missing",
      eligibility: payload.eligibility === "eligible" ? "legend-i" : "uncertain",
    },
    season: mapSeason(screen.season),
    currentDay: screen.current_day === null ? null : mapDay(screen.current_day),
    recentDays: screen.recent_days.map(mapDay),
    seasonDays: screen.season_days.map(mapDay),
    dataQuality: mapDataQuality(screen.data_quality),
    provenance: mapSnakeProvenanceRequired(screen.provenance),
  };
}

function isSnakeCoverage(value: unknown): value is Record<string, unknown> {
  return (
    isRecord(value) &&
    isOneOf(value.state, ["complete", "partial", "missing", "unknown"] as const) &&
    isInteger(value.tracked_players) &&
    value.tracked_players >= 0 &&
    isFiniteNumber(value.measured_percent) &&
    value.measured_percent >= 0 &&
    value.measured_percent <= 100 &&
    isString(value.note)
  );
}

function mapSnakeCoverage(
  value: Record<string, unknown>,
): TrackedLeaderboard["coverage"] {
  return {
    state: value.state as TrackedLeaderboard["coverage"]["state"],
    trackedPlayers: value.tracked_players as number,
    measuredPercent: value.measured_percent as number,
    note: value.note as string,
  };
}

function isSnakeProvenance(value: unknown): value is Record<string, unknown> {
  return (
    isRecord(value) &&
    isString(value.source) &&
    isUtcTimestamp(value.observed_at) &&
    isOneOf(value.freshness, ["fresh", "stale", "unknown"] as const) &&
    isOneOf(value.confidence, ["high", "partial", "uncertain"] as const) &&
    isOneOf(value.coverage, ["complete", "partial", "missing", "unknown"] as const) &&
    isString(value.version)
  );
}

function isLiveLeaderboardProvenance(
  value: unknown,
  sourceObservations: unknown,
): value is Record<string, unknown> {
  if (isSnakeProvenance(value)) return true;
  return (
    isRecord(value) &&
    value.observed_at === null &&
    isString(value.source) &&
    isOneOf(value.freshness, ["fresh", "stale", "unknown"] as const) &&
    isOneOf(value.confidence, ["high", "partial", "uncertain"] as const) &&
    isOneOf(value.coverage, ["complete", "partial", "missing", "unknown"] as const) &&
    isString(value.version) &&
    isRecord(sourceObservations) &&
    sourceObservations.oldest_observed_at === null &&
    sourceObservations.newest_observed_at === null &&
    sourceObservations.stale_count === 0
  );
}

function mapSnakeProvenance(value: Record<string, unknown>) {
  return {
    source: value.source as string,
    observedAt: value.observed_at as string,
    freshness: value.freshness as "fresh" | "stale" | "unknown",
    confidence: value.confidence as "high" | "partial" | "uncertain",
    coverage: value.coverage as "complete" | "partial" | "missing" | "unknown",
    version: value.version as string,
  };
}

function mapLiveLeaderboardProvenance(value: unknown, sourceObservations: unknown) {
  if (!isLiveLeaderboardProvenance(value, sourceObservations))
    throw new PythonApiError(502, { error: "malformed" });
  return {
    source: value.source as string,
    observedAt: value.observed_at as string | null,
    freshness: value.freshness as "fresh" | "stale" | "unknown",
    confidence: value.confidence as "high" | "partial" | "uncertain",
    coverage: value.coverage as "complete" | "partial" | "missing" | "unknown",
    version: value.version as string,
  };
}

function mapSnakeProvenanceRequired(value: unknown) {
  if (!isSnakeProvenance(value)) throw new PythonApiError(502, { error: "malformed" });
  return mapSnakeProvenance(value);
}

function mapSourceObservations(value: unknown): TrackedLeaderboard["sourceObservations"] {
  if (
    !isRecord(value) ||
    !(value.oldest_observed_at === null || isUtcTimestamp(value.oldest_observed_at)) ||
    !(value.newest_observed_at === null || isUtcTimestamp(value.newest_observed_at)) ||
    !isInteger(value.stale_count) ||
    value.stale_count < 0
  )
    throw new PythonApiError(502, { error: "malformed" });
  return {
    oldestObservedAt: value.oldest_observed_at,
    newestObservedAt: value.newest_observed_at,
    staleCount: value.stale_count,
  };
}

function mapSeason(value: unknown): PlayerPage["season"] {
  if (value === null) return null;
  if (
    !isRecord(value) ||
    !isString(value.id) ||
    !isString(value.start) ||
    !isString(value.end) ||
    !isInteger(value.current_day_number)
  )
    throw new PythonApiError(502, { error: "malformed" });
  return {
    id: value.id,
    anchor: value.start,
    currentDayNumber: value.current_day_number,
    dayCount: 28,
  };
}

function mapDataQuality(value: unknown): PlayerPage["dataQuality"] {
  if (
    !Array.isArray(value) ||
    !value.every(
      (item) =>
        isRecord(item) &&
        isOneOf(item.code, [
          "stale",
          "partial",
          "uncertain",
          "unavailable",
          "malformed",
          "unclassified",
          "rate-limited",
        ] as const) &&
        isString(item.label) &&
        isString(item.detail),
    )
  )
    throw new PythonApiError(502, { error: "malformed" });
  return value as PlayerPage["dataQuality"];
}

type AccountOperations = Pick<
  PythonClient,
  | "createAccount"
  | "getAccount"
  | "updateAccount"
  | "listSavedTags"
  | "addSavedTag"
  | "removeSavedTag"
  | "listGroups"
  | "createGroup"
  | "updateGroup"
  | "deleteGroup"
  | "getAccountSummary"
  | "linkProvider"
  | "unlinkProvider"
  | "getPublicUser"
  | "verifyPlayerToken"
>;

function createAccountOperations(
  identity: GoogleAccountIdentity | undefined,
  accountReadTimeoutMs: number,
): AccountOperations {
  if (identity !== undefined) {
    if (
      (identity.provider !== "google" && identity.provider !== "discord") ||
      !isProviderSubject(identity.providerSubject)
    ) {
      throw new Error("account client requires a bounded provider identity");
    }
  }
  return {
    createAccount: (input, idempotencyKey) =>
      createAccount(input, idempotencyKey, identity),
    getAccount: () => getAccount(identity, accountReadTimeoutMs),
    updateAccount: (input, idempotencyKey) =>
      updateAccount(input, idempotencyKey, identity),
    listSavedTags: () => listSavedTags(identity),
    addSavedTag: (tag, idempotencyKey) => addSavedTag(tag, idempotencyKey, identity),
    removeSavedTag: (tag, idempotencyKey) =>
      removeSavedTag(tag, idempotencyKey, identity),
    listGroups: () => listGroups(identity),
    createGroup: (input, idempotencyKey) => createGroup(input, idempotencyKey, identity),
    updateGroup: (groupId, input, idempotencyKey) =>
      updateGroup(groupId, input, idempotencyKey, identity),
    deleteGroup: (groupId, idempotencyKey) =>
      deleteGroup(groupId, idempotencyKey, identity),
    getAccountSummary: () => getAccountSummary(identity),
    linkProvider: (provider, providerSubject, idempotencyKey) =>
      changeProviderIdentity("link", provider, providerSubject, idempotencyKey, identity),
    unlinkProvider: (provider, providerSubject, idempotencyKey) =>
      changeProviderIdentity(
        "unlink",
        provider,
        providerSubject,
        idempotencyKey,
        identity,
      ),
    getPublicUser,
    verifyPlayerToken: (tag, token, idempotencyKey) =>
      verifyPlayerToken(tag, token, idempotencyKey, identity),
  };
}

function requireIdentity(
  identity: GoogleAccountIdentity | undefined,
): asserts identity is GoogleAccountIdentity {
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
  identity: GoogleAccountIdentity | undefined,
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
  const payload = await requestJson<unknown>(
    "/v1/account",
    "POST",
    jsonBody({ username, display_name: displayName }),
    undefined,
    idempotencyKey,
    identity,
  );
  return mappedOrMalformed(mapAccount(payload));
}

async function getAccount(
  identity: GoogleAccountIdentity | undefined,
  timeoutMs = REQUEST_TIMEOUT_MS,
): Promise<ClashLensAccount> {
  requireIdentity(identity);
  const payload = await requestJson<unknown>(
    "/v1/account",
    "GET",
    undefined,
    undefined,
    undefined,
    identity,
    undefined,
    timeoutMs,
  );
  return mappedOrMalformed(mapAccount(payload));
}

async function updateAccount(
  input: AccountNameInput & { preferences: Record<string, unknown> },
  idempotencyKey: string,
  identity: GoogleAccountIdentity | undefined,
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
  const payload = await requestJson<unknown>(
    "/v1/account",
    "PATCH",
    jsonBody({ username, display_name: displayName, preferences }),
    undefined,
    idempotencyKey,
    identity,
  );
  return mappedOrMalformed(mapAccount(payload));
}

async function listSavedTags(
  identity: GoogleAccountIdentity | undefined,
): Promise<SavedPlayer[]> {
  requireIdentity(identity);
  const payload = await requestJson<unknown>(
    "/v1/account/saved-tags",
    "GET",
    undefined,
    undefined,
    undefined,
    identity,
  );
  return mappedOrMalformed(mapSavedTags(payload));
}

async function addSavedTag(
  tag: string,
  idempotencyKey: string,
  identity: GoogleAccountIdentity | undefined,
): Promise<SavedTagResult> {
  requireIdentity(identity);
  if (!isCanonicalUuid(idempotencyKey)) {
    throw new PythonApiError(400, { error: "invalid_input" });
  }
  const normalized = normalizeSubmittedPlayerTag(tag);
  if (normalized === null) {
    throw new PythonApiError(422, { error: "invalid_tag" });
  }
  const payload = await requestJson<unknown>(
    "/v1/account/saved-tags",
    "POST",
    jsonBody({ tag: normalized }),
    undefined,
    idempotencyKey,
    identity,
  );
  return mappedOrMalformed(mapSavedTagResult(payload));
}

async function removeSavedTag(
  tag: string,
  idempotencyKey: string,
  identity: GoogleAccountIdentity | undefined,
): Promise<SavedTagResult> {
  requireIdentity(identity);
  if (!isCanonicalUuid(idempotencyKey)) {
    throw new PythonApiError(400, { error: "invalid_input" });
  }
  const normalized = normalizeSubmittedPlayerTag(tag);
  if (normalized === null) {
    throw new PythonApiError(422, { error: "invalid_tag" });
  }
  const payload = await requestJson<unknown>(
    `/v1/account/saved-tags/${encodeURIComponent(normalized)}`,
    "DELETE",
    undefined,
    undefined,
    idempotencyKey,
    identity,
  );
  return mappedOrMalformed(mapSavedTagResult(payload));
}

async function listGroups(
  identity: GoogleAccountIdentity | undefined,
): Promise<PrivateGroup[]> {
  requireIdentity(identity);
  const payload = await requestJson<unknown>(
    "/v1/account/groups",
    "GET",
    undefined,
    undefined,
    undefined,
    identity,
  );
  return mappedOrMalformed(mapGroups(payload));
}

async function createGroup(
  input: GroupInput,
  idempotencyKey: string,
  identity: GoogleAccountIdentity | undefined,
): Promise<PrivateGroup> {
  requireIdentity(identity);
  if (!isCanonicalUuid(idempotencyKey)) {
    throw new PythonApiError(400, { error: "invalid_input" });
  }
  const name = normalizeGroupName(input.name);
  const tags = normalizeTagList(input.tags);
  if (name === null || tags === null) {
    throw new PythonApiError(422, { error: "invalid_request" });
  }
  const payload = await requestJson<unknown>(
    "/v1/account/groups",
    "POST",
    jsonBody({ name, tags }),
    undefined,
    idempotencyKey,
    identity,
  );
  return mappedOrMalformed(mapGroupResult(payload));
}

async function updateGroup(
  groupId: string,
  input: GroupInput,
  idempotencyKey: string,
  identity: GoogleAccountIdentity | undefined,
): Promise<PrivateGroup> {
  requireIdentity(identity);
  if (!isCanonicalUuid(idempotencyKey) || !isCanonicalUuid(groupId)) {
    throw new PythonApiError(400, { error: "invalid_input" });
  }
  const name = normalizeGroupName(input.name);
  const tags = normalizeTagList(input.tags);
  if (name === null || tags === null) {
    throw new PythonApiError(422, { error: "invalid_request" });
  }
  const payload = await requestJson<unknown>(
    `/v1/account/groups/${groupId}`,
    "PATCH",
    jsonBody({ name, tags }),
    undefined,
    idempotencyKey,
    identity,
  );
  return mappedOrMalformed(mapGroupResult(payload));
}

async function deleteGroup(
  groupId: string,
  idempotencyKey: string,
  identity: GoogleAccountIdentity | undefined,
): Promise<GroupDeleteResult> {
  requireIdentity(identity);
  if (!isCanonicalUuid(idempotencyKey) || !isCanonicalUuid(groupId)) {
    throw new PythonApiError(400, { error: "invalid_input" });
  }
  const payload = await requestJson<unknown>(
    `/v1/account/groups/${groupId}`,
    "DELETE",
    undefined,
    undefined,
    idempotencyKey,
    identity,
  );
  return mappedOrMalformed(mapGroupDeleteResult(payload));
}

async function getAccountSummary(
  identity: GoogleAccountIdentity | undefined,
): Promise<AccountSummary> {
  requireIdentity(identity);
  const payload = await requestJson<unknown>(
    "/v1/account/summary",
    "GET",
    undefined,
    undefined,
    undefined,
    identity,
  );
  return mappedOrMalformed(mapSummary(payload));
}

const ALLOWED_PROVIDERS: readonly ("google" | "discord")[] = ["google", "discord"];

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
  identity: LoginProviderIdentity | undefined,
): Promise<{ providers: string[] }> {
  requireIdentity(identity);
  if (!isCanonicalUuid(idempotencyKey)) {
    throw new PythonApiError(400, { error: "invalid_input" });
  }
  if (!ALLOWED_PROVIDERS.includes(provider) || !isProviderSubject(providerSubject)) {
    throw new PythonApiError(422, { error: "invalid_request" });
  }
  const payload = await requestJson<unknown>(
    `/v1/account/providers/${provider}`,
    action === "link" ? "POST" : "DELETE",
    jsonBody({ provider_subject: providerSubject }),
    undefined,
    idempotencyKey,
    identity,
  );
  if (
    !isRecord(payload) ||
    !Array.isArray(payload.providers) ||
    !payload.providers.every(isString)
  ) {
    throw new PythonApiError(502, { error: "malformed" });
  }
  return { providers: [...payload.providers] };
}

async function getPublicUser(username: string): Promise<PublicUser> {
  const normalized = normalizeUsername(username);
  if (normalized === null) {
    throw new PythonApiError(404, { error: "user_not_found" });
  }
  const payload = await requestJson<unknown>(
    `/v1/users/${encodeURIComponent(normalized)}`,
    "GET",
    undefined,
    undefined,
    undefined,
  );
  return mappedOrMalformed(mapPublicUser(payload));
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
  identity: GoogleAccountIdentity | undefined,
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
    "POST",
    jsonBody({ token }),
    idempotencyKey,
    identity,
  );
  if (status >= 200 && status < 300) {
    return mappedOrMalformed(mapVerificationResult(payload));
  }
  if (isRecord(payload) && typeof payload.status === "string") {
    return mappedOrMalformed(mapVerificationResult(payload));
  }
  throw new PythonApiError(status, payload);
}
