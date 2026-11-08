import type { TrackedLeaderboard } from "../lib/contracts";
import {
  PythonApiError,
  isCanonicalPlayerTag,
  isFiniteNumber,
  isInteger,
  isNullableString,
  isOneOf,
  isRecord,
  isResetTimestamp,
  isSnakeProvenance,
  isString,
  isUtcTimestamp,
  mapRecentDays,
} from "./python-response.server";
import { requestJson } from "./python-transport.server";
const FOCUS_NEIGHBORS = 5;

export async function getTrackedLeaderboard(
  limit = 25,
  view: "live" | "daily" = "live",
  offset = 0,
  selector?: { officialSeasonId: string; dayNumber: number },
  focusTag?: string,
): Promise<TrackedLeaderboard> {
  const query = new URLSearchParams({
    limit: String(limit),
    offset: String(offset),
  });
  if (focusTag) query.set("focus_tag", focusTag);
  if (selector) {
    query.set("official_season_id", selector.officialSeasonId);
    query.set("season_day_number", String(selector.dayNumber));
  }
  const payload = await requestJson<unknown>(
    `/v1/leaderboards/${view === "live" ? "live" : "frozen"}?${query.toString()}`,
    { method: "GET" },
  );
  return mapLeaderboard(payload, view, focusTag);
}

function mapLeaderboard(
  payload: unknown,
  view: "live" | "daily",
  focusTag?: string,
): TrackedLeaderboard {
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
  const pageFirstPosition = (payload.page - 1) * payload.page_size + 1;
  const pageEntries = Math.max(
    0,
    Math.min(payload.page_size, payload.total_entries - pageFirstPosition + 1),
  );
  const firstEntry: unknown = payload.entries[0];
  const firstPosition =
    focusTag && isRecord(firstEntry) && isInteger(firstEntry.position)
      ? firstEntry.position
      : pageFirstPosition;
  const neighborsBefore = pageFirstPosition - firstPosition;
  const neighborsAfter = payload.entries.length - pageEntries - neighborsBefore;
  if (
    payload.tracked_population < 0 ||
    payload.total_entries < 0 ||
    payload.page < 1 ||
    payload.page_size < 1 ||
    payload.page_count !== expectedPageCount ||
    payload.page > Math.max(1, payload.page_count) ||
    payload.has_previous !== payload.page > 1 ||
    payload.has_next !== payload.page < payload.page_count ||
    neighborsBefore < 0 ||
    neighborsAfter < 0 ||
    Math.max(neighborsBefore, neighborsAfter) > FOCUS_NEIGHBORS ||
    firstPosition + payload.entries.length - 1 > payload.total_entries ||
    (focusTag
      ? !payload.entries.some((entry) => isRecord(entry) && entry.tag === focusTag)
      : neighborsBefore + neighborsAfter > 0)
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
      !(entry.official_rank === null || isInteger(entry.official_rank)) ||
      !(entry.tied === undefined || typeof entry.tied === "boolean") ||
      !(entry.reset_position == null || isInteger(entry.reset_position)) ||
      !(entry.today_change == null || isInteger(entry.today_change)) ||
      !(entry.recent_days === undefined || mapRecentDays(entry.recent_days))
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
      tied: entry.tied === true,
      // Missing from an older API, or on a Daily board.
      resetRank: isInteger(entry.reset_position) ? entry.reset_position : null,
      todayChange: isInteger(entry.today_change) ? entry.today_change : null,
      recentDays: mapRecentDays(entry.recent_days) ?? [],
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
    seasonResetPending: Number(payload.season_reset_pending) || 0,
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
