import "../season-heat.css";

import type { HeatCell } from "../lib/dashboard-cards";
import { heatStep } from "../lib/dashboard-cards";

// The Season heat map in its one form, for the dashboard, the player page
// and a profile alike: a week to a row, each row named by its first day,
// each day a square of ember pixels, denser the better the day against the
// player's own Season, and red pixels for a day that lost trophies, denser
// the bigger the loss. Each day's figure reads out on hover, focus or tap
// (and in the Daily Legend log); today is an ember ring and the days to come
// are faint outlines.

const sign = (value: number) => (value > 0 ? "+" : value < 0 ? "−" : "");

/** A day's result as a cell shows it, signed so "72" never reads as a rank
 *  (a loss also red); a rare four-digit one in thousands. */
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
   *  lost trophies, named by the class is-loss, which draws it in red. */
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
          // A played day is its pixels alone; its figure is in its name.
          const text =
            step !== null
              ? ""
              : cell.kind === "today"
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
              className={`season-heat-${cell.kind}${lost ? " is-loss" : ""}`}
              data-step={step ?? undefined}
            >
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

/** The scale: one ramp from the worst day to the best, red pixels for
 *  losses and ember for gains, as the grid draws them, ended by the
 *  Season's own worst and best results when there are `cells` to read them
 *  from (else the words). A Season of only losses or only gains shows only
 *  its own half; 0 marks where they meet. */
export function HeatKey({ loss = true, cells }: { loss?: boolean; cells?: HeatCell[] }) {
  const values = (cells ?? []).flatMap((cell) =>
    cell.kind === "played" && cell.value !== null ? [cell.value] : [],
  );
  const worst = values.length ? Math.min(...values) : null;
  const best = values.length ? Math.max(...values) : null;
  const red = loss && (worst === null || worst < 0);
  const ember = !loss || best === null || best >= 0;
  const ends =
    worst === null || best === null ? "" : ` from ${shown(worst)} to ${shown(best)}`;
  return (
    <span className="season-heat-key">
      <span className="sr-only">
        {`Each day against the player's own Season${ends}: `}
        {red && ember
          ? "losses red and gains ember, denser the further from 0."
          : red
            ? "every day a loss, red, denser the bigger the loss."
            : "denser ember the better the day."}
      </span>
      <span className="season-heat-scale" aria-hidden="true">
        {loss ? <span className="season-heat-unit">Net trophies</span> : null}
        {worst === null ? "worst" : shown(worst)}
        {red ? (
          <span className="season-heat-ramp">
            {[0, 1, 2].map((step) => (
              <i key={step} data-step={step} className="is-loss" />
            ))}
          </span>
        ) : null}
        {red && ember ? "0" : null}
        {ember ? (
          <span className="season-heat-ramp">
            {[0, 1, 2, 3, 4].map((step) => (
              <i key={step} data-step={step} />
            ))}
          </span>
        ) : null}
        {best === null ? "best" : shown(best)}
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
