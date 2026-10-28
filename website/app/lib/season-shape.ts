import { seasonStartAt } from "../components/SeasonReread";
import type { PlayerPage, RankedDaySummary, SeasonWeek } from "./contracts";

const DAY_MS = 86_400_000;
const WEEK_MS = 7 * DAY_MS;
const SEASON_START_TROPHIES = 5000;

/**
 * What one day of the Season was for this player: a Legend I day, a Legend I
 * day before they signed up (saved as 5,000 to 5,000 with no battles), a day
 * outside Legend I, a day the weekly league record has not decided, or a day
 * still ahead.
 */
export type SeasonDayKind = "legend" | "waiting" | "outside" | "unrecorded" | "ahead";

/** One unbroken stretch of Legend I weeks, by Season day number. */
export interface SeasonRun {
  first: number;
  /** The run's last day so far: today while it goes on. */
  last: number;
  /** The run's first day with a battle, or null before one. */
  signedUp: number | null;
  /** Whether the record says the player was promoted at the run's first Monday. */
  promoted: boolean;
  /** Whether the player dropped out of Legend I at the Monday Reset after `last`. */
  dropped: boolean;
}

export interface SeasonShape {
  seasonStart: number;
  /** Today's Season day number. */
  today: number;
  /** Each day's kind, Day 1 first. */
  kinds: SeasonDayKind[];
  runs: SeasonRun[];
  /** Whether the Season so far was anything but Legend I from Day 1. */
  partial: boolean;
  /** Whether the record puts the player outside Legend I today. */
  outsideNow: boolean;
  /** Finished Legend I days from each run's sign-up on, and how many are saved. */
  legendDays: { expected: number; saved: number };
  /** The newest finished Legend I day, the one a dropped player left on. */
  lastLegendDay: RankedDaySummary | null;
}

const startOf = (day: RankedDaySummary) => Date.parse(day.period.split(" – ")[0]);

/** A day known to be 5,000 to 5,000 with no battles: not one with unknown numbers. */
function unplayed(day: RankedDaySummary) {
  return (
    day.offenseEvents.length === 0 &&
    day.defenseEvents.length === 0 &&
    day.offense.attacks === 0 &&
    day.defense.defenses === 0 &&
    day.trophyChange === 0 &&
    day.startTrophies === SEASON_START_TROPHIES
  );
}

/**
 * The current Season as the weekly league record and the saved days tell it.
 * A player can leave Legend I only at a Monday Reset, but can enter it on any
 * day: promotion lands at the Monday Reset and each day until they sign up is
 * saved as 5,000 to 5,000 with no battles.
 */
export function seasonShape(player: PlayerPage, now: number): SeasonShape {
  const anchor = player.season ? Date.parse(player.season.anchor) : NaN;
  const seasonStart =
    anchor <= now && now < anchor + 28 * DAY_MS ? anchor : seasonStartAt(now);
  const today = Math.min(28, Math.floor((now - seasonStart) / DAY_MS) + 1);
  const saved = new Map<number, RankedDaySummary>();
  for (const day of [
    ...player.seasonDays,
    ...(player.currentDay ? [player.currentDay] : []),
    ...player.recentDays,
  ]) {
    const number = Math.floor((startOf(day) - seasonStart) / DAY_MS) + 1;
    if (number >= 1 && number <= 28 && !saved.has(number)) saved.set(number, day);
  }
  const weeks = new Map<number, SeasonWeek>(
    (player.seasonWeeks ?? []).map((week) => [Date.parse(week.start), week]),
  );
  const weekOf = (number: number) =>
    weeks.get(seasonStart + Math.floor((number - 1) / 7) * WEEK_MS);
  const kinds: SeasonDayKind[] = [];
  for (let number = 1; number <= 28; number++) {
    const league = weekOf(number)?.league;
    kinds.push(
      number > today
        ? "ahead"
        : league === "below_legend_i"
          ? "outside"
          : league === "legend_i" || saved.has(number)
            ? "legend"
            : "unrecorded",
    );
  }
  const runs: SeasonRun[] = [];
  for (let number = 1; number <= today; number++) {
    if (kinds[number - 1] !== "legend") continue;
    const first = number;
    while (number < today && kinds[number] === "legend") number++;
    // Until the player's first battle, each saved day is 5,000 to 5,000.
    let signedUp: number | null = null;
    for (let day = first; day <= number; day++) {
      const kept = saved.get(day);
      if (kept && !unplayed(kept)) {
        signedUp = day;
        break;
      }
      // Today, still being played, can't show the player has not signed up.
      if (kept && day < today) kinds[day - 1] = "waiting";
    }
    const entry = weekOf(first)?.entry;
    runs.push({
      first,
      last: number,
      signedUp,
      promoted: entry === "promoted" || entry === "first_seen",
      dropped: number < 28 && kinds[number] === "outside",
    });
  }
  let expected = 0;
  let savedCount = 0;
  let lastLegendDay: RankedDaySummary | null = null;
  for (let number = 1; number < today; number++) {
    if (kinds[number - 1] !== "legend") continue;
    expected++;
    const kept = saved.get(number);
    if (kept) {
      savedCount++;
      lastLegendDay = kept;
    }
  }
  const lastRun = runs.at(-1);
  if (lastRun?.dropped && lastRun.signedUp === null)
    lastLegendDay = saved.get(lastRun.last) ?? lastLegendDay;
  return {
    seasonStart,
    today,
    kinds,
    runs,
    partial: kinds.some((kind) => kind === "outside" || kind === "waiting"),
    outsideNow: kinds[today - 1] === "outside",
    legendDays: { expected, saved: savedCount },
    lastLegendDay,
  };
}

