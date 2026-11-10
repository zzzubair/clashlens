import { parseCalendar } from "../lib/calendar";
import { mapSeasonBounds } from "./player-facts.server";
import type { ArmyAnalytics } from "../lib/contracts";
import {
  PythonApiError,
  isFiniteNumber,
  isInteger,
  isOneOf,
  isRecord,
  isString,
} from "./python-response.server";
import { requestJson } from "./python-transport.server";

export async function getArmyAnalytics(query: URLSearchParams): Promise<ArmyAnalytics> {
  let payload: unknown;
  try {
    payload = await requestJson<unknown>(`/v1/analytics/armies?${query.toString()}`, {
      method: "GET",
    });
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
        parseCalendar(cause.payload.calendar) ??
          (() => {
            throw new PythonApiError(502, { error: "malformed" });
          })(),
      );
    }
    throw cause;
  }
  return mapArmyAnalytics(payload, true);
}

export async function getArmySeasonSummary(
  season: string,
  query: URLSearchParams,
): Promise<ArmyAnalytics> {
  // Historical army reads cover the whole season only: the query carries
  // lens/category/sort, never a day range or population filter.
  const payload = await requestJson<unknown>(
    `/v1/analytics/armies/seasons/${encodeURIComponent(season)}?${query.toString()}`,
    { method: "GET" },
  );
  return mapArmyAnalytics(payload, false);
}

// Finished seasons with saved attack summaries, newest first.
export interface ArmySeason {
  id: string;
  start: string | null;
  end: string | null;
}

export async function getArmySeasons(): Promise<ArmySeason[]> {
  const payload = await requestJson<unknown>("/v1/analytics/armies/seasons", {
    method: "GET",
  });
  if (
    !isRecord(payload) ||
    !Array.isArray(payload.seasons) ||
    !payload.seasons.every(isString)
  ) {
    throw new PythonApiError(502, { error: "malformed" });
  }
  if (!Array.isArray(payload.season_details))
    throw new PythonApiError(502, { error: "malformed" });
  const details = payload.season_details.map(mapSeasonBounds);
  if (details.some((detail) => detail === null))
    throw new PythonApiError(502, { error: "malformed" });
  return payload.seasons.map(
    (id) => details.find((detail) => detail?.id === id) ?? { id, start: null, end: null },
  );
}

export class NoCompletedLegendDaysError extends Error {
  readonly previousSeasonId: string | null;

  constructor(
    previousSeasonId: string | null,
    readonly calendar: import("../lib/contracts").Calendar,
  ) {
    super("no completed Legend days this season");
    this.name = "NoCompletedLegendDaysError";
    this.previousSeasonId = previousSeasonId;
  }
}

