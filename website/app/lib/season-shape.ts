import type { RankedDaySummary } from "./contracts";
import { utcDate } from "./date-format";

const DAY_MS = 86_400_000;

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

export interface SeasonShape<Day = RankedDaySummary> {
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
  lastLegendDay: Day | null;
}

/** A Season day's date, such as "Mon 12 Oct". */
export function seasonDayDate(shape: SeasonShape<unknown>, number: number) {
  return utcDate(shape.seasonStart + (number - 1) * DAY_MS, { weekday: true });
}

/** One plain line for each stretch of Legend I this Season. */
export function runLines(shape: SeasonShape<unknown>): string[] {
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
export function logGaps(shape: SeasonShape<unknown>): LogGap[] {
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

export function gapLabel(shape: SeasonShape<unknown>, gap: LogGap) {
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
export function inGap(
  gaps: LogGap[],
  shape: SeasonShape<unknown>,
  day: { start: string },
) {
  const at = Date.parse(day.start);
  return gaps.some(
    (gap) => shape.seasonStart + (gap.first - 1) * DAY_MS <= at && at <= gap.lastStart,
  );
}