/** A Season day's date, such as "Mon 12 Oct". */
export function seasonDayDate(shape: SeasonShape, number: number) {
  return new Date(shape.seasonStart + (number - 1) * DAY_MS).toLocaleDateString("en-GB", {
    weekday: "short",
    day: "numeric",
    month: "short",
    timeZone: "UTC",
  });
}

/** One plain line for each stretch of Legend I this Season. */
export function runLines(shape: SeasonShape): string[] {
  return shape.runs.map((run) => {
    const ongoing = !run.dropped && run.last === shape.today;
    const days =
      run.first === run.last && !ongoing
        ? `Day ${run.first}`
        : ongoing
          ? `From Day ${run.first}`
          : `Days ${run.first}–${run.last}`;
    const parts = [days];
    if (run.first > 1)
      parts.push(
        `${run.promoted ? "promoted" : "joined"} at the ${seasonDayDate(shape, run.first)} Reset`,
      );
    if (run.signedUp === null)
      parts.push(run.dropped ? "never signed up" : "not signed up yet");
    else if (run.signedUp > run.first)
      parts.push(`signed up ${seasonDayDate(shape, run.signedUp)} (Day ${run.signedUp})`);
    if (run.dropped)
      parts.push(
        `dropped to Legend II at the ${seasonDayDate(shape, run.last + 1)} Reset`,
      );
    return parts.join(" · ");
  });
}

/** A stretch of the log with no Legend I battles: outside Legend I, or before sign-up. */
export interface LogGap {
  kind: "outside" | "waiting";
  first: number;
  last: number;
  /** The start of `last`, so the log can place the gap among its days. */
  lastStart: number;
}

/** The log's gaps up to yesterday, and today when it is one. */
export function logGaps(shape: SeasonShape): LogGap[] {
  const gaps: LogGap[] = [];
  for (let number = 1; number <= shape.today; number++) {
    const kind = shape.kinds[number - 1];
    if (kind !== "outside" && kind !== "waiting") continue;
    const first = number;
    while (number < shape.today && shape.kinds[number] === kind) number++;
    // Today's day before sign-up stays on today's card.
    const last = kind === "waiting" && number === shape.today ? number - 1 : number;
    if (last >= first)
      gaps.push({
        kind,
        first,
        last,
        lastStart: shape.seasonStart + (last - 1) * DAY_MS,
      });
  }
  return gaps;
}

export function gapLabel(shape: SeasonShape, gap: LogGap) {
  const from = seasonDayDate(shape, gap.first);
  const to =
    gap.kind === "outside" && gap.last === shape.today
      ? "now"
      : seasonDayDate(shape, gap.last);
  const dates = gap.first === gap.last && to !== "now" ? from : `${from} – ${to}`;
  return gap.kind === "outside"
    ? `${dates} · Not in Legend I, not tracked`
    : `${dates} · In Legend I, not signed up yet · 5,000`;
}

/** Whether a day of the log is one a gap row already stands for. */
export function inGap(gaps: LogGap[], shape: SeasonShape, day: RankedDaySummary) {
  const number = Math.floor((startOf(day) - shape.seasonStart) / DAY_MS) + 1;
  return gaps.some((gap) => gap.first <= number && number <= gap.last);
}

/** Whether the player left Legend I at the Reset ending this day. */
export function leftAfter(player: PlayerPage, day: RankedDaySummary) {
  const end = Date.parse(day.period.split(" – ")[1]);
  return (player.seasonWeeks ?? []).some(
    (week) => Date.parse(week.start) === end && week.league === "below_legend_i",
  );
}
