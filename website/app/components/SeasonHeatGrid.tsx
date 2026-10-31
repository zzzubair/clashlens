import "../season-heat.css";

import type { HeatCell } from "../lib/dashboard-cards";
import { heatStep } from "../lib/dashboard-cards";

// The Season heat map in its one form, for the dashboard, the player page
// and a profile alike: a week to a row, each row named by its first day,
// each day's result in its cell, coloured against the player's own Season
// from worst (red) to best (green). Today says so and the days to come are
// dotted outlines. A day's signed value says whether it lost trophies, as
// relative colours can paint a small gain red.

const sign = (value: number) => (value > 0 ? "+" : value < 0 ? "−" : "");

/** A day's result as a cell shows it: a rare four-digit one in thousands. */
function shown(value: number): string {
  const size = Math.abs(value);
  return `${sign(value)}${size < 1000 ? size.toLocaleString("en-GB") : `${(size / 1000).toFixed(1)}k`}`;
}

/** A day's result in words, for screen readers and readouts. */
export function heatResult(cell: HeatCell): string {
  if (cell.kind === "today") return "today, not finished yet";
  if (cell.kind === "future") return "not played yet";
  if (cell.kind === "missing" || cell.value === null) return "no data";
  if (cell.kind === "shield") return "no battles";
  return `${sign(cell.value)}${Math.abs(cell.value).toLocaleString("en-GB")}`;
}

/**
 * The grid itself. With `onPick` each day is a button that can be picked and
 * read out (`onPoint` follows the pointer and keyboard focus); without it the
 * days are plain cells, named for screen readers.
 */
export function SeasonHeatGrid({
  cells,
  label,
  loss,
  picked = null,
  onPick,
  onPoint,
}: {
  cells: HeatCell[];
  /** The list's name for screen readers. */
  label: string;
  /** The values are each day's net change, so a negative one is a day that
   *  lost trophies, named by the class is-loss (it has no look of its own). */
  loss: boolean;
  picked?: number | null;
  onPick?: (day: number) => void;
  onPoint?: (day: number | null) => void;
}) {
  return (
    <div className="season-heat">
      <ol className="season-heat-days" aria-label={label}>
        {cells.map((cell) => {
          const step = cell.shade === null ? null : heatStep(cell.shade);
          const lost = loss && cell.kind === "played" && (cell.value ?? 0) < 0;
          const text =
            cell.kind === "today"
              ? "Today"
              : cell.kind === "future"
                ? ""
                : cell.value === null || cell.kind === "missing"
                  ? "–"
                  : shown(cell.value);
          const name = `Day ${cell.day}: ${heatResult(cell)}`;
          return (
            <li
              key={cell.day}
              // Three digits and a sign take a size down to keep their room.
              className={`season-heat-${cell.kind}${lost ? " is-loss" : ""}${text.length > 3 && cell.kind !== "today" ? " is-long" : ""}`}
              style={
                step === null
                  ? undefined
                  : ({
                      "--heat": `var(--cl-heat-${step})`,
                      "--heat-text": `var(--cl-heat-${step}-text, var(--cl-heat-text))`,
                    } as React.CSSProperties)
              }
            >
              {/* Each row is a week, named by its first day. */}
              {cell.day % 7 === 1 ? (
                <span className="season-heat-week" aria-hidden="true">
                  D<span>ay </span>
                  {cell.day}
                </span>
              ) : null}
              {onPick ? (
                <button
                  type="button"
                  aria-pressed={picked === cell.day}
                  aria-label={name}
                  onClick={() => onPick(cell.day)}
                  onMouseEnter={() => onPoint?.(cell.day)}
                  onMouseLeave={() => onPoint?.(null)}
                  onFocus={() => onPoint?.(cell.day)}
                  onBlur={() => onPoint?.(null)}
                >
                  {text}
                </button>
              ) : (
                <>
                  <span className="sr-only">{name}</span>
                  <span aria-hidden="true">{text}</span>
                </>
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}

/** The scale in words: the same words on every page. */
export function HeatKey() {
  return (
    <span className="season-heat-key">
      <span className="sr-only">
        Coloured against the player's own Season: best days green, worst red.
      </span>
      <span className="season-heat-scale" aria-hidden="true">
        Season: worst
        <span className="season-heat-ramp">
          {[0, 1, 2, 3, 4].map((step) => (
            <i key={step} style={{ background: `var(--cl-heat-${step})` }} />
          ))}
        </span>
        best
      </span>
    </span>
  );
}

/**
 * The line under a grid: the key, covered by the pointed or picked day's
 * readout while there is one, so nothing jumps. A phone, with no pointer,
 * puts the readout on its own line over the key, and reads out `fallback`
 * (today) until a day is picked.
 */
export function HeatFoot({
  readout,
  fallback = null,
  children,
}: {
  readout: React.ReactNode;
  fallback?: React.ReactNode;
  children: React.ReactNode;
}) {
  const line = readout || fallback;
  return (
    <p className="season-heat-foot" aria-live="polite">
      {line ? (
        <span className={`season-heat-readout${readout ? "" : " is-fallback"}`}>
          {line}
        </span>
      ) : null}
      <span className={readout ? "is-covered" : undefined}>{children}</span>
    </p>
  );
}
