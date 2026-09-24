import type {
  BattleArmy,
  HistoricalSeasonSummary,
  PlayerPage,
  RankedBattleEvent,
  RankedDaySummary,
  SummarizedSeasonRef,
} from "../lib/contracts";
import {
  PythonApiError,
  isCanonicalPlayerTag,
  isFiniteNumber,
  isInteger,
  isNullableString,
  isOneOf,
  isRecord,
  isString,
  isUtcTimestamp,
} from "./python-response.server";

export function mapPlayerSeasons(payload: unknown): SummarizedSeasonRef[] {
  if (!isRecord(payload) || !Array.isArray(payload.seasons)) malformed();
  return payload.seasons.map((value) => {
    if (
      !isRecord(value) ||
      !isString(value.official_season_id) ||
      value.official_season_id.length === 0 ||
      !isOneOf(value.coverage_state, ["complete", "partial"] as const) ||
      !isInteger(value.days_observed) ||
      !isInteger(value.days_missing) ||
      !isOneOf(value.source, ["tracked_summary", "official_league_history"] as const)
    )
      malformed();
    const officialHistory = mapOfficialSeasonHistory(value.official_history);
    if (
      value.days_observed < 0 ||
      value.days_observed > 28 ||
      value.days_missing < 0 ||
      value.days_missing > 28 ||
      (value.source === "official_league_history" &&
        (!isCanonicalLegendSeasonId(value.official_season_id) ||
          officialHistory === null ||
          value.coverage_state !== "partial" ||
          value.days_observed !== 0 ||
          value.days_missing !== 28))
    )
      malformed();
    return {
      seasonId: value.official_season_id,
      coverageState: value.coverage_state,
      daysObserved: value.days_observed,
      daysMissing: value.days_missing,
      source: value.source,
      officialHistory,
    };
  });
}

function mapOfficialSeasonHistory(value: unknown) {
  if (value === null) return null;
  if (
    !isRecord(value) ||
    value.source !== "official_league_history" ||
    !isUtcTimestamp(value.observed_at) ||
    !(
      value.eod_trophies === null ||
      (isInteger(value.eod_trophies) && value.eod_trophies >= 0)
    ) ||
    !(
      value.final_placement === null ||
      (isInteger(value.final_placement) && value.final_placement >= 1)
    )
  )
    malformed();
  return {
    observedAt: value.observed_at,
    eodTrophies: value.eod_trophies as number | null,
    finalPlacement: value.final_placement as number | null,
  };
}

