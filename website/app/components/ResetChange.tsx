import { rankMovement } from "../lib/rank-movement";
import { utcDate, dayRange as compactDayRange } from "../lib/date-format";
import { dayBar, rowScale } from "../lib/day-bars";
import { useEffect, useRef, useState, type CSSProperties } from "react";

import type { RecentDay, TrackedPlayerEntry } from "../lib/contracts";
import { signedNumber as signed } from "../lib/number-format";

const RECENT_DAYS = 7;

export function comparesWithReset(entries: TrackedPlayerEntry[]) {
  return entries.some((entry) => entry.resetRank !== null);
}

export function RankMove({ entry }: { entry: TrackedPlayerEntry }) {
  if (entry.resetRank === null)
    return (
      <span className="rank-move" data-new="">
        new<span className="sr-only"> since Reset</span>
      </span>
    );
  const moved = rankMovement(entry.rank, entry.resetRank);
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

export function hasToday(entries: TrackedPlayerEntry[]) {
  return entries.some((entry) => entry.todayChange !== null);
}

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

function dailyChanges(entry: TrackedPlayerEntry): (RecentDay | null)[] {
  const days = entry.recentDays.slice(-RECENT_DAYS);
  return [...Array<null>(RECENT_DAYS - days.length).fill(null), ...days];
}

export function hasRecentDays(entries: TrackedPlayerEntry[]): boolean {
  return entries.some((entry) =>
    entry.recentDays.some((day) => day.trophyChange !== null),
  );
}

function dayRange(entries: TrackedPlayerEntry[]): string {
  const days = entries.find((entry) => entry.recentDays.length)?.recentDays ?? [];
  if (!days.length) return "The last 7 finished Legend days";
  return compactDayRange(days.map(({ start }) => start))!;
}

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

export function RecentDays({ entry }: { entry: TrackedPlayerEntry }) {
  const [shown, setShown] = useState<number | null>(null);
  const chart = useRef<HTMLSpanElement>(null);
  const pointer = useRef("mouse");
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
  const biggest = rowScale(
    days.map((day) => day?.trophyChange ?? null),
    0,
  );
  const label = days
    .flatMap((day) =>
      day?.trophyChange == null
        ? []
        : [`${utcDate(day.start, { weekday: true })} ${signed(day.trophyChange)}`],
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
            style={size ? ({ "--h": dayBar(size, biggest) } as CSSProperties) : undefined}
          >
            {shown === index ? (
              <span className="recent-tip" aria-hidden="true">
                {`${utcDate(day.start, { weekday: true })}: ${
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
