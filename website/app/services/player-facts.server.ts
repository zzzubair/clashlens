import type {
  SeasonBounds,
  DayPresentation,
  DayEnd,
  BattleStatisticsFact,
} from "../lib/contracts";
import type { SeasonShape } from "../lib/season-shape";
import {
  PythonApiError,
  isRecord,
  isUtcTimestamp,
  isInteger,
  isFiniteNumber,
  isOneOf,
  isString,
} from "./python-response.server";

export function malformed(): never {
  throw new PythonApiError(502, { error: "malformed" });
}
export function record(value: unknown) {
  if (!isRecord(value)) malformed();
  return value;
}
export function timestamp(value: unknown) {
  if (!isUtcTimestamp(value)) malformed();
  return value;
}
export function nullableTimestamp(value: unknown) {
  return value === null ? null : timestamp(value);
}
export function integer(value: unknown) {
  if (!isInteger(value)) malformed();
  return value;
}
export function number(value: unknown) {
  if (!isFiniteNumber(value)) malformed();
  return value;
}
export function nullableNumber(value: unknown) {
  return value === null ? null : number(value);
}
export function boolean(value: unknown) {
  if (typeof value !== "boolean") malformed();
  return value;
}
export function strings(value: unknown): string[] {
  if (!Array.isArray(value) || !value.every(isString)) malformed();
  return value;
}
const statuses = [
  "in_progress",
  "verified",
  "calculated",
  "uncertain",
  "not_enrolled",
] as const;
function status(value: unknown) {
  if (!isOneOf(value, statuses)) malformed();
  return value;
}

export function mapSeasonBounds(value: unknown): SeasonBounds | null {
  if (value === null) return null;
  const v = record(value);
  if (!isString(v.id)) malformed();
  return {
    id: v.id,
    start: timestamp(v.start),
    end: timestamp(v.end),
    last_day_start: timestamp(v.last_day_start),
    day_count: integer(v.day_count),
  };
}
export function mapPresentation(value: unknown): DayPresentation {
  const v = record(value);
  return {
    status: status(v.status),
    battle_net: nullableNumber(v.battle_net),
    battle_net_complete: nullableNumber(v.battle_net_complete),
    is_current: boolean(v.is_current),
    show_in_player_log: boolean(v.show_in_player_log),
    live_routine: boolean(v.live_routine),
    reason_codes: strings(v.reason_codes),
    caution_codes: strings(v.caution_codes),
  };
}
export function mapDisplayEnd(value: unknown): DayEnd {
  const v = record(value);
  const c = v.conflict === null ? null : record(v.conflict);
  if (
    c &&
    !(c.reset_kind === null || isOneOf(c.reset_kind, ["weekly", "season"] as const))
  )
    malformed();
  return {
    before_reset: nullableNumber(v.before_reset),
    after_reset: nullableNumber(v.after_reset),
    player_log: nullableNumber(v.player_log),
    status: status(v.status),
    conflict: c
      ? {
          battle_end: number(c.battle_end),
          next_start: number(c.next_start),
          reset_amount: number(c.reset_amount),
          reset_kind: c.reset_kind as "weekly" | "season" | null,
        }
      : null,
  };
}
export function mapDisplayStart(value: unknown) {
  const v = record(value);
  if (!isOneOf(v.source, ["saved", "season_rule", "calculated", "unknown"] as const))
    malformed();
  const c = v.calculation === null ? null : record(v.calculation);
  return {
    trophies: nullableNumber(v.trophies),
    source: v.source,
    calculation: c
      ? { trophies: number(c.end_trophies), netChange: number(c.net_change) }
      : undefined,
  };
}
export function mapShape<Day extends { start: string }>(
  value: unknown,
  days: Day[],
): SeasonShape<Day> | null {
  if (value === null) return null;
  const v = record(value);
  if (
    !Array.isArray(v.kinds) ||
    !v.kinds.every((k) =>
      isOneOf(k, ["legend", "waiting", "outside", "unrecorded", "ahead"] as const),
    ) ||
    !Array.isArray(v.runs)
  )
    malformed();
  const last = nullableTimestamp(v.last_legend_day_start);
  const lastLegendDay =
    last === null ? null : days.find((day) => Date.parse(day.start) === Date.parse(last));
  if (lastLegendDay === undefined) malformed();
  const counts = record(v.legend_days);
  return {
    seasonStart: Date.parse(timestamp(v.season_start)),
    today: integer(v.today),
    kinds: v.kinds as SeasonShape["kinds"],
    runs: v.runs.map((item) => {
      const r = record(item);
      return {
        first: integer(r.first),
        last: integer(r.last),
        signedUp: r.signed_up === null ? null : integer(r.signed_up),
        promoted: boolean(r.promoted),
        dropped: boolean(r.dropped),
      };
    }),
    partial: boolean(v.partial),
    outsideNow: boolean(v.outside_now),
    legendDays: { expected: integer(counts.expected), saved: integer(counts.saved) },
    lastLegendDay,
  };
}
export function mapBattleStatistics(
  value: unknown,
): Record<"7" | "14" | "season", BattleStatisticsFact> {
  const v = record(value);
  const side = (value: unknown) => {
    const s = record(value);
    if (!Array.isArray(s.stars) || s.stars.length !== 4 || !s.stars.every(isInteger))
      malformed();
    return {
      count: integer(s.count),
      stars: s.stars,
      trophies: number(s.trophies),
      playedDays: integer(s.played_days),
      perDay: nullableNumber(s.per_day),
      perBattle: nullableNumber(s.per_battle),
      disputed: boolean(s.disputed),
    };
  };
  const period = (value: unknown) => {
    const p = record(value);
    return {
      start: timestamp(p.start),
      end: timestamp(p.end),
      daysSaved: integer(p.days_saved),
      daysExpected: integer(p.days_expected),
      incomplete: boolean(p.incomplete),
      resetDayStart: nullableTimestamp(p.reset_day_start),
      attack: side(p.attack),
      defense: side(p.defense),
    };
  };
  return { "7": period(v["7"]), "14": period(v["14"]), season: period(v.season) };
}

