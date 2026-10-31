import { useEffect, useRef, useState, type CSSProperties } from "react";

import type { RecentDay, TrackedPlayerEntry } from "../lib/contracts";
import { signed } from "./LegendClock";
import { formatAge } from "./Provenance";

/** Finished days a row's chart draws; today has its own column. */
const RECENT_DAYS = 7;

/**
 * Whether a Live board can say how its rows moved since the last Reset. On a
 * Season's first Legend day, and on a Daily board, no row has a Reset place.
 */
export function comparesWithReset(entries: TrackedPlayerEntry[]) {
  return entries.some((entry) => entry.resetRank !== null);
}

/** Places moved since the last Reset: ▲ up, ▼ down, a quiet "new" when the
 *  player was not on that board, and a dash when they hold their place, so
 *  every row's name starts at the same point. */
export function RankMove({ entry }: { entry: TrackedPlayerEntry }) {
  if (entry.resetRank === null)
    return (
      <span className="rank-move" data-new="">
        new<span className="sr-only"> since Reset</span>
      </span>
    );
  const moved = entry.resetRank - entry.rank;
  if (!moved)
    return (
      <span className="rank-move" data-held="">
        <span aria-hidden="true">–</span>
        <span className="sr-only">No change since Reset</span>
      </span>
    );
  const places = Math.abs(moved).toLocaleString("en-US");
  return (
    <span className="rank-move" data-up={moved > 0 || undefined}>
      <span aria-hidden="true">
        <span className="rank-move-arrow">{moved > 0 ? "▲" : "▼"}</span>
        {places}
      </span>
      <span className="sr-only">
        {`${moved > 0 ? "Up" : "Down"} ${places} since Reset`}
      </span>
    </span>
  );
}

/** Whether any row knows today's net, so the board shows the Today column. */
export function hasToday(entries: TrackedPlayerEntry[]) {
  return entries.some((entry) => entry.todayChange !== null);
}

/** Trophies won or lost today so far, from the Legend day's settled start,
 *  as the dashboard's Legend day card counts them; coloured only when they
 *  moved. */
export function TodayChange({ entry }: { entry: TrackedPlayerEntry }) {
  const change = entry.todayChange;
  if (change === null) return null;
  return (
    <span
      className="today-change"
      data-sign={change > 0 ? "up" : change < 0 ? "down" : undefined}
    >
      {signed(change)}
    </span>
  );
}

/**
 * A row's 7 chart slots, oldest first: its last 7 finished Legend days, each
 * named by its own start, after empty slots for days before the Season's
 * first, so every row's days line up.
 */
function dailyChanges(entry: TrackedPlayerEntry): (RecentDay | null)[] {
  const days = entry.recentDays.slice(-RECENT_DAYS);
  return [...Array<null>(RECENT_DAYS - days.length).fill(null), ...days];
}

/** Whether any row has a finished day to chart, so the board shows the
 *  Last 7 days column at all. */
export function hasRecentDays(entries: TrackedPlayerEntry[]): boolean {
  return entries.some((entry) =>
    entry.recentDays.some((day) => day.trophyChange !== null),
  );
}

/** An update's age as Home and Rankings word it: "4 min", not "4 minutes",
 *  so it fits beside a tag on a phone. */
export function shortAge(seconds: number) {
  return seconds < 60 ? "<1 min" : formatAge(seconds).replace(/ minutes?$/, " min");
}

/** A Legend day by the date it started, such as "Tue 27 Oct", as Season
 *  days are named elsewhere. */
function dayDate(start: string): string {
  return new Date(start)
    .toLocaleDateString("en-GB", {
      weekday: "short",
      day: "numeric",
      month: "short",
      timeZone: "UTC",
    })
    .replace("Sept", "Sep");
}

/** The finished days the charts draw, named once from the rows' own days:
 *  "22–28 Oct", or "29 Sep – 5 Oct" across a month's end. */
