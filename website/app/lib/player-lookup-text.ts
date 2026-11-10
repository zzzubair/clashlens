import type {
  DayPresentation,
  DayStatus,
  PlayerLookup,
  PlayerPage,
  RankedDaySummary,
  RefreshStatus,
  RefreshWork,
  SummarizedSeasonRef,
} from "./contracts";

/** What a player page says about a tag without current results. */
export const LOOKUP_MESSAGES: Record<PlayerLookup["state"], string> = {
  unknown: "Waiting to check this tag with Clash of Clans.",
  checking:
    "Checking this tag with Clash of Clans. Legend I players start tracking automatically.",
  tracking: "Now tracking in Legend I. The first results are being prepared.",
  not_found: "Player not found. Clash of Clans has no player with this tag.",
  not_in_legend: "This player is not in Legend I.",
  uncertain:
    "Clash Lens tracks Legend League players only. This player exists, but we could not confirm they are in Legend I. Any saved history is still available.",
  failed:
    "We could not finish checking this tag. This does not mean the player is missing or outside Legend I.",
};

/**
 * The player page's lookup note. That page has a search box, so a tag the game
 * doesn't know also asks for a typo check; a player outside Legend I gets their league.
 */
export function lookupMessage(lookup: PlayerLookup): string {
  const message = LOOKUP_MESSAGES[lookup.state];
  if (lookup.state === "not_found")
    return `${message} Check it for typos and search again.`;
  const league = lookup.state === "not_in_legend" ? lookup.profile?.league : undefined;
  return league ? `${message} Current league: ${league}.` : message;
}

// The saved player, or a finished refresh's player when that is newer.
export function newestPlayer(
  status: RefreshStatus | RefreshWork | null,
  data: { requestedTag: string | null; player: PlayerPage | null },
): PlayerPage | null {
  const refreshed =
    status && "player" in status && status.tag === data.requestedTag
      ? status.player
      : null;
  return refreshed &&
    (data.player === null ||
      Date.parse(refreshed.profile.freshness.observedAt) >
        Date.parse(data.player.profile.freshness.observedAt))
    ? refreshed
    : data.player;
}

// A newest profile we cannot use is explained, even over saved results.
export function explainsNoResults(lookup: PlayerLookup | null): boolean {
  return lookup?.state === "tracking" && (lookup.reason ?? "pending") !== "pending";
}

// What a visit shows and how often it rereads saved data. A successful lookup
// alone decides the state; saved results show as current only when it says
// tracking too. Once explained, the visit rereads once a minute, through failed
// or partial reads, until a successful lookup gives a normal page or a final
// answer.
export function playerLookupView(
  player: PlayerPage | null,
  fetched: PlayerLookup | null,
  explainedVisit: boolean,
) {
  const current =
    fetched === null
      ? !explainedVisit
      : fetched.state === "tracking" && !explainsNoResults(fetched);
  const trackedPlayer = current && player?.trackingState === "tracking" ? player : null;
  const lookup: PlayerLookup | null =
    fetched ??
    (player && !explainedVisit ? { tag: player.tag, state: player.trackingState } : null);
  const minuteChecks =
    explainedVisit &&
    trackedPlayer === null &&
    (lookup === null || lookup.state === "checking" || lookup.state === "tracking");
  const isChecking =
    !explainedVisit &&
    (lookup?.state === "checking" ||
      (lookup?.state === "tracking" && trackedPlayer === null));
  return { trackedPlayer, lookup, minuteChecks, isChecking };
}

/**
 * Whether a player without current results has a current Season to show. A
 * player outside Legend I needs a saved Legend day this Season, so the page
 * never shows them an empty Season.
 */
export function showsCurrentSeason(
  lookup: PlayerLookup | null,
  history: { day: RankedDaySummary }[],
) {
  return (
    history.some(
      ({ day }) =>
        day.presentation?.status !== "not_enrolled" ||
        day.offenseEvents.length + day.defenseEvents.length > 0,
    ) || !["not_in_legend", "uncertain"].includes(lookup?.state ?? "")
  );
}

/**
 * Why a tracked player's newest profile gives no current results, or null
 * when nothing needs explaining. Clash Lens rechecks these players' profiles
 * every 15 minutes.
 */