export function mapDashboardSeason(
  value: unknown,
): import("../lib/dashboard-cards").SeasonData {
  if (value === null) return { today: null, startMs: null, days: [] };
  const v = record(value);
  if (!Array.isArray(v.days)) malformed();
  return {
    today: integer(v.today),
    startMs: Date.parse(timestamp(v.start)),
    days: v.days.map((value) => {
      const d = record(value);
      if (!(
        d.hits === null ||
        (Array.isArray(d.hits) &&
          d.hits.every(
            (hit) => Array.isArray(hit) && hit.length === 2 && hit.every(isFiniteNumber),
          ))
      ))
        malformed();
      return {
        day: integer(d.day),
        attacks: nullableNumber(d.attacks),
        defenses: nullableNumber(d.defenses),
        offense: nullableNumber(d.offense),
        defense: nullableNumber(d.defense),
        net: nullableNumber(d.net),
        triples: nullableNumber(d.triples),
        held: nullableNumber(d.held),
        endTrophies: nullableNumber(d.end_trophies),
        rank: nullableNumber(d.rank),
        hits: d.hits as [number, number][] | null,
      };
    }),
  };
}
export function mapRivalSeason(value: unknown) {
  const v = record(value);
  return {
    resetRank: nullableNumber(v.reset_rank),
    hitRate: nullableNumber(v.hit_rate),
    offense: nullableNumber(v.offense),
    defense: nullableNumber(v.defense),
    netPerDay: nullableNumber(v.net_per_day),
    finishedDays: v.finished_days === null ? null : integer(v.finished_days),
  };
}

export function mapTrophyTrends(
  value: unknown,
): import("../lib/contracts").PlayerPage["trophyTrends"] {
  if (value === null) return null;
  const v = record(value);
  const window = (value: unknown) => {
    const w = record(value);
    return {
      span: integer(w.span),
      counted: integer(w.counted),
      total: nullableNumber(w.total),
    };
  };
  return { "7": window(v["7"]), "14": window(v["14"]) };
}
