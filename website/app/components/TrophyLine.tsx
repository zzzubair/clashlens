import { useState } from "react";

import type { PlayerPage, RankedDaySummary } from "../lib/contracts";
import { dayEnd } from "../lib/player-lookup-text";

const WIDTH = 320;
const HEIGHT = 100;
const DAY = 86_400_000;
// Round trophy steps for the scale; the first that needs at most four gaps.
const STEPS = [25, 50, 100, 200, 250, 500, 1000];

const date = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "short",
  timeZone: "UTC",
});
const count = (value: number) => value.toLocaleString("en-GB");
const signed = (value: number) =>
  value > 0 ? `+${count(value)}` : value < 0 ? `−${count(-value)}` : "±0";

/**
 * The Season so far as one line, read like a stock chart: each finished
 * Legend day's EOD at its own day, then the trophies now, over a trophy scale
 * at round counts down its left side. Each Reset has a dot that names its
 * count and change on hover or tap; the Daily Legend log below already shows
 * each day's rise or fall, so the chart does not shade them too. A day
 * without a known EOD keeps its place and is bridged by a dashed line. Drawn
 * only from two points up.
 */
export function TrophyLine({
  history,
  player,
  now,
  current,
  until = now,
  endLabel = "Now",
}: {
  /** The Daily Legend log's days, newest first. */
  history: Array<{ day: RankedDaySummary }>;
  player: PlayerPage;
  now: number;
  current: number;
  /** When the line ends: now, or the Reset a player left Legend I at. */
  until?: number;
  endLabel?: string;
}) {
  const [active, setActive] = useState<number | null>(null);
  const dayStart = (day: RankedDaySummary) => Date.parse(day.period.split(" – ")[0]);
  const oldest = history.at(-1)?.day;
  if (!oldest) return null;
  const origin = dayStart(oldest);
  // Trophies at each Reset, by days since the oldest day began: a day's start,
  // which is also the day before's EOD, and each known EOD. Then now.
  const resets = new Map<number, number>();
  for (const { day } of [...history].reverse()) {
    const at = (dayStart(day) - origin) / DAY;
    if (day.startTrophies != null && !resets.has(at)) resets.set(at, day.startTrophies);
    const end = dayEnd(day, player, now).trophies;
    if (end !== null) resets.set(at + 1, end);
  }
  const marks = [...resets].sort(([a], [b]) => a - b);
  if (marks.length === 0) return null;
  // Now ends today's step, one day after the last finished day; a line that
  // ends earlier ends at that Reset.
  const end = until < now ? (until - origin) / DAY : Math.floor((now - origin) / DAY) + 1;
  marks.push([Math.max(marks.at(-1)![0], end), current]);
  const values = marks.map(([, value]) => value);
  const low = Math.min(...values);
  const high = Math.max(...values);
  // The scale runs between round counts just outside the line's lowest and
  // highest points, with a level line at each step.
  const step = STEPS.find((size) => (high - low) / size <= 4) ?? 1000;
  const bottom = Math.floor(low / step) * step;
  const top = Math.max(Math.ceil(high / step) * step, bottom + step);
  const ticks = Array.from(
    { length: (top - bottom) / step + 1 },
    (_, i) => bottom + i * step,
  );
  const last = marks.at(-1)![0] || 1;
  const x = (slot: number) => (slot / last) * WIDTH;
  const y = (value: number) => (1 - (value - bottom) / (top - bottom)) * HEIGHT;
  const point = ([slot, value]: [number, number]) =>
    `${x(slot).toFixed(1)} ${y(value).toFixed(1)}`;
  // Neighbours more than a day apart have a missing EOD between them.
  const gap = (index: number) => marks[index][0] - marks[index - 1][0] > 1.001;
  const steps = (dashed: boolean) =>
    marks
      .slice(1)
      .flatMap((mark, i) =>
        gap(i + 1) === dashed ? [`M${point(marks[i])} L${point(mark)}`] : [],
      )
      .join(" ");
  const line = marks.map((mark, i) => `${i ? "L" : "M"}${point(mark)}`).join(" ");
  // A faint mark each week along the line, dated, so its slope has a scale.
  const weeks = [7, 14, 21].filter((at) => at < last - 1);
  // What a Reset's dot says: the day it ended, its count, and the change
  // since the Reset before when that was the day before.
  const resetText = (index: number) => {
    const [slot, value] = marks[index];
    const when =
      index === marks.length - 1
        ? endLabel
        : index === 0
          ? `${date.format(origin + slot * DAY)} start`
          : `${date.format(origin + (slot - 1) * DAY)} EOD`;
    const change = index > 0 && !gap(index) ? value - marks[index - 1][1] : null;
    return { slot, when, value, change };
  };
  // A Refresh can bring fewer Resets than the one picked; then nothing is.
  const shown = active === null || active >= marks.length ? null : resetText(active);
  // The nearest Reset to a pointer across the chart.
  const nearest = (event: React.PointerEvent<HTMLDivElement>) => {
    const box = event.currentTarget.getBoundingClientRect();
    const slot = ((event.clientX - box.left) / box.width) * last;
    let best = 0;
    marks.forEach(([at], i) => {
      if (Math.abs(at - slot) < Math.abs(marks[best][0] - slot)) best = i;
    });
    return best;
  };
  const at = (slot: number, value: number) => ({
    left: `${(slot / last) * 100}%`,
    top: `${(y(value) / HEIGHT) * 100}%`,
  });
  return (
    <figure className="trophy-line">
      {/* The figcaption's sentence says the same to a screen reader. */}
      <span className="trophy-line-title" aria-hidden="true">
        Season trophies
      </span>
      <div className="trophy-line-axis" aria-hidden="true">
        {ticks.map((tick) => (
          <span key={tick} style={{ top: `${(y(tick) / HEIGHT) * 100}%` }}>
            {count(tick)}
          </span>
        ))}
      </div>
      {/* Hover with a mouse or tap on a phone; each Reset is also listed for
          screen readers below, so the dots are not tab stops. */}
      <div
        className="trophy-line-plot"
        onPointerMove={(event) => {
          if (event.pointerType === "mouse") setActive(nearest(event));
        }}
        onPointerDown={(event) => {
          const index = nearest(event);
          setActive(event.pointerType === "mouse" || active !== index ? index : null);
        }}
        onPointerLeave={(event) => {
          if (event.pointerType === "mouse") setActive(null);
        }}
      >
        <svg
          viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
          preserveAspectRatio="none"
          aria-hidden="true"
          focusable="false"
        >
          <defs>
            <linearGradient id="trophy-line-fill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0" stopColor="var(--cl-brand-orange)" stopOpacity="0.22" />
              <stop offset="1" stopColor="var(--cl-brand-orange)" stopOpacity="0" />
            </linearGradient>
            {/* Drawn across the whole width, so the solid and dashed parts share it. */}
            <linearGradient
              id="trophy-line-stroke"
              gradientUnits="userSpaceOnUse"
              x1="0"
              y1="0"
              x2={WIDTH}
              y2="0"
            >
              {/* Text colours, so the line reads on light paper as on dark. */}
              <stop offset="0" stopColor="var(--cl-star)" />
              <stop offset="1" stopColor="var(--cl-brand-orange)" />
            </linearGradient>
          </defs>
          {ticks.map((tick) => (
            <line
              key={tick}
              className="trophy-line-level"
              x1="0"
              x2={WIDTH}
              y1={y(tick)}
              y2={y(tick)}
              vectorEffect="non-scaling-stroke"
            />
          ))}
          {weeks.map((week) => (
            <line
              key={week}
              className="trophy-line-week"
              data-near-end={week / last > 0.8 || undefined}
              x1={x(week)}
              x2={x(week)}
              y1="0"
              y2={HEIGHT}
              vectorEffect="non-scaling-stroke"
            />
          ))}
          <path
            className="trophy-line-area"
            d={`${line} L${WIDTH} ${HEIGHT} L0 ${HEIGHT} Z`}
            fill="url(#trophy-line-fill)"
          />
          <path
            className="trophy-line-path"
            d={steps(false)}
            stroke="url(#trophy-line-stroke)"
            vectorEffect="non-scaling-stroke"
          />
          <path
            className="trophy-line-path trophy-line-gap"
            d={steps(true)}
            stroke="url(#trophy-line-stroke)"
            vectorEffect="non-scaling-stroke"
          />
        </svg>
        {shown === null ? null : (
          <span
            className="trophy-line-cross"
            style={{ left: `${(shown.slot / last) * 100}%` }}
            aria-hidden="true"
          />
        )}
        {/* A dot at each Reset; the last, now, glows. */}
        {marks.map(([slot, value], i) => (
          <span
            key={slot}
            className={i === marks.length - 1 ? "trophy-line-now" : "trophy-line-dot"}
            data-active={i === active || undefined}
            style={at(slot, value)}
            aria-hidden="true"
          />
        ))}
        {shown === null ? null : (
          <span
            className="trophy-line-tip"
            style={at(shown.slot, shown.value)}
            data-side={
              shown.slot / last > 0.8
                ? "left"
                : shown.slot / last < 0.2
                  ? "right"
                  : undefined
            }
            data-below={y(shown.value) < HEIGHT * 0.4 || undefined}
            aria-hidden="true"
          >
            <small>{shown.when}</small>
            <b>{count(shown.value)}</b>
            {shown.change === null ? null : (
              <span
                className={
                  shown.change > 0
                    ? "score-positive"
                    : shown.change < 0
                      ? "score-negative"
                      : undefined
                }
              >
                {signed(shown.change)}
              </span>
            )}
          </span>
        )}
      </div>
      <figcaption>
        <span className="trophy-line-start" aria-hidden="true">
          {date.format(origin)}
        </span>
        {weeks.map((week) => (
          <span
            key={week}
            className="trophy-line-date"
            style={{ left: `${(week / last) * 100}%` }}
            data-near-end={week / last > 0.8 || undefined}
            aria-hidden="true"
          >
            {date.format(origin + week * DAY)}
          </span>
        ))}
        <span className="trophy-line-end" aria-hidden="true">
          {endLabel}
        </span>
        <span className="sr-only">
          This Season from {date.format(origin)}: {count(values[0])} to {count(current)}{" "}
          trophies.
        </span>
      </figcaption>
      <ol className="sr-only" aria-label="Trophies at each Reset">
        {marks.map(([slot], i) => {
          const { when, value, change } = resetText(i);
          return (
            <li key={slot}>
              {when}: {count(value)}
              {change === null ? "" : `, ${signed(change)}`}
            </li>
          );
        })}
      </ol>
    </figure>
  );
}