export function lookupExplanation(
  reason: PlayerLookup["reason"] | null,
  name: string | null | undefined,
): string | null {
  switch (reason) {
    case "no_legend_battles":
      return `${name ?? "This player"} is in Legend League but hasn't played a Legend League battle this Season.`;
    case "season_unconfirmed":
      return "Clash of Clans has not confirmed this player's Season yet. We are still checking, but current results are unavailable until it does.";
    case "unknown_tier":
      return "Clash of Clans reported a league we do not recognize for this player. We are still checking, but current results are unavailable until it reports a known league.";
    case "profile_rejected":
      return "Clash of Clans sent player details we could not use. We are still checking, but current results are unavailable until it sends valid details.";
    default:
      return null;
  }
}

const STATUS_LABELS: Record<DayStatus, string> = {
  in_progress: "In progress",
  verified: "Verified",
  calculated: "Calculated",
  uncertain: "Uncertain",
  not_enrolled: "Not enrolled",
};

export function dayPresentation(day: {
  presentation: DayPresentation | null;
  state: string;
  attacks?: number | null;
  defenses?: number | null;
  offenseEvents?: unknown[];
  defenseEvents?: unknown[];
}) {
  const fact = day.presentation!;
  const reasons = dayReasons(
    fact.status === "not_enrolled" ? ["not_enrolled"] : fact.reason_codes,
    fact.is_current,
    {
      attacks: day.attacks ?? day.offenseEvents?.length ?? null,
      defenses: day.defenses ?? day.defenseEvents?.length ?? null,
    },
  );
  const cautions = dayReasons(fact.caution_codes, true, {
    attacks: day.attacks ?? day.offenseEvents?.length ?? null,
    defenses: day.defenses ?? day.defenseEvents?.length ?? null,
  });
  if (fact.is_current && !fact.live_routine && cautions.length === 0)
    cautions.push("Some daily evidence is unavailable.");
  if (fact.is_current)
    reasons.push(...cautions.filter((reason) => !reasons.includes(reason)));
  else if (
    reasons.length === 0 &&
    fact.status !== "verified" &&
    (fact.status !== "calculated" || day.state === "Live")
  )
    reasons.push(
      day.state === "Live"
        ? "Final evidence for this day has not been processed yet."
        : "Some daily evidence is unavailable.",
    );
  return {
    status: STATUS_LABELS[fact.status],
    reasons,
    battleNet: fact.battle_net,
    cautions,
  };
}

// The checks after Reset, which a Legend day in progress cannot have yet. The
// automatic defense loss also waits for Reset, and for the previous day.
const WAITING_REASONS = new Set([
  "missing_end_battle_log_baseline",
  "missing_end_baseline",
]);
const AUTOMATIC_DEFENSE = "automatic_defense_basis_unavailable";

/**
 * The note above the daily log for today's Legend day, or null when there is
 * nothing to add: a routine wait in progress is already shown by the day's
 * In progress badge. Once the page's clock passes Reset, a routine wait is no
 * longer in progress, even before the page rereads it; a caution stays.
 */
export function liveDayNotice(day: RankedDaySummary, ended: boolean, label: string) {
  const { cautions } = dayPresentation(day);
  if (!day.presentation!.live_routine)
    return { heading: label, text: cautions.join(" ") };
  return ended
    ? {
        heading: "Day ended",
        text: "This Legend day has ended. Updated results are not on this page yet.",
      }
    : null;
}

