import { parseCalendar } from "../lib/calendar";
import {
  mapTrophyTrends,
  mapDashboardWindows,
  mapDashboardGoal,
  mapDashboardSeason,
  mapRivalSeason,
  mapShape,
  mapPresentation,
  mapDisplayEnd,
  mapDisplayStart,
  mapBattleStatistics,
  integer,
  nullableTimestamp,
  nullableNumber,
  record,
} from "./player-facts.server";
import type { ArmyUnitNames } from "../lib/army";
import type {
  EodProofState,
  HistoricalSeasonSummary,
  PlayerPage,
  RankedBattleEvent,
  RankedDaySummary,
  SeasonWeek,
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
  isSnakeProvenance,
  isString,
  isUtcTimestamp,
} from "./python-response.server";

const EOD_STATES = ["accepted", "provisional"] as const;

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
      value.days_observed > integer(value.day_count) ||
      value.days_missing < 0 ||
      value.days_missing > integer(value.day_count) ||
      (value.source === "official_league_history" &&
        (officialHistory === null ||
          value.coverage_state !== "partial" ||
          value.days_observed !== 0 ||
          value.days_missing !== integer(value.day_count)))
    )
      malformed();
    return {
      seasonId: value.official_season_id,
      seasonStart: nullableTimestamp(value.season_start),
      seasonEnd: nullableTimestamp(value.season_end),
      lastDayStart: nullableTimestamp(value.last_day_start),
      dayCount: integer(value.day_count),
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
    payload.daily_entries.length > integer(payload.day_count) ||
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
      payload.days_missing !== integer(payload.day_count) ||
      payload.missing_days.length !== integer(payload.day_count) ||
      !payload.missing_days.every((day, index) => day === index + 1) ||
      payload.daily_entries.length !== 0 ||
      payload.published_at !== null ||
      payload.season_start === null ||
      payload.season_end === null ||
      !isUtcTimestamp(payload.season_start) ||
      !isUtcTimestamp(payload.season_end) ||
      payload.end_trophies !== officialHistory.eodTrophies ||
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
      !isUtcTimestamp(value.ranked_day_start) ||
      !(value.ranked_day_end === null || isString(value.ranked_day_end)) ||
      !isString(value.state) ||
      !isString(value.coverage) ||
      typeof value.has_adjustment !== "boolean" ||
      !Array.isArray(value.flags) ||
      !value.flags.every(isString) ||
      ![value.eod_state, value.eod_change_state].every(
        (item) => (item ?? null) === null || isOneOf(item, EOD_STATES),
      )
    )
      malformed();
    const metrics = [
      value.start_trophies,
      value.end_trophies,
      value.eod_change ?? null,
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
      start: value.ranked_day_start,
      end: value.ranked_day_end as string | null,
      seasonId: payload.official_season_id as string,
      presentation: mapPresentation(value.presentation),
      startTrophies: value.start_trophies as number | null,
      endTrophies: value.end_trophies as number | null,
      eodState: (value.eod_state ?? null) as EodProofState,
      eodChange: (value.eod_change ?? null) as number | null,
      eodChangeState: (value.eod_change_state ?? null) as EodProofState,
      attackGain: value.attack_gain as number | null,
      defenseLoss: value.defense_loss as number | null,
      netChange: value.net_change as number | null,
      attacks: value.attack_count as number | null,
      defenses: value.defense_count as number | null,
      state: value.state as string,
      confidence: isOneOf(value.confidence, [
        "exact",
        "inferred",
        "partial",
        "uncertain",
      ] as const)
        ? value.confidence
        : null,
      coverage: value.coverage as string,
      hasAdjustment: value.has_adjustment as boolean,
      adjustmentTotal: value.adjustment_total as number | null,
      flags: value.flags as string[],
      resetRank: mapResetRank(value.reset_rank),
    };
  });
  return {
    kind: "player-season-summary",
    summaryStatistics: Object.fromEntries(
      ["attack", "defense"].map((side) => {
        const value = record(record(payload.summary_statistics)[side]);
        return [
          side,
          {
            perDay: nullableNumber(value.per_day),
            perBattle: nullableNumber(value.per_battle),
          },
        ];
      }),
    ) as HistoricalSeasonSummary["summaryStatistics"],
    dayCount: integer(payload.day_count),
    lastDayStart: nullableTimestamp(payload.last_day_start),
    seasonShape: mapShape(payload.season_shape, dailyEntries),
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
    seasonWeeks: mapSeasonWeeks(payload.season_weeks),
  };
}

