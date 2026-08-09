import type {
  PlayerPage,
  RefreshStatus,
  RefreshWork,
  SearchResponse,
  TrackedLeaderboard,
} from "../lib/contracts";
import { normalizePlayerTag } from "../lib/player-tag";
import { isCanonicalUuid, MAX_SEARCH_QUERY_LENGTH } from "../lib/validation";
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
  ): Promise<TrackedLeaderboard>;
  searchPlayers(query: string, limit?: number): Promise<SearchResponse>;
  getPlayer(tag: string): Promise<PlayerPage>;
  requestRefresh(tag: string, idempotencyKey: string): Promise<RefreshWork>;
  getRefreshStatus(workId: string, tag: string): Promise<RefreshStatus>;
}

export function createPythonClient(): PythonClient {
  return {
    getTrackedLeaderboard,
    searchPlayers,
    getPlayer: getPlayerPage,
    requestRefresh: requestPlayerRefresh,
    getRefreshStatus,
  };
}

async function getTrackedLeaderboard(
  limit = 25,
  view: "live" | "daily" = "live",
): Promise<TrackedLeaderboard> {
  const payload = await requestJson<unknown>(
    `/v1/leaderboards/${view === "live" ? "live" : "frozen"}?limit=${String(limit)}`,
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
  return mapSearch(payload);
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
  method: "GET" | "POST",
  body: Buffer | undefined,
  expectedKind: string | string[] | undefined,
  requestId?: string,
): Promise<T> {
  const config = getConfig();
  const proof = createProofHeaders({
    key: config.key,
    caller: config.caller,
    keyId: config.keyId,
    method,
    rawTarget: target,
    body,
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
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
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
  if (!response.ok) {
    throw new PythonApiError(response.status, payload);
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
  if (!isValidResponsePayload(payload)) {
    throw new PythonApiError(502, { error: "malformed" });
  }
  return payload as T;
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

function isRefreshWorkId(value: unknown): value is string {
  return isString(value) && /^[A-Za-z0-9_-]{1,128}$/.test(value);
}

function isFreshness(value: unknown): boolean {
  return (
    isRecord(value) &&
    isOneOf(value.state, ["fresh", "stale", "unknown"] as const) &&
    isString(value.observedAt) &&
    isFiniteNumber(value.ageSeconds) &&
    value.ageSeconds >= 0
  );
}

function isProvenance(value: unknown): boolean {
  return (
    isRecord(value) &&
    isString(value.source) &&
    isString(value.observedAt) &&
    isOneOf(value.freshness, ["fresh", "stale", "unknown"] as const) &&
    isOneOf(value.confidence, ["high", "partial", "uncertain"] as const) &&
    isOneOf(value.coverage, ["complete", "partial", "missing", "unknown"] as const) &&
    isString(value.version)
  );
}

function isTrackedEntry(value: unknown): boolean {
  return (
    isRecord(value) &&
    isInteger(value.rank) &&
    value.rank > 0 &&
    isCanonicalPlayerTag(value.tag) &&
    isString(value.name) &&
    isString(value.clan) &&
    isInteger(value.trophies) &&
    isFreshness(value.freshness) &&
    isOneOf(value.state, ["available", "stale", "uncertain"] as const) &&
    isOneOf(value.confidence, ["high", "partial", "uncertain"] as const) &&
    (value.officialRank === null ||
      (isInteger(value.officialRank) &&
        value.officialRank > 0 &&
        value.officialRank <= 200))
  );
}

function isTrackedLeaderboard(value: Record<string, unknown>): boolean {
  return (
    value.kind === "tracked-leaderboard" &&
    isOneOf(value.view, ["live", "daily"] as const) &&
    Array.isArray(value.entries) &&
    value.entries.every(isTrackedEntry) &&
    isInteger(value.totalTracked) &&
    isRecord(value.coverage) &&
    isOneOf(value.coverage.state, [
      "complete",
      "partial",
      "missing",
      "unknown",
    ] as const) &&
    isInteger(value.coverage.trackedPlayers) &&
    isFiniteNumber(value.coverage.measuredPercent) &&
    isString(value.coverage.note) &&
    isProvenance(value.provenance) &&
    Array.isArray(value.qualityStates) &&
    value.qualityStates.every(isString)
  );
}

function isKnownPlayerResult(value: unknown): boolean {
  return (
    isRecord(value) &&
    isCanonicalPlayerTag(value.tag) &&
    isString(value.name) &&
    isString(value.clan) &&
    isInteger(value.trophies) &&
    isFreshness(value.freshness) &&
    isOneOf(value.state, ["available", "stale", "uncertain"] as const) &&
    isString(value.context)
  );
}

function isSearchResponse(value: Record<string, unknown>): boolean {
  return (
    value.kind === "player-search" &&
    isString(value.query) &&
    (value.exactTag === null || isCanonicalPlayerTag(value.exactTag)) &&
    Array.isArray(value.results) &&
    value.results.every(isKnownPlayerResult) &&
    typeof value.knownOnly === "boolean"
  );
}

function isProfile(value: unknown): boolean {
  return (
    isRecord(value) &&
    isCanonicalPlayerTag(value.tag) &&
    isString(value.name) &&
    isString(value.clan) &&
    isInteger(value.trophies) &&
    isFreshness(value.freshness) &&
    isOneOf(value.confidence, ["high", "partial", "uncertain"] as const) &&
    isOneOf(value.coverage, ["complete", "partial", "missing", "unknown"] as const) &&
    isOneOf(value.eligibility, ["legend-i", "uncertain"] as const)
  );
}

function isDaySummary(value: unknown): boolean {
  if (!isRecord(value)) return false;
  const offense = value.offense;
  const defense = value.defense;
  const completeness = value.completeness;
  return (
    isInteger(value.dayNumber) &&
    value.dayNumber > 0 &&
    isString(value.label) &&
    isString(value.period) &&
    isOneOf(value.state, ["Live", "Complete", "Partial", "Uncertain"] as const) &&
    isRecord(offense) &&
    isInteger(offense.attacks) &&
    isInteger(offense.threeStars) &&
    isInteger(offense.trophyGain) &&
    isRecord(defense) &&
    isInteger(defense.defenses) &&
    isInteger(defense.threeStarsAgainst) &&
    isInteger(defense.trophyLoss) &&
    isInteger(value.trophyChange) &&
    isRecord(completeness) &&
    isOneOf(completeness.state, ["complete", "partial", "uncertain"] as const) &&
    isString(completeness.reason) &&
    Array.isArray(value.uncertainty) &&
    value.uncertainty.every(isString)
  );
}

function isPlayerPage(value: Record<string, unknown>): boolean {
  return (
    value.kind === "player-page" &&
    isCanonicalPlayerTag(value.tag) &&
    isRecord(value.profile) &&
    isProfile(value.profile) &&
    value.profile.tag === value.tag &&
    isRecord(value.season) &&
    isString(value.season.id) &&
    isString(value.season.anchor) &&
    isInteger(value.season.currentDayNumber) &&
    isInteger(value.season.dayCount) &&
    isDaySummary(value.currentDay) &&
    Array.isArray(value.recentDays) &&
    value.recentDays.every(isDaySummary) &&
    Array.isArray(value.dataQuality) &&
    value.dataQuality.every(
      (item) =>
        isRecord(item) &&
        isString(item.code) &&
        isString(item.label) &&
        isString(item.detail),
    ) &&
    isProvenance(value.provenance)
  );
}

function isRefreshPayload(value: Record<string, unknown>): boolean {
  const validWorkState = [
    "queued",
    "running",
    "complete",
    "unavailable",
    "failed",
  ] as const;
  return (
    (value.kind === "refresh-work" || value.kind === "refresh-status") &&
    isRefreshWorkId(value.workId) &&
    isCanonicalPlayerTag(value.tag) &&
    isOneOf(value.state, validWorkState) &&
    isInteger(value.progressPercent) &&
    value.progressPercent >= 0 &&
    value.progressPercent <= 100 &&
    isString(value.message) &&
    isNullableString(value.publishedAt) &&
    (value.kind === "refresh-work" || "player" in value) &&
    (value.player === undefined ||
      value.player === null ||
      (isRecord(value.player) &&
        isPlayerPage(value.player) &&
        value.player.tag === value.tag))
  );
}

function mapRefresh(
  payload: unknown,
  kind: "refresh-work" | "refresh-status",
): RefreshWork | RefreshStatus {
  if (isRecord(payload) && isRefreshPayload(payload)) {
    return payload as unknown as RefreshWork | RefreshStatus;
  }
  if (
    !isRecord(payload) ||
    !isString(payload.refresh_id) ||
    !isCanonicalUuid(payload.refresh_id) ||
    !isCanonicalPlayerTag(payload.tag) ||
    !isOneOf(payload.status, [
      "pending",
      "leased",
      "running",
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
    status: "pending" | "leased" | "running" | "complete" | "failed" | "cancelled";
    outcome: string;
  };
  const state =
    raw.status === "pending"
      ? "queued"
      : raw.status === "leased" || raw.status === "running"
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
  if (isRecord(payload) && isTrackedLeaderboard(payload))
    return payload as unknown as TrackedLeaderboard;
  if (
    !isRecord(payload) ||
    !isOneOf(payload.kind, ["live", "frozen"] as const) ||
    !Array.isArray(payload.entries) ||
    !isInteger(payload.tracked_population) ||
    !isSnakeCoverage(payload.coverage) ||
    !isSnakeProvenance(payload.provenance) ||
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
  const entries = payload.entries.map((entry, index) => {
    if (
      !isRecord(entry) ||
      !isInteger(entry.position) ||
      entry.position < 1 ||
      !isCanonicalPlayerTag(entry.tag) ||
      !isNullableString(entry.name) ||
      !isNullableString(entry.clan) ||
      !isInteger(entry.trophies) ||
      !isString(entry.observed_at) ||
      !isFiniteNumber(entry.age_seconds) ||
      entry.age_seconds < 0 ||
      !isOneOf(entry.freshness, ["fresh", "stale", "unknown"] as const) ||
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
      officialRank: entry.official_rank,
    };
  });
  return {
    kind: "tracked-leaderboard",
    view,
    entries: entries as TrackedLeaderboard["entries"],
    totalTracked: payload.tracked_population,
    coverage: mapSnakeCoverage(payload.coverage),
    provenance: mapSnakeProvenance(payload.provenance),
    qualityStates: payload.quality_states as TrackedLeaderboard["qualityStates"],
  };
}

function mapSearch(payload: unknown): SearchResponse {
  if (isRecord(payload) && isSearchResponse(payload))
    return payload as unknown as SearchResponse;
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
      !isOneOf(item.freshness, ["fresh", "stale", "unknown"] as const) ||
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
    exactTag: null,
    results: results as SearchResponse["results"],
    knownOnly: payload.known_only,
  };
}

function mapPlayerPage(payload: unknown): PlayerPage {
  if (isRecord(payload) && isPlayerPage(payload)) return payload as unknown as PlayerPage;
  if (
    !isRecord(payload) ||
    !isCanonicalPlayerTag(payload.tag) ||
    !isString(payload.name) ||
    !isInteger(payload.trophies) ||
    !isRecord(payload.screen_ready)
  )
    throw new PythonApiError(502, { error: "malformed" });
  const screen = payload.screen_ready;
  const mapDay = (value: unknown) => {
    if (
      !isRecord(value) ||
      !isString(value.ranked_day_start) ||
      !isNullableString(value.ranked_day_end) ||
      !isOneOf(value.state, ["Live", "Complete", "Partial", "Uncertain"] as const) ||
      !isRecord(value.completeness) ||
      !isOneOf(value.completeness.state, ["complete", "partial", "uncertain"] as const) ||
      !isString(value.completeness.reason) ||
      !isOneOf(value.public_confidence, ["high", "partial", "uncertain"] as const) ||
      !Array.isArray(value.uncertainty_reasons) ||
      !value.uncertainty_reasons.every(isString) ||
      !(value.season_day_number === null || isInteger(value.season_day_number))
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
    isString(value.observed_at) &&
    isOneOf(value.freshness, ["fresh", "stale", "unknown"] as const) &&
    isOneOf(value.confidence, ["high", "partial", "uncertain"] as const) &&
    isOneOf(value.coverage, ["complete", "partial", "missing", "unknown"] as const) &&
    isString(value.version)
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

function mapSnakeProvenanceRequired(value: unknown) {
  if (!isSnakeProvenance(value)) throw new PythonApiError(502, { error: "malformed" });
  return mapSnakeProvenance(value);
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

function isValidResponsePayload(value: Record<string, unknown>): boolean {
  switch (value.kind) {
    case "tracked-leaderboard":
      return isTrackedLeaderboard(value);
    case "player-search":
      return isSearchResponse(value);
    case "player-page":
      return isPlayerPage(value);
    case "refresh-work":
    case "refresh-status":
      return isRefreshPayload(value);
    default:
      return false;
  }
}
