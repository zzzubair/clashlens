import type { PlayerPage, RankedDaySummary } from "../lib/contracts";
import { dayEnd } from "../lib/player-lookup-text";

const WIDTH = 320;
const HEIGHT = 56;

/**
 * The Season so far as one line: each finished Legend day's EOD, then the
 * trophies now. Days without a known EOD are skipped. Drawn only from two
 * points up, with its first value and "Now" under its two ends.
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
  const ends = [...history]
    .reverse()
    .map(({ day }) => dayEnd(day, player, now).trophies)
    .filter((trophies): trophies is number => trophies !== null);
  const start = history.at(-1)?.day.startTrophies;
  const points = [...(start == null ? [] : [start]), ...ends, current];
  if (points.length < 2) return null;
  const low = Math.min(...points);
  const high = Math.max(...points);
  const span = Math.max(high - low, 40);
  const x = (index: number) => (index / (points.length - 1)) * WIDTH;
  const y = (value: number) => 4 + (1 - (value - low) / span) * (HEIGHT - 10);
  const line = points
    .map(
      (value, index) =>
        `${index ? "L" : "M"}${x(index).toFixed(1)} ${y(value).toFixed(1)}`,
    )
    .join(" ");
  const first = points[0];
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
          <linearGradient id="trophy-line-stroke" x1="0" y1="0" x2="1" y2="0">
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
          d={line}
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