function dayRange(entries: TrackedPlayerEntry[]): string {
  const days = entries.find((entry) => entry.recentDays.length)?.recentDays ?? [];
  if (!days.length) return "The last 7 finished Legend days";
  const [first, last] = [days[0], days[days.length - 1]].map(({ start }) => {
    const date = new Date(start);
    return {
      day: date.getUTCDate(),
      month: date
        .toLocaleDateString("en-GB", { month: "short", timeZone: "UTC" })
        .replace("Sept", "Sep"),
    };
  });
  return first.month === last.month
    ? `${first.day}–${last.day} ${last.month}`
    : `${first.day} ${first.month} – ${last.day} ${last.month}`;
}

/** The chart's column heading, two words; its tooltip names the dates and
 *  says how to read the bars. */
export function RecentDaysHead({ entries }: { entries: TrackedPlayerEntry[] }) {
  return (
    <th
      scope="col"
      className="recent-cell"
      title={`${dayRange(entries)}, oldest first: trophies won (up) or lost (down) each day. Each row has its own scale. Point at or tap a bar for its day.`}
    >
      Last 7 days
    </th>
  );
}

/**
 * The last 7 finished days as thin bars up (won) or down (lost) from a zero
 * line across the middle, scaled to the row's own biggest day either way, so
 * every row shows its shape however big another row's days are. The last bar
 * is the latest finished day; today is the Today column's, not a bar. A day
 * without a result is an empty slot. Each day is named by its own start.
 * Pointing at a bar, or tapping it, shows its date and trophies; screen
 * readers hear every day in the chart's label.
 */
export function RecentDays({ entry }: { entry: TrackedPlayerEntry }) {
  const [shown, setShown] = useState<number | null>(null);
  const chart = useRef<HTMLSpanElement>(null);
  const pointer = useRef("mouse");
  // A tapped day's readout stays until the next tap somewhere else.
  useEffect(() => {
    if (shown === null) return;
    const away = (event: PointerEvent) => {
      if (!chart.current?.contains(event.target as Node)) setShown(null);
    };
    document.addEventListener("pointerdown", away);
    return () => document.removeEventListener("pointerdown", away);
  }, [shown]);
  const days = dailyChanges(entry);
  if (days.every((day) => day?.trophyChange == null)) return null;
  // The row's biggest day, won or lost, reaches the top or the bottom.
  const biggest = Math.max(...days.map((day) => Math.abs(day?.trophyChange ?? 0)));
  const label = days
    .flatMap((day) =>
      day?.trophyChange == null
        ? []
        : [`${dayDate(day.start)} ${signed(day.trophyChange)}`],
    )
    .join(", ");
  const dayAt = (target: EventTarget) =>
    Number((target as Element).closest<HTMLElement>(".recent-day")?.dataset.index ?? NaN);
  return (
    <span
      ref={chart}
      className="recent-days"
      role="img"
      aria-label={`Daily trophies: ${label}`}
      onPointerDown={(event) => {
        pointer.current = event.pointerType;
      }}
      onPointerMove={(event) => {
        const index = dayAt(event.target);
        if (event.pointerType === "mouse" && !Number.isNaN(index)) setShown(index);
      }}
      onPointerLeave={(event) => {
        if (event.pointerType === "mouse") setShown(null);
      }}
      onClick={(event) => {
        // A click opens the player, as anywhere else on the row; a tap reads
        // the day out.
        if (pointer.current === "mouse")
          event.currentTarget
            .closest("tr")
            ?.querySelector<HTMLAnchorElement>("a.player-name")
            ?.click();
        else {
          const index = dayAt(event.target);
          setShown(Number.isNaN(index) || index === shown ? null : index);
        }
      }}
    >
      {days.map((day, index) => {
        // A slot before the Season's first day is no day: nothing to read out.
        if (day === null) return <span key={index} className="recent-day" />;
        const change = day.trophyChange;
        const sign =
          change === null ? undefined : change > 0 ? "up" : change < 0 ? "down" : "flat";
        const size = change ? Math.abs(change) : 0;
        return (
          <span
            key={index}
            className="recent-day"
            data-index={index}
            data-sign={sign}
            style={
              size ? ({ "--h": `${(size / biggest) * 50}%` } as CSSProperties) : undefined
            }
          >
            {shown === index ? (
              <span className="recent-tip" aria-hidden="true">
                {`${dayDate(day.start)}: ${
                  change === null ? "no saved result" : `${signed(change)} trophies`
                }`}
              </span>
            ) : null}
          </span>
        );
      })}
    </span>
  );
}