const REASON_TEXT: Record<string, string> = {
  missing_start_battle_log_baseline:
    "The battle log was not checked at the start of this day.",
  missing_end_battle_log_baseline: "The battle log was not checked after this day ended.",
  missing_start_baseline: "Trophies at the start of this day were not recorded.",
  start_baseline_incomplete: "The start-of-day trophy reading is incomplete.",
  missing_end_baseline: "Trophies at the end of this day were not recorded.",
  end_baseline_incomplete: "The end-of-day trophy reading is incomplete.",
  end_reading_unverified:
    "No trophy reading after this day could confirm its end: a battle may still have been landing, an attack's trophies may have shown late, part of a battle log could not be read, or the weekly raise to 5,000 hid the automatic defense loss.",
  battle_log_stale_window:
    "The battle log was not checked often enough to be sure every battle was seen.",
  battle_log_overlap_gap: "Some battles may be missing between two battle log checks.",
  battle_log_row_gap: "Part of a battle log reply could not be read.",
  battle_log_row_count_exceeds_fifty: "Part of a battle log reply could not be read.",
  duplicate_battle_identity_in_observation:
    "Part of a battle log reply could not be read.",
  unclassified_rows: "Some battles in the log could not be identified.",
  perspective_disagreement: "The two players' battle logs disagree about a result.",
  duplicate_contribution_disagreement:
    "The two players' battle logs disagree about a result.",
  trophy_equation_mismatch:
    "Recorded battles do not add up to the change between trophy readings.",
  automatic_defense_basis_unavailable:
    "The automatic defense loss at Reset could not be calculated.",
  season_anchor_conflict: "The Season start date could not be confirmed.",
  player_not_eligible: "The player was not in Legend I for all of this day.",
  not_enrolled: "The player had not signed up for this Season yet.",
  shield_sequence_longer_than_two_days:
    "A shield period was longer than expected and could not be explained.",
  malformed_evidence: "Some saved evidence for this day could not be read.",
  malformed_contribution: "Some saved evidence for this day could not be read.",
  "ranked_day_state:Inconsistent": "The evidence for this day conflicts.",
  "ranked_day_state:Malformed": "Some saved evidence for this day could not be read.",
  battle_event_projection_incomplete: "Not every recorded battle is listed for this day.",
  detailed_boundaries_unavailable:
    "Detailed start and end readings for this day were not saved.",
  ranked_version_missing: "Some saved evidence for this day could not be read.",
  malformed_battle_entries: "Some saved evidence for this day could not be read.",
  ranked_version_mismatch: "The evidence for this day conflicts.",
  attack_star_total_mismatch: "Recorded attacks do not match the day's attack count.",
  defense_star_total_mismatch: "Recorded defenses do not match the day's defense count.",
  truncated_reasons: "More reasons were saved than can be shown.",
};
// Plain words for Python's reason codes.
export function dayReasons(
  codes: string[],
  isCurrentDay: boolean,
  counts: { attacks: number | null; defenses: number | null } = {
    attacks: null,
    defenses: null,
  },
): string[] {
  const reasons = codes.map((code) =>
    code === "attack_count_exceeds_eight"
      ? excessNote(counts.attacks, "attacks")
      : code === "defense_count_exceeds_eight"
        ? excessNote(counts.defenses, "defenses")
        : isCurrentDay && WAITING_REASONS.has(code)
          ? "Ending evidence arrives after Reset."
          : isCurrentDay && code === AUTOMATIC_DEFENSE
            ? "Any automatic defense loss needs complete records for this Legend day and the previous one."
            : (REASON_TEXT[code] ?? "Some daily evidence is unavailable."),
  );
  return [...new Set(reasons)];
}

function excessNote(count: number | null, kind: "attacks" | "defenses"): string {
  return count === null
    ? `Clash of Clans returned more than the usual 8 ${kind} for this day, so this day is uncertain.`
    : `Clash of Clans returned ${count} ${kind} for this day, more than the usual 8, so this day is uncertain.`;
}

export function legendDayKey(period: string): string {
  return period.slice(0, 10);
}

// Every saved record of a player, in the order the log prefers them.
export function savedDays(player: PlayerPage) {
  return [
    ...player.seasonDays,
    ...(player.currentDay ? [player.currentDay] : []),
    ...player.recentDays,
  ];
}

// Python supplies the end and its disagreement; only wording belongs here.
export function dayEnd(day: RankedDaySummary) {
  const end = day.displayEnd!;
  const c = end.conflict;
  const own = end.after_reset;
  const conflict = c
    ? `Battles add up to ${c.battle_end.toLocaleString("en-GB")}${c.reset_kind ? `, which the ${c.reset_kind === "season" ? "Season" : "weekly"} reset takes to ${own!.toLocaleString("en-GB")}` : ""}; next day started at ${c.next_start.toLocaleString("en-GB")}.`
    : null;
  const evidence = dayPresentation(
    day.presentation!.is_current
      ? {
          ...day,
          presentation: { ...day.presentation!, status: end.status, is_current: false },
        }
      : day,
  );
  return {
    trophies: end.player_log,
    status: STATUS_LABELS[end.status],
    reasons: conflict ? [conflict, ...evidence.reasons] : evidence.reasons,
    conflict,
  };
}

export function selectPlayerHistory(player: PlayerPage | null) {
  return (player?.historyDays ?? []).map((day) => ({
    day,
    dayNumber: player!.historyDayNumbers[day.start],
    seasonDay: `Day ${player!.historyDayNumbers[day.start]}`,
  }));
}

// The ended Season whose saved days include this Legend day (YYYY-MM-DD).
export function seasonForDay(seasons: SummarizedSeasonRef[], day: string): string | null {
  const season = seasons.find(
    ({ seasonStart, seasonEnd, source }) =>
      source === "tracked_summary" &&
      seasonStart !== null &&
      seasonEnd !== null &&
      seasonStart.slice(0, 10) <= day &&
      day < seasonEnd.slice(0, 10),
  );
  return season?.seasonId ?? null;
}