export function mapPlayerPage(
  payload: unknown,
  battles = true,
  dashboard = false,
): PlayerPage {
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
      opponent: { tag: value.opponent.tag, name: value.opponent.name || null },
      destructionPercentage: value.destruction_percentage,
      stars: value.stars,
      trophyChange: value.trophy_change,
      perspectiveDisagreement: value.perspective_disagreement === true,
      armyShareCode: isString(value.army_share_code) ? value.army_share_code : null,
    };
  };
  const mapDay = (value: unknown, reference?: "season" | "recent" | "current") => {
    if (
      !isRecord(value) ||
      !isUtcTimestamp(value.ranked_day_start) ||
      !isNullableString(value.ranked_day_end) ||
      !isOneOf(value.state, ["Live", "Complete", "Partial"] as const) ||
      !isRecord(value.completeness) ||
      !isOneOf(value.completeness.state, ["complete", "partial", "uncertain"] as const) ||
      !isString(value.completeness.reason) ||
      !isOneOf(value.public_confidence, ["high", "partial", "uncertain"] as const) ||
      !(value.season_day_number === null || isInteger(value.season_day_number)) ||
      !(
        value.start_trophies == null ||
        (isInteger(value.start_trophies) && value.start_trophies >= 0)
      ) ||
      !Array.isArray(value.offense_events) ||
      !Array.isArray(value.defense_events) ||
      !(
        value.adjustments === undefined ||
        (Array.isArray(value.adjustments) &&
          value.adjustments.every(
            (item) => isRecord(item) && isString(item.type) && isInteger(item.amount),
          ))
      )
    )
      malformed();
    const adjustments = (Array.isArray(value.adjustments) ? value.adjustments : []).map(
      (item) => {
        if (!isRecord(item) || !isString(item.type) || !isInteger(item.amount))
          malformed();
        return { type: item.type, amount: item.amount };
      },
    );
    const automatic = adjustments.find((item) => item.type === "automatic_defense");
    const reset = adjustments.find(
      (item) => item.type === "weekly_reset" || item.type === "season_reset",
    );
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
    const display = battles
      ? mapDisplayStart(
          reference &&
            isRecord(value.display_start_by_reference) &&
            value.display_start_by_reference[reference] !== null
            ? value.display_start_by_reference[reference]
            : value.display_start,
        )
      : null;
    if (!(value.official_season_id === null || isString(value.official_season_id)))
      malformed();
    return {
      dayNumber: value.season_day_number as number | null,
      start: value.ranked_day_start,
      end: value.ranked_day_end,
      seasonId: value.official_season_id,
      presentation: battles ? mapPresentation(value.presentation) : null,
      displayEnd: battles ? mapDisplayEnd(value.display_end) : null,
      label: "Ranked day",
      state: value.state,
      startTrophies: display ? display.trophies : (value.start_trophies as number | null),
      startTrophiesCalculation: display?.calculation,
      startTrophiesSource:
        display?.source === "season_rule"
          ? ("Season rule" as const)
          : display?.source === "calculated"
            ? ("Calculated" as const)
            : undefined,
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
      automaticDefenseLoss: automatic ? -automatic.amount : null,
      resetAdjustment: reset
        ? {
            kind:
              reset.type === "weekly_reset" ? ("weekly" as const) : ("Season" as const),
            amount: reset.amount,
          }
        : null,
      resetRank: mapResetRank(value.reset_rank),
      battlesComplete: value.battles_complete === true,
      offenseEvents: value.offense_events.map((event) => mapEvent(event, "offense")),
      defenseEvents: value.defense_events.map((event) => mapEvent(event, "defense")),
      completeness: {
        state: value.completeness.state as "complete" | "partial" | "uncertain",
        reason: value.completeness.reason as string,
      },
    };
  };
  if (
    !Array.isArray(screen.days) ||
    !isNullableString(screen.current_day_start) ||
    !Array.isArray(screen.recent_day_starts) ||
    !Array.isArray(screen.season_day_starts) ||
    !isSnakeProvenance(screen.provenance)
  )
    malformed();
  const days = new Map(
    screen.days.map((value) => {
      if (!isRecord(value) || !isUtcTimestamp(value.ranked_day_start)) malformed();
      return [value.ranked_day_start, value] as const;
    }),
  );
  const primaryDays = new Map([...days].map(([start, value]) => [start, mapDay(value)]));
  const dayAt = (start: unknown, reference?: "season" | "recent" | "current") => {
    if (!isString(start) || !days.has(start)) malformed();
    const raw = days.get(start)!;
    if (reference && isRecord(raw.display_start_by_reference))
      return mapDay(raw, reference);
    return primaryDays.get(start)!;
  };
  const historyStarts = screen.history_day_starts;
  if (battles && !isRecord(screen.history_day_numbers)) malformed();
  if (battles && (!Array.isArray(historyStarts) || !historyStarts.every(isUtcTimestamp)))
    malformed();
  const seasonDays = screen.season_day_starts.map((start) => dayAt(start, "season"));
  const recentDays = screen.recent_day_starts.map((start) => dayAt(start, "recent"));
  const currentDay =
    screen.current_day_start === null ? null : dayAt(screen.current_day_start, "current");
  const calendar = parseCalendar(screen.calendar);
  if (!calendar) malformed();
  // Only the Dashboard asks for its Season and rival facts.
  const dashboardSeason =
    battles && dashboard
      ? mapDashboardSeason(screen.dashboard_season, calendar.day_count)
      : null;
  const rivalSeason = dashboard ? mapRivalSeason(screen.rival_season) : null;
  if (dashboardSeason && rivalSeason) {
    dashboardSeason.windows = mapDashboardWindows(
      screen.dashboard_windows,
      dashboardSeason,
    );
    dashboardSeason.averages = {
      offense: rivalSeason.offense,
      defense: rivalSeason.defense,
    };
  }
  const player: PlayerPage = {
    kind: "player-page",
    trophyTrends: battles ? mapTrophyTrends(screen.trophy_trends) : null,
    dashboardSeason,
    dashboardGoal:
      battles && screen.dashboard_goal !== undefined
        ? mapDashboardGoal(screen.dashboard_goal)
        : null,
    rivalSeason,
    calendar,
    seasonShape: battles
      ? mapShape(screen.season_shape, [
          ...seasonDays,
          ...(currentDay ? [currentDay] : []),
          ...recentDays,
        ])
      : null,
    historyDayNumbers: battles
      ? Object.fromEntries(
          (historyStarts as string[]).map((start) => [
            start,
            integer(record(screen.history_day_numbers)[start]),
          ]),
        )
      : {},
    historyDays: battles
      ? (historyStarts as string[]).map(
          (start) =>
            [...seasonDays, ...(currentDay ? [currentDay] : []), ...recentDays].find(
              (day) => day.start === start,
            ) ?? dayAt(start),
        )
      : [],
    battleStatistics: battles ? mapBattleStatistics(screen.battle_statistics) : null,
    tag: payload.tag,
    trackingState:
      payload.active === true
        ? "tracking"
        : payload.eligibility === "ineligible"
          ? "not_in_legend"
          : "uncertain",
    profile: {
      tag: payload.tag,
      name: payload.name,
      // Python sends no clan name when the saved profile has no clan.
      clan: isString(payload.clan) ? payload.clan : "",
      clanTag: isString(payload.clan_tag) ? payload.clan_tag : null,
      trophies: payload.trophies,
      seasonResetPending: payload.season_reset_pending === true,
      currentLeagueSeasonId: isString(payload.current_league_season_id)
        ? payload.current_league_season_id
        : null,
      freshness: {
        state:
          payload.freshness === "fresh" || payload.freshness === "stale"
            ? payload.freshness
            : "unknown",
        observedAt: isString(payload.observed_at) ? payload.observed_at : "",
        ageSeconds: isFiniteNumber(payload.age_seconds) ? payload.age_seconds : 0,
      },
      battleHistoryUpdatedAt: isUtcTimestamp(payload.battle_history_updated_at)
        ? payload.battle_history_updated_at
        : null,
      notFoundAt: isUtcTimestamp(payload.profile_not_found_at)
        ? payload.profile_not_found_at
        : null,
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
    currentDay,
    currentDayPublishedAt: isUtcTimestamp(screen.current_day_published_at)
      ? screen.current_day_published_at
      : null,
    recentDays,
    seasonDays,
    armyUnits: mapArmyUnits(screen.army_units),
    dataQuality: mapDataQuality(screen.data_quality),
  };
  // Most days are in two or three lists. A day that came out the same in each
  // is one object, so the page data carries it once instead of once per list.
  const shared = new Map<string, RankedDaySummary>();
  const share = (day: RankedDaySummary) => {
    const key = JSON.stringify(day);
    if (!shared.has(key)) shared.set(key, day);
    return shared.get(key)!;
  };
  player.recentDays = player.recentDays.map(share);
  player.seasonDays = player.seasonDays.map(share);
  if (player.currentDay) player.currentDay = share(player.currentDay);
  return player;
}

