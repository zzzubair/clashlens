import type { PlayerPage, RankedDaySummary } from "../lib/contracts";
import { dayEnd } from "../lib/player-lookup-text";

const WIDTH = 320;
const HEIGHT = 56;
const DAY = 86_400_000;

/**
 * The Season so far as one line: each finished Legend day's EOD at its own
 * day, then the trophies now. A day without a known EOD keeps its place and
 * is bridged by a dashed line. Drawn only from two points up, with its first
 * value and "Now" under its two ends.
 */
export function TrophyLine({
  history,
  player,
  now,
  current,
}: {
  /** The Daily Legend log's days, newest first. */
  history: Array<{ day: RankedDaySummary }>;
  player: PlayerPage;
  now: number;
  current: number;
}) {
  const dayStart = (day: RankedDaySummary) => Date.parse(day.period.split(" – ")[0]);
  const oldest = history.at(-1)?.day;
  if (!oldest) return null;
  const origin = dayStart(oldest);
  // [days since the oldest day began, trophies]: its start, each known EOD, now.
  const marks: Array<[number, number]> = [];
  if (oldest.startTrophies != null) marks.push([0, oldest.startTrophies]);
  for (const { day } of [...history].reverse()) {
    const trophies = dayEnd(day, player, now).trophies;
    if (trophies !== null) marks.push([(dayStart(day) - origin) / DAY + 1, trophies]);
  }
  if (marks.length === 0) return null;
  // Now ends today's step, one day after the last finished day.
  marks.push([Math.max(marks.at(-1)![0], Math.floor((now - origin) / DAY) + 1), current]);
  const values = marks.map(([, value]) => value);
  const low = Math.min(...values);
  const high = Math.max(...values);
  const span = Math.max(high - low, 40);
  const last = marks.at(-1)![0] || 1;
  const y = (value: number) => 4 + (1 - (value - low) / span) * (HEIGHT - 10);
  const point = ([slot, value]: [number, number]) =>
    `${((slot / last) * WIDTH).toFixed(1)} ${y(value).toFixed(1)}`;
  // Neighbours more than a day apart have a missing EOD between them.
  const gap = (index: number) => marks[index][0] - marks[index - 1][0] > 1.001;
  const steps = (dashed: boolean) =>
    marks
      .slice(1)
      .map((mark, i) =>
        gap(i + 1) === dashed ? `M${point(marks[i])} L${point(mark)}` : "",
      )
      .join(" ");
  const line = marks.map((mark, i) => `${i ? "L" : "M"}${point(mark)}`).join(" ");
  const first = values[0];
  return (
    <figure className="trophy-line">
      <svg
        viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        preserveAspectRatio="none"
        aria-hidden="true"
        focusable="false"
      >
        <defs>
          <linearGradient id="trophy-line-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor="var(--cl-brand-orange)" stopOpacity="0.32" />
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
            <stop offset="0" stopColor="var(--cl-gold)" />
            <stop offset="1" stopColor="var(--cl-action-text)" />
          </linearGradient>
        </defs>
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
      <span
        className="trophy-line-now"
        style={{ top: `${(y(current) / HEIGHT) * 100}%` }}
        aria-hidden="true"
      />
      <figcaption>
        <span aria-hidden="true">
          This Season <b>{first.toLocaleString("en-GB")}</b>
        </span>
        <span aria-hidden="true">Now</span>
        <span className="sr-only">
          This Season: {first.toLocaleString("en-GB")} to{" "}
          {current.toLocaleString("en-GB")} trophies
        </span>
      </figcaption>
    </figure>
  );
}