export function mapHistoricalSeason(payload: unknown): HistoricalSeasonSummary {
  if (
    !isRecord(payload) ||
    payload.kind !== "player-season-summary" ||
    !isCanonicalPlayerTag(payload.tag) ||
    !isString(payload.official_season_id) ||
    payload.official_season_id.length === 0 ||
    !(payload.season_start === null || isString(payload.season_start)) ||
    !(payload.season_end === null || isString(payload.season_end)) ||
    !isOneOf(payload.coverage_state, ["complete", "partial"] as const) ||
    !Array.isArray(payload.daily_entries) ||
    payload.daily_entries.length > 28 ||
    !Array.isArray(payload.unresolved_flags) ||
    !payload.unresolved_flags.every(isString) ||
    !Array.isArray(payload.missing_days) ||
    !payload.missing_days.every(isInteger) ||
    !isInteger(payload.days_observed) ||
    !isInteger(payload.days_missing) ||
    !isRecord(payload.attack_stars) ||
    !isRecord(payload.defense_stars) ||
    !isOneOf(payload.source, ["tracked_summary", "official_league_history"] as const)
  )
    malformed();
  const officialOnly = payload.source === "official_league_history";
  const validStar = (value: unknown) =>
    isInteger(value) || (officialOnly && value === null);
  if (
    !validStar(payload.attack_stars_unknown) ||
    !validStar(payload.defense_stars_unknown) ||
    ![payload.attack_stars, payload.defense_stars].every((stars) =>
      ["0", "1", "2", "3"].every((key) => validStar(stars[key])),
    )
  )
    malformed();
  const counts = [
    payload.attack_count,
    payload.attack_gain,
    payload.defense_count,
    payload.defense_loss,
    payload.net_trophy_change,
    payload.start_trophies,
    payload.end_trophies,
  ];
  if (!counts.every((item) => item === null || isInteger(item))) malformed();
  if (
    !(payload.final_rank === null || isInteger(payload.final_rank)) ||
    !(payload.published_at === null || isString(payload.published_at))
  )
    malformed();
  const officialHistory = mapOfficialSeasonHistory(payload.official_history);
  if (
    officialOnly &&
    (officialHistory === null ||
      payload.coverage_state !== "partial" ||
      payload.days_observed !== 0 ||
      payload.days_missing !== 28 ||
      payload.missing_days.length !== 28 ||
      !payload.missing_days.every((day, index) => day === index + 1) ||
      payload.daily_entries.length !== 0 ||
      payload.published_at !== null ||
      payload.season_start === null ||
      payload.season_end === null ||
      !isUtcTimestamp(payload.season_start) ||
      !isUtcTimestamp(payload.season_end) ||
      !isSeasonDuration(payload.season_start, payload.season_end) ||
      !isCanonicalSeasonId(payload.official_season_id, payload.season_start) ||
      payload.end_trophies !== officialHistory.eodTrophies ||
      payload.final_rank !== officialHistory.finalPlacement ||
      payload.attack_stars_unknown !== null ||
      payload.defense_stars_unknown !== null ||
      ![payload.attack_stars, payload.defense_stars].every((stars) =>
        ["0", "1", "2", "3"].every((key) => stars[key] === null),
      ) ||
      ![
        payload.attack_count,
        payload.attack_gain,
        payload.defense_count,
        payload.defense_loss,
        payload.net_trophy_change,
        payload.start_trophies,
      ].every((value) => value === null))
  )
    malformed();
  const dailyEntries = payload.daily_entries.map((value) => {
    if (
      !isRecord(value) ||
      !(value.season_day_number === null || isInteger(value.season_day_number)) ||
      !isString(value.ranked_day_start) ||
      !(value.ranked_day_end === null || isString(value.ranked_day_end)) ||
      !isString(value.state) ||
      !isString(value.coverage) ||
      typeof value.has_adjustment !== "boolean" ||
      !Array.isArray(value.flags) ||
      !value.flags.every(isString)
    )
      malformed();
    const metrics = [
      value.start_trophies,
      value.end_trophies,
      value.attack_gain,
      value.defense_loss,
      value.net_change,
      value.attack_count,
      value.defense_count,
      value.adjustment_total,
    ];
    if (!metrics.every((item) => item === null || isInteger(item))) malformed();
    if ("battle_id" in value || "battles" in value || "opponent" in value) malformed();
    return {
      dayNumber: value.season_day_number as number | null,
      period: isString(value.ranked_day_end)
        ? `${value.ranked_day_start} – ${value.ranked_day_end}`
        : (value.ranked_day_start as string),
      startTrophies: value.start_trophies as number | null,
      endTrophies: value.end_trophies as number | null,
      attackGain: value.attack_gain as number | null,
      defenseLoss: value.defense_loss as number | null,
      netChange: value.net_change as number | null,
      attacks: value.attack_count as number | null,
      defenses: value.defense_count as number | null,
      state: value.state as string,
      coverage: value.coverage as string,
      hasAdjustment: value.has_adjustment as boolean,
      adjustmentTotal: value.adjustment_total as number | null,
      flags: value.flags as string[],
    };
  });
  return {
    kind: "player-season-summary",
    tag: payload.tag,
    seasonId: payload.official_season_id,
    seasonStart: payload.season_start as string | null,
    seasonEnd: payload.season_end as string | null,
    startTrophies: payload.start_trophies as number | null,
    endTrophies: payload.end_trophies as number | null,
    finalRank: payload.final_rank as number | null,
    attackCount: payload.attack_count as number | null,
    attackGain: payload.attack_gain as number | null,
    defenseCount: payload.defense_count as number | null,
    defenseLoss: payload.defense_loss as number | null,
    netTrophyChange: payload.net_trophy_change as number | null,
    attackStars: payload.attack_stars as Record<string, number | null>,
    defenseStars: payload.defense_stars as Record<string, number | null>,
    attackStarsUnknown: payload.attack_stars_unknown as number | null,
    defenseStarsUnknown: payload.defense_stars_unknown as number | null,
    daysObserved: payload.days_observed,
    daysMissing: payload.missing_days as number[],
    coverageState: payload.coverage_state,
    unresolvedFlags: payload.unresolved_flags as string[],
    dailyEntries,
    publishedAt: payload.published_at as string | null,
    source: payload.source,
    officialHistory,
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
    malformed();
  const components = value.components.map((item) => {
    if (
      !isRecord(item) ||
      !isString(item.typed_id) ||
      !isString(item.name) ||
      !isInteger(item.quantity) ||
      item.quantity < 1 ||
      !isString(item.origin)
    )
      malformed();
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
      malformed();
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

export function mapPlayerPage(payload: unknown): PlayerPage {
  if (
    !isRecord(payload) ||
    !isCanonicalPlayerTag(payload.tag) ||
    !isString(payload.name) ||
    !isInteger(payload.trophies) ||
    !isRecord(payload.screen_ready)
  )
    malformed();
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
      malformed();
    return {
      battleId: value.battle_id,
      battleTimestamp: value.battle_timestamp,
      opponent: { tag: value.opponent.tag, name: value.opponent.name },
      destructionPercentage: value.destruction_percentage,
      stars: value.stars,
      trophyChange: value.trophy_change,
      perspectiveDisagreement: value.perspective_disagreement === true,
      army: mapBattleArmy(value.army ?? null),
      armyShareCode: isString(value.army_share_code) ? value.army_share_code : null,
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
      !(
        value.start_trophies == null ||
        (isInteger(value.start_trophies) && value.start_trophies >= 0)
      ) ||
      !Array.isArray(value.offense_events) ||
      value.offense_events.length > 8 ||
      !Array.isArray(value.defense_events) ||
      value.defense_events.length > 8
    )
      malformed();
    const valid = [
      value.attack_count,
      value.attack_three_star_count,
      value.attack_gain,
      value.defense_count,
      value.defense_three_star_count,
      value.defense_loss,
      value.net_trophy_change,
    ].every((item) => item === null || isInteger(item));
    if (!valid) malformed();
    return {
      dayNumber: value.season_day_number as number | null,
      label: "Ranked day",
      period: isString(value.ranked_day_end)
        ? `${value.ranked_day_start} – ${value.ranked_day_end}`
        : value.ranked_day_start,
      state: value.state,
      startTrophies: (value.start_trophies as number | null | undefined) ?? null,
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
  if (!Array.isArray(screen.recent_days) || !Array.isArray(screen.season_days))
    malformed();
  const player: PlayerPage = {
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
  calculateStartingTrophies(player.seasonDays, player.profile);
  calculateStartingTrophies(player.recentDays, player.profile);
  if (player.currentDay) calculateStartingTrophies([player.currentDay], player.profile);
  return player;
}

function calculateStartingTrophies(
  days: RankedDaySummary[],
  profile: PlayerPage["profile"],
) {
  const observedAt = Date.parse(profile.freshness.observedAt);
  const bounds = (day: RankedDaySummary) => day.period.split(" – ").map(Date.parse);
  const ordered = [...days].sort((a, b) => bounds(b)[0] - bounds(a)[0]);
  let nextDay: RankedDaySummary | undefined;
  for (const day of ordered) {
    const [start, end] = bounds(day);
    const next = nextDay;
    nextDay = day;
    if (day.startTrophies != null || !Number.isFinite(start) || !Number.isFinite(end))
      continue;
    const events = [...day.offenseEvents, ...day.defenseEvents];
    // Only subtract the displayed change when every included battle agrees
    // with the totals. A profile from another day is never a daily end total.
    if (
      day.offense.trophyGain === null ||
      day.defense.trophyLoss === null ||
      day.offense.attacks !== day.offenseEvents.length ||
      day.defense.defenses !== day.defenseEvents.length ||
      day.offenseEvents.reduce((sum, event) => sum + event.trophyChange, 0) !==
        day.offense.trophyGain ||
      -day.defenseEvents.reduce((sum, event) => sum + event.trophyChange, 0) !==
        day.defense.trophyLoss ||
      events.some(
        (event) =>
          event.perspectiveDisagreement ||
          Date.parse(event.battleTimestamp) < start ||
          Date.parse(event.battleTimestamp) >= end,
      )
    )
      continue;
    const netChange = day.trophyChange ?? day.offense.trophyGain - day.defense.trophyLoss;
    let trophies: number | undefined;
    if (
      observedAt >= start &&
      observedAt < end &&
      (day.completeness.state === "complete" ||
        (day.offense.attacks === 8 && day.defense.defenses === 8)) &&
      events.every((event) => Date.parse(event.battleTimestamp) <= observedAt)
    ) {
      trophies = profile.trophies;
    } else if (
      next?.startTrophies != null &&
      bounds(next)[0] === end &&
      day.dayNumber !== null &&
      next.dayNumber === day.dayNumber + 1 &&
      (day.completeness.state === "complete" ||
        (day.offense.attacks === 8 && day.defense.defenses === 8))
    ) {
      // Work backwards only across adjacent days in the same season. Eight
      // attacks and defenses avoid guessing unobserved reset deductions.
      trophies = next.startTrophies;
    }
    if (
      trophies === undefined ||
      !Number.isSafeInteger(trophies - netChange) ||
      trophies - netChange < 0
    )
      continue;
    day.startTrophies = trophies - netChange;
    day.startTrophiesCalculation = { trophies, netChange };
  }
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

function mapSnakeProvenanceRequired(value: unknown) {
  if (!isSnakeProvenance(value)) malformed();
  return {
    source: value.source as string,
    observedAt: value.observed_at as string,
    freshness: value.freshness as "fresh" | "stale" | "unknown",
    confidence: value.confidence as "high" | "partial" | "uncertain",
    coverage: value.coverage as "complete" | "partial" | "missing" | "unknown",
    version: value.version as string,
  };
}

function mapSeason(value: unknown): PlayerPage["season"] {
  if (value === null) return null;
  if (
    !isRecord(value) ||
    !isString(value.id) ||
    !isUtcTimestamp(value.start) ||
    !isUtcTimestamp(value.end) ||
    !isSeasonDuration(value.start, value.end) ||
    !isInteger(value.current_day_number) ||
    value.current_day_number < 1 ||
    value.current_day_number > 28 ||
    !isOneOf(value.anchor_source, [
      "official_league_history",
      "daily_publication",
    ] as const) ||
    !isUtcTimestamp(value.anchor_observed_at) ||
    (value.anchor_source === "official_league_history" &&
      !isCanonicalSeasonId(value.id, value.start))
  )
    malformed();
  return {
    id: value.id,
    anchor: value.start,
    currentDayNumber: value.current_day_number,
    dayCount: 28,
    anchorSource: value.anchor_source,
    anchorObservedAt: value.anchor_observed_at,
  };
}

function isSeasonDuration(start: string, end: string): boolean {
  return Date.parse(end) - Date.parse(start) === 28 * 24 * 60 * 60 * 1_000;
}

function isCanonicalSeasonId(seasonId: string, start: string): boolean {
  return (
    isCanonicalLegendSeasonId(seasonId) && Number(seasonId) === Date.parse(start) / 1_000
  );
}

function isCanonicalLegendSeasonId(seasonId: string): boolean {
  if (!/^\d+$/.test(seasonId)) return false;
  const seconds = Number(seasonId);
  if (!Number.isSafeInteger(seconds)) return false;
  const start = new Date(seconds * 1_000);
  return (
    start.getUTCDay() === 1 &&
    start.getUTCHours() === 5 &&
    start.getUTCMinutes() === 0 &&
    start.getUTCSeconds() === 0 &&
    (seconds - 1_783_918_800) % (28 * 24 * 60 * 60) === 0
  );
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
    malformed();
  return value as PlayerPage["dataQuality"];
}

function malformed(): never {
  throw new PythonApiError(502, { error: "malformed" });
}