// A unit with a malformed entry is left out; its portrait shows its id.
export function mapArmyUnits(value: unknown): ArmyUnitNames {
  if (!isRecord(value)) return {};
  return Object.fromEntries(
    Object.entries(value).flatMap(([id, unit]) =>
      isRecord(unit) && isString(unit.name)
        ? [[id, { name: unit.name, siege: unit.siege === true }]]
        : [],
    ),
  );
}

function mapSeason(value: unknown): PlayerPage["season"] {
  if (value === null) return null;
  if (
    !isRecord(value) ||
    !isString(value.id) ||
    !isUtcTimestamp(value.start) ||
    !isUtcTimestamp(value.end) ||
    !isInteger(value.current_day_number) ||
    value.current_day_number < 1 ||
    value.current_day_number > integer(value.day_count) ||
    !isOneOf(value.anchor_source, [
      "official_league_history",
      "daily_publication",
      "weekly_league_record",
    ] as const) ||
    !isUtcTimestamp(value.anchor_observed_at)
  )
    malformed();
  return {
    id: value.id,
    anchor: value.start,
    currentDayNumber: value.current_day_number,
    dayCount: integer(value.day_count),
    end: value.end,
    initialTrophies: integer(value.initial_trophies),
    anchorSource: value.anchor_source,
    anchorObservedAt: value.anchor_observed_at,
  };
}

// A week with a malformed entry is left out: the page then treats it as a
// week the record has not decided.
export function mapSeasonWeeks(value: unknown): SeasonWeek[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((week) =>
    isRecord(week) &&
    isUtcTimestamp(week.week_start) &&
    isOneOf(week.league, ["legend_i", "below_legend_i", "unknown"] as const) &&
    (week.entry === null ||
      isOneOf(week.entry, [
        "season_start",
        "stayed",
        "promoted",
        "first_seen",
        "unknown",
      ] as const))
      ? [{ start: week.week_start, league: week.league, entry: week.entry }]
      : [],
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

// A day's rank on its Reset board; absent or null means unknown.
function mapResetRank(value: unknown): number | null {
  if (value == null) return null;
  if (!isInteger(value) || value < 1) malformed();
  return value;
}

function malformed(): never {
  throw new PythonApiError(502, { error: "malformed" });
}