function mapArmyAnalytics(payload: unknown, requireCalendar: boolean): ArmyAnalytics {
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
    !isInteger(payload.cohort_evidence.cohort_players) ||
    !isInteger(payload.cohort_evidence.stale_or_uncertain_cohort_members) ||
    !isInteger(payload.cohort_evidence.streak_excluded_players) ||
    !isInteger(payload.cohort_evidence.shielded_player_days) ||
    !isRecord(payload.collection_coverage) ||
    !isString(payload.collection_coverage.state) ||
    !isInteger(payload.collection_coverage.completed_days) ||
    (payload.collection_coverage.covered_days !== undefined &&
      (!Array.isArray(payload.collection_coverage.covered_days) ||
        !payload.collection_coverage.covered_days.every(isInteger))) ||
    (payload.collection_coverage.streak_gap_days !== undefined &&
      (!Array.isArray(payload.collection_coverage.streak_gap_days) ||
        !payload.collection_coverage.streak_gap_days.every(isInteger))) ||
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
  const usageOnly = payload.history_usage_only === true;
  const parseRow = (row: unknown): ArmyAnalytics["rows"][number] => {
    if (!isRecord(row)) throw new PythonApiError(502, { error: "malformed" });
    if (
      !isString(row.key) ||
      !isString(row.label) ||
      !isInteger(row.usage_count) ||
      !isInteger(row.usage_denominator) ||
      !isFiniteNumber(row.usage_rate) ||
      (usageOnly
        ? !isInteger(row.quantity) ||
          !isInteger(row.one_star_count) ||
          !isInteger(row.two_star_count) ||
          !isInteger(row.three_star_count)
        : !Array.isArray(row.star_counts) ||
          row.star_counts.length !== 4 ||
          !row.star_counts.every(isInteger) ||
          !Array.isArray(row.star_rates) ||
          row.star_rates.length !== 4 ||
          !row.star_rates.every(isFiniteNumber) ||
          !isFiniteNumber(row.three_star_rate) ||
          !isFiniteNumber(row.average_stars) ||
          !isFiniteNumber(row.average_destruction) ||
          (row.average_trophies !== undefined && !isFiniteNumber(row.average_trophies)) ||
          (row.average_miss_trophies != null &&
            !isFiniteNumber(row.average_miss_trophies)) ||
          !isInteger(row.unknown_excluded_attacks)) ||
      (row.variants !== undefined && !Array.isArray(row.variants))
    )
      throw new PythonApiError(502, { error: "malformed" });
    return {
      key: row.key,
      label: row.label,
      usageCount: row.usage_count,
      usageDenominator: row.usage_denominator,
      usageRate: row.usage_rate,
      quantity: usageOnly ? (row.quantity as number) : undefined,
      oneStarCount: usageOnly ? (row.one_star_count as number) : undefined,
      twoStarCount: usageOnly ? (row.two_star_count as number) : undefined,
      threeStarCount: usageOnly ? (row.three_star_count as number) : undefined,
      starCounts: row.star_counts as [number, number, number, number] | undefined,
      starRates: row.star_rates as [number, number, number, number] | undefined,
      threeStarRate: row.three_star_rate as number | undefined,
      averageStars: row.average_stars as number | undefined,
      averageDestruction: row.average_destruction as number | undefined,
      averageTrophies: row.average_trophies as number | undefined,
      averageMissTrophies: row.average_miss_trophies as number | null | undefined,
      unknownExcludedAttacks: row.unknown_excluded_attacks as number | undefined,
      variants: (row.variants as unknown[] | undefined)?.map(parseRow),
      typeId: isInteger(row.type_id) ? row.type_id : undefined,
      variantId: isInteger(row.variant_id) ? row.variant_id : undefined,
    };
  };
  const rows = payload.rows.map(parseRow);
  const armyStates = Object.fromEntries(
    Object.entries(payload.army_states).map(([state, count]) => {
      if (!isInteger(count)) throw new PythonApiError(502, { error: "malformed" });
      return [state, count];
    }),
  );
  const pagination = payload.pagination;
  const calendar = parseCalendar(payload.calendar);
  if (requireCalendar && calendar === null)
    throw new PythonApiError(502, { error: "malformed" });
  if (
    pagination !== undefined &&
    (!isRecord(pagination) ||
      !isInteger(pagination.offset) ||
      !isInteger(pagination.total_rows) ||
      !(pagination.next_offset === null || isInteger(pagination.next_offset)))
  )
    throw new PythonApiError(502, { error: "malformed" });
  return {
    pagination: isRecord(pagination)
      ? {
          offset: pagination.offset as number,
          totalRows: pagination.total_rows as number,
          nextOffset: pagination.next_offset as number | null,
        }
      : undefined,
    kind: "army-analytics",
    calendar,
    seasonBounds: mapSeasonBounds(payload.season_bounds),
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
      cohortPlayers: payload.cohort_evidence.cohort_players,
      staleOrUncertainCohortMembers:
        payload.cohort_evidence.stale_or_uncertain_cohort_members,
      streakExcludedPlayers: payload.cohort_evidence.streak_excluded_players,
      shieldedPlayerDays: payload.cohort_evidence.shielded_player_days,
    },
    collectionCoverage: {
      state: payload.collection_coverage.state,
      completedDays: payload.collection_coverage.completed_days,
      coveredDays: payload.collection_coverage.covered_days as number[] | undefined,
      streakGapDays: payload.collection_coverage.streak_gap_days as number[] | undefined,
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
