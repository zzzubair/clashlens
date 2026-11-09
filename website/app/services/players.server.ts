import { mapPublicUserResults } from "../lib/account-contracts";
import type {
  HistoricalSeasonSummary,
  PlayerPage,
  RefreshStatus,
  RefreshWork,
  SearchResponse,
  SummarizedSeasonRef,
} from "../lib/contracts";
import { normalizePlayerTag } from "../lib/player-tag";
import { isCanonicalUuid, MAX_SEARCH_QUERY_LENGTH } from "../lib/validation";
import {
  mapHistoricalSeason,
  mapPlayerPage,
  mapPlayerSeasons,
} from "./player-response.server";
import {
  isCanonicalPlayerTag,
  isFiniteNumber,
  isInteger,
  isOneOf,
  isRecord,
  isString,
  PythonApiError,
} from "./python-response.server";
import { requestJson } from "./python-transport.server";

export async function searchPlayers(query: string, limit = 50): Promise<SearchResponse> {
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
    { method: "GET" },
  );
  return mapSearch(payload, query);
}

export async function getPlayer(
  tag: string,
  battles = true,
  goalRank?: number,
): Promise<PlayerPage> {
  const query = new URLSearchParams();
  if (!battles) query.set("battles", "false");
  if (goalRank !== undefined) query.set("goal_rank", String(goalRank));
  const payload = await requestJson<unknown>(
    `/v1/players/${encodeURIComponent(tag)}${query.size ? `?${query}` : ""}`,
    { method: "GET" },
  );
  if (
    goalRank !== undefined &&
    (!isRecord(payload) ||
      !isRecord(payload.screen_ready) ||
      !("dashboard_goal" in payload.screen_ready))
  )
    throw new PythonApiError(502, { error: "malformed" });
  return mapPlayerPage(payload, battles);
}

export async function getPlayerSeasons(tag: string): Promise<SummarizedSeasonRef[]> {
  const payload = await requestJson<unknown>(
    `/v1/players/${encodeURIComponent(tag)}/seasons`,
    { method: "GET" },
  );
  return mapPlayerSeasons(payload);
}

export async function getPlayerSeason(
  tag: string,
  seasonId: string,
): Promise<HistoricalSeasonSummary> {
  const payload = await requestJson<unknown>(
    `/v1/players/${encodeURIComponent(tag)}/seasons/${encodeURIComponent(seasonId)}`,
    { method: "GET" },
  );
  return mapHistoricalSeason(payload);
}

export async function requestRefresh(
  tag: string,
  idempotencyKey: string,
): Promise<RefreshWork> {
  if (!isCanonicalUuid(idempotencyKey)) {
    throw new PythonApiError(400, { error: "invalid_input" });
  }
  const payload = await requestJson<unknown>(
    `/v1/players/${encodeURIComponent(tag)}/refresh`,
    { method: "POST", requestId: idempotencyKey },
  );
  return mapRefresh(payload, "refresh-work");
}

export async function getRefreshStatus(
  workId: string,
  tag: string,
): Promise<RefreshStatus> {
  const payload = await requestJson<unknown>(
    `/v1/refreshes/${encodeURIComponent(workId)}`,
    { method: "GET" },
  );
  const status = mapRefresh(payload, "refresh-status");
  if (status.tag !== tag) {
    throw new PythonApiError(409, { error: "conflict" });
  }
  if (status.state === "complete") {
    return { ...status, player: await getPlayer(tag) };
  }
  return status;
}

function mapRefresh(payload: unknown, kind: "refresh-work"): RefreshWork;
function mapRefresh(payload: unknown, kind: "refresh-status"): RefreshStatus;
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
    !isOneOf(payload.display_state, [
      "queued",
      "running",
      "complete",
      "failed",
    ] as const) ||
    !isString(payload.outcome)
  ) {
    throw new PythonApiError(502, { error: "malformed" });
  }
  const state = payload.display_state;
  const value = {
    kind,
    workId: payload.refresh_id,
    tag: payload.tag,
    state,
    progressPercent: state === "queued" ? 0 : state === "running" ? 50 : 100,
    message: payload.outcome,
    publishedAt: null,
  } as const;
  return kind === "refresh-status"
    ? { ...value, kind: "refresh-status", player: null }
    : { ...value, kind: "refresh-work" };
}

function mapSearch(payload: unknown, submittedQuery: string): SearchResponse {
  if (
    !isRecord(payload) ||
    !isString(payload.query) ||
    !Array.isArray(payload.results) ||
    !Array.isArray(payload.users) ||
    typeof payload.known_only !== "boolean"
  )
    throw new PythonApiError(502, { error: "malformed" });
  const users = mapPublicUserResults(payload.users);
  if (users === null) throw new PythonApiError(502, { error: "malformed" });
  const results = payload.results.map((item) => {
    if (
      !isRecord(item) ||
      !isCanonicalPlayerTag(item.tag) ||
      !isString(item.name) ||
      !(isInteger(item.trophies) || item.trophies === null) ||
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
    users,
    knownOnly: payload.known_only,
  };
}
