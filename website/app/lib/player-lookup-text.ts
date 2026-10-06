import { seasonStartAt } from "../components/SeasonReread";
import type {
  PlayerLookup,
  PlayerPage,
  RankedDaySummary,
  SummarizedSeasonRef,
} from "./contracts";

/** What a player page says about a tag without current results. */
export const LOOKUP_MESSAGES: Record<PlayerLookup["state"], string> = {
  unknown: "Waiting to check this tag with Clash of Clans.",
  checking:
    "Checking this tag with Clash of Clans. Legend I players start tracking automatically.",
  tracking: "Now tracking in Legend I. The first results are being prepared.",
  not_found:
    "Player not found. Clash of Clans did not find this tag. Check the tag and try again.",
  not_in_legend:
    "This player is not in Legend I. Clash Lens tracks Legend League players only. We have kept the tag and any saved history.",
  uncertain:
    "Clash Lens tracks Legend League players only. This player exists, but we could not confirm they are in Legend I. Any saved history is still available.",
  failed:
    "We could not finish checking this tag. This does not mean the player is missing or outside Legend I.",
};

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

export interface DayEvidence {
  net: number | null;
  state: string;
  coverage: string;
  codes: string[];
  attackGain: number | null;
  defenseLoss: number | null;
  attacks: number | null;
  defenses: number | null;
  battlesComplete?: boolean;
}

export function dayEvidence(day: RankedDaySummary): DayEvidence {
  return {
    net: day.trophyChange,
    state: day.state,
    coverage: day.completeness.state,
    codes: day.uncertainty,
    attackGain: day.offense.trophyGain,
    defenseLoss: day.defense.trophyLoss,
    attacks: day.offenseEvents.length,
    defenses: day.defenseEvents.length,
    battlesComplete: day.battlesComplete,
  };
}

// Codes that leave a day's 8 attacks and 8 defenses in doubt, or may hide such a code.
const BATTLE_DOUBT_CODES = new Set([
  "perspective_disagreement",
  "duplicate_contribution_disagreement",
  "trophy_equation_mismatch",
  "ranked_version_mismatch",
  "attack_star_total_mismatch",
  "defense_star_total_mismatch",
  "truncated_reasons",
]);

// Only Python's calendar check makes a day current; a saved "Live" state can
// outlast its day. No saved result proves the Reset settled yet, so a finished
// day with a number is still provisional. A finished day with every battle
// recorded (all 8 of each) has nothing missing from its number. Python marks
// this for recent days; saved Season entries carry only their counts and codes.
export function presentDay(day: DayEvidence, isCurrentDay: boolean) {
  const battlesComplete =
    day.battlesComplete ??
    (day.attacks === 8 &&
      day.defenses === 8 &&
      !day.codes.some((code) => BATTLE_DOUBT_CODES.has(code)));
  const status = isCurrentDay
    ? "In progress"
    : day.net === null
      ? "Result unknown"
      : !battlesComplete &&
          (day.state !== "Complete" ||
            day.coverage !== "complete" ||
            day.codes.length > 0)
        ? "Incomplete"
        : "Provisional result";
  const reasons = isCurrentDay ? liveDay(day).reasons : dayReasons(day.codes, false, day);
  if (reasons.length === 0 && status === "Incomplete")
    reasons.push(
      day.state === "Live"
        ? "Final evidence for this day has not been processed yet."
        : "Some daily evidence is unavailable.",
    );
  const battleNet =
    day.attackGain === null || day.defenseLoss === null
      ? null
      : day.attackGain - day.defenseLoss;
  return { status, reasons, battleNet };
}

// The checks after Reset, which a Legend day in progress cannot have yet. The
// automatic defense loss also waits for Reset, and for the previous day.
const WAITING_REASONS = new Set([
  "missing_end_battle_log_baseline",
  "missing_end_baseline",
]);
const AUTOMATIC_DEFENSE = "automatic_defense_basis_unavailable";

/**
 * Today's Legend day. It is only waiting for Reset when every reason is one
 * it cannot avoid before then; any other reason is a caution to keep visible.
 */
export function liveDay(day: DayEvidence) {
  const cautions = dayReasons(
    day.codes.filter((code) => !WAITING_REASONS.has(code) && code !== AUTOMATIC_DEFENSE),
    true,
    day,
  );
  const routine =
    day.state === "Live" && day.coverage === "partial" && cautions.length === 0;
  if (!routine && cautions.length === 0)
    cautions.push("Some daily evidence is unavailable.");
  return {
    routine,
    cautions,
    reasons: [...new Set([...dayReasons(day.codes, true, day), ...cautions])],
  };
}

/**
 * The note above the daily log for today's Legend day, or null when there is
 * nothing to add: a routine wait in progress is already shown by the day's
 * In progress badge. Once the page's clock passes Reset, a routine wait is no
 * longer in progress, even before the page rereads it; a caution stays.
 */
export function liveDayNotice(day: DayEvidence, ended: boolean, label: string) {
  const { routine, cautions } = liveDay(day);
  if (!routine) return { heading: label, text: cautions.join(" ") };
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
    ? `Clash of Clans returned more than the usual 8 ${kind} for this day, so this day is marked partial.`
    : `Clash of Clans returned ${count} ${kind} for this day, more than the usual 8, so this day is marked partial.`;
}

export function legendDayKey(period: string): string {
  return period.split(" – ")[0].slice(0, 10);
}

const DAY_MS = 24 * 60 * 60 * 1000;

// The log keeps only the current Season's days at the server time, numbered
// from its start; an ended Season's days are under that Season in Seasons.
export function selectPlayerHistory(player: PlayerPage | null, now: number) {
  const anchor = player?.season ? Date.parse(player.season.anchor) : null;
  const seasonStart =
    anchor !== null && now < anchor + 28 * DAY_MS ? anchor : seasonStartAt(now);
  const seasonDay = (day: RankedDaySummary) =>
    Math.floor((Date.parse(day.period.split(" – ")[0]) - seasonStart) / DAY_MS) + 1;
  const days = [
    ...(player?.seasonDays ?? []),
    ...(player?.currentDay ? [player.currentDay] : []),
    ...(player?.recentDays ?? []),
  ].filter(
    (day) =>
      seasonDay(day) >= 1 &&
      seasonDay(day) <= 28 &&
      (!day.uncertainty.includes("player_not_eligible") ||
        day.offenseEvents.length > 0 ||
        day.defenseEvents.length > 0),
  );
  return days
    .filter(
      (day, index) =>
        days.findIndex(
          (saved) => legendDayKey(saved.period) === legendDayKey(day.period),
        ) === index,
    )
    .sort((a, b) => legendDayKey(b.period).localeCompare(legendDayKey(a.period)))
    .map((day) => ({ day, seasonDay: `Day ${seasonDay(day)}` }));
}

// The ended Season whose saved days include this Legend day (YYYY-MM-DD).
export function seasonForDay(seasons: SummarizedSeasonRef[], day: string): string | null {
  const start = Date.parse(`${day}T05:00:00Z`);
  const season = seasons.find(({ seasonId, source }) => {
    const seasonStart = Number(seasonId) * 1000;
    return (
      source === "tracked_summary" &&
      seasonStart <= start &&
      start < seasonStart + 28 * DAY_MS
    );
  });
  return season?.seasonId ?? null;
}
