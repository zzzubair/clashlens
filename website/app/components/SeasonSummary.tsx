import { Fragment, useRef, useState } from "react";

import type { RankedDaySummary } from "../lib/contracts";
import { heatCells, heatStep, type SeasonDay } from "../lib/dashboard-cards";

// One side of a Season's recorded battles. Averages are trophies moved, never
// signed; null anywhere means that part is unknown.
export interface SummarySide {
  count: number | null;
  stars: Array<number | null>;
  unknown: number | null;
  perDay: number | null;
  perBattle: number | null;
}

const count = (value: number | null) =>
  value === null ? "Unknown" : value.toLocaleString("en-GB");
const signed = (value: number | null, sign: 1 | -1, digits = 0) =>
  value === null
    ? "Unavailable"
    : value === 0
      ? "0"
      : `${sign > 0 ? "+" : "−"}${value.toLocaleString("en-GB", {
          minimumFractionDigits: digits,
          maximumFractionDigits: digits,
        })}`;
// Words such as "Not published yet" take a smaller size than numbers.
const words = (value: string) => (/[a-z]/i.test(value) ? "summary-words" : undefined);
export const per = (total: number | null, by: number | null) =>
  total === null || !by ? null : total / by;
// Grid track sizes for a row's 3-, 2-, 1- and 0-star segments. A segment
// with any battles stays a visible sliver.
const shares = (stars: Array<number | null>) =>
  Object.fromEntries(
    [3, 2, 1, 0].map((star) => [
      `--s${star}`,
      stars[star] ? `minmax(0.3rem, ${stars[star]}fr)` : "0fr",
    ]),
  ) as React.CSSProperties;

// The one Season box: rank and trophies first, then hit rate, battles by
// stars and averages. Meta is one short line of dates and coverage. Without
// battles it shows only the headline. An ended Season's final rank is its
// standout number. Wide screens put the headline beside the attack and
// defense bars, each side's averages under its bar.
export function SeasonSummary({
  title,
  meta,
  controls,
  rank,
  finalRank = false,
  trophies,
  attack,
  defense,
  strip,
  children,
}: {
  title: string;
  meta?: string;
  controls?: React.ReactNode;
  rank: [label: string, value: string];
  finalRank?: boolean;
  trophies: [label: string, value: string, mark?: React.ReactNode];
  attack?: SummarySide;
  defense?: SummarySide;
  /** What a page shows under the headline numbers, such as the player page's heat strip. */
  strip?: React.ReactNode;
  children?: React.ReactNode;
}) {
  const total = attack?.count ?? null;
  const triples = attack?.stars[3] ?? null;
  const unknown = [
    [attack?.unknown, "attack"],
    [defense?.unknown, "defense"],
  ].flatMap(([value, side]) =>
    typeof value === "number" && value > 0
      ? [`${count(value)} ${side}${value === 1 ? "" : "s"}`]
      : [],
  );
  return (
    <section
      className="data-section season-summary"
      aria-labelledby="season-summary-title"
    >
      <div className="season-summary-head">
        <div>
          <h2 id="season-summary-title">{title}</h2>
          {meta ? (
            <p className="season-summary-meta" aria-live="polite">
              {meta}
            </p>
          ) : null}
        </div>
        {controls}
      </div>
      <dl className="season-summary-headline">
        {[rank, trophies].map(([label, value, mark]) => (
          <div
            key={label}
            className={finalRank && label === rank[0] ? "season-summary-rank" : undefined}
          >
            <dt>{label}</dt>
            <dd className={words(value)}>
              {value}
              {mark}
            </dd>
          </div>
        ))}
        {attack ? (
          <div>
            <dt>Hit rate</dt>
            <dd className={triples === null || !total ? "summary-words" : undefined}>
              {triples === null || !total
                ? "Unavailable"
                : `${((100 * triples) / total).toFixed(1)}%`}
              {triples === null || !total ? null : (
                <small>
                  {count(triples)} of {count(total)}
                </small>
              )}
              {triples === null || !total ? null : (
                <span className="meter" aria-hidden="true">
                  <span
                    style={
                      { "--value": `${(100 * triples) / total}%` } as React.CSSProperties
                    }
                  />
                </span>
              )}
            </dd>
          </div>
        ) : null}
      </dl>
      {strip}
      {attack && defense ? (
        <>
          <div
            className="season-summary-table"
            tabIndex={0}
            role="region"
            aria-label="Battles by stars"
          >
            <table>
              <thead>
                <tr className="sr-only">
                  <td />
                  <th scope="col">Total</th>
                  {[3, 2, 1, 0].map((stars) => (
                    <th scope="col" key={stars}>
                      <span aria-hidden="true">
                        {stars}
                        <span className="summary-star">★</span>
                      </span>
                      <span className="sr-only">{stars}-star</span>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {(
                  [
                    ["Attacks", attack],
                    ["Defenses", defense],
                  ] as const
                ).map(([label, side]) => (
                  // Each star count is a bar segment as wide as its share; the
                  // four columns under the bar name every count, so none hides
                  // in a sliver.
                  <Fragment key={label}>
                    <tr data-side={label} style={shares(side.stars)}>
                      <th scope="row">{label}</th>
                      <td>{count(side.count)}</td>
                      {[3, 2, 1, 0].map((stars) => (
                        <td
                          key={stars}
                          data-stars={stars}
                          data-empty={side.stars[stars] ? undefined : ""}
                        >
                          {count(side.stars[stars] ?? null)}
                        </td>
                      ))}
                    </tr>
                    <tr
                      className="season-summary-key"
                      data-side={label}
                      aria-hidden="true"
                    >
                      <td colSpan={6}>
                        {[3, 2, 1, 0].map((stars) => (
                          <span key={stars} data-stars={stars}>
                            <span className="summary-key-stars">
                              {stars}
                              <span className="summary-star">★</span>
                            </span>
                            <b>{count(side.stars[stars] ?? null)}</b>
                          </span>
                        ))}
                      </td>
                    </tr>
                  </Fragment>
                ))}
              </tbody>
            </table>
            {/* A phone folds the count under each bar; the bars stay. */}
            <details className="season-summary-stars">
              <summary>
                <span className="season-summary-stars-show">Star counts</span>
                <span className="season-summary-stars-hide">Hide star counts</span>
              </summary>
            </details>
          </div>
          {unknown.length > 0 ? (
            <p className="section-note">Stars unknown for {unknown.join(" and ")}.</p>
          ) : null}
          {/* Each side's two averages together, so they sit under its bar. */}
          <dl className="season-summary-averages">
            {(
              [
                [
                  "Offense per day",
                  signed(attack.perDay, 1),
                  "Averaged over the finished Legend days with at least one recorded attack",
                ],
                ["Per attack", signed(attack.perBattle, 1, 1)],
                [
                  "Defense per day",
                  signed(defense.perDay, -1),
                  "Averaged over the finished Legend days with at least one recorded defense",
                ],
                ["Per defense", signed(defense.perBattle, -1, 1)],
              ] as const
            ).map(([label, value, hint]) => (
              <div key={label}>
                <dt title={hint}>{label}</dt>
                <dd>{value}</dd>
              </div>
            ))}
          </dl>
        </>
      ) : null}
      {children}
    </section>
  );
}

const DAY = 86_400_000;
const shortDate = (time: number) =>
  new Date(time)
    .toLocaleDateString("en-GB", { day: "numeric", month: "short", timeZone: "UTC" })
    .replace("Sept", "Sep");

// A cell's change, a rare four-digit one in thousands so it fits: −1.3k.
const cellText = (value: number) =>
  Math.abs(value) < 1000
    ? signed(Math.abs(value), value < 0 ? -1 : 1)
    : `${value < 0 ? "−" : "+"}${(Math.abs(value) / 1000).toFixed(1)}k`;

/**
 * Each Season day's trophy change as one cell, coloured against the player's
 * own finished days as the dashboard's heat map is: best green, worst red.
 * Today and the days still to come are outlines. A week is a row on a phone
 * and a run of the strip on a wider screen, each under its first day's date.
 * Pointing at or focusing a day reads it out; the arrow keys move between
 * days. Nothing shows before the first Reset.
 */
export function SeasonHeatStrip({
  history,
  seasonStart,
  today,
}: {
  history: Array<{ day: RankedDaySummary }>;
  seasonStart: number;
  today: number;
}) {
  // The day read out above the cells: today until another is pointed at.
  const [shown, setShown] = useState(today);
  const list = useRef<HTMLOListElement>(null);
  if (today < 2) return null;
  const days = history.map(({ day }): SeasonDay => {
    const loss = day.defense.trophyLoss;
    return {
      day: Math.round((Date.parse(day.period.split(" – ")[0]) - seasonStart) / DAY) + 1,
      attacks: day.offense.attacks,
      defenses: day.defense.defenses,
      offense: day.offense.trophyGain,
      defense: loss === null ? null : -loss,
      net: day.trophyChange,
      triples: null,
      held: null,
      endTrophies: null,
      rank: null,
      hits: null,
    };
  });
  const cells = heatCells({ today, days }, "net");
  const lost = cells.some((cell) => cell.kind === "played" && (cell.value ?? 0) < 0);
  const result = (cell: (typeof cells)[number]) =>
    cell.kind === "today"
      ? "today, not finished yet"
      : cell.kind === "future"
        ? "not played yet"
        : cell.kind === "missing"
          ? "result unknown"
          : cell.kind === "shield"
            ? "no battles"
            : signed(Math.abs(cell.value ?? 0), (cell.value ?? 0) < 0 ? -1 : 1);
  const current = cells.find((cell) => cell.day === shown) ?? cells[0];
  const move = (event: React.KeyboardEvent<HTMLOListElement>) => {
    const steps: Record<string, number> = {
      ArrowLeft: -1,
      ArrowRight: 1,
      ArrowUp: -7,
      ArrowDown: 7,
    };
    const step = steps[event.key];
    const next =
      event.key === "Home"
        ? 1
        : event.key === "End"
          ? cells.length
          : current.day + (step ?? 0);
    if (next === current.day || next < 1 || next > cells.length) return;
    event.preventDefault();
    list.current?.querySelectorAll("li")[next - 1]?.focus();
  };
  return (
    <div className="season-heat">
      <p className="season-heat-head" aria-hidden="true">
        <span>Season heat map</span>
        <span className="season-heat-readout">
          <b>Day {current.day}</b> {shortDate(seasonStart + (current.day - 1) * DAY)}
          <span
            className={
              current.kind === "played"
                ? (current.value ?? 0) < 0
                  ? "score-negative"
                  : "score-positive"
                : undefined
            }
          >
            {current.kind === "today" ? "in progress" : result(current)}
          </span>
        </span>
        <span className="season-heat-key">
          worst
          {[0, 1, 2, 3, 4].map((step) => (
            <i key={step} style={{ background: `var(--cl-heat-${step})` }} />
          ))}
          best
          {/* Relative colours can paint a small gain red; a lost day is marked. */}
          {lost ? (
            <span className="season-heat-lost">
              <b /> lost
            </span>
          ) : null}
        </span>
      </p>
      <div className="season-heat-grid">
        <span className="season-heat-weeks" aria-hidden="true">
          {[0, 1, 2, 3].map((week) => (
            <span key={week}>{shortDate(seasonStart + week * 7 * DAY)}</span>
          ))}
        </span>
        <ol
          ref={list}
          className="season-heat-days"
          aria-label="Each day's trophy change this Season, coloured against the player's own days: best green, worst red"
          onKeyDown={move}
        >
          {cells.map((cell) => {
            const step = cell.shade === null ? null : heatStep(cell.shade);
            return (
              <li
                key={cell.day}
                className={`season-heat-${cell.kind}${cell.kind === "played" && (cell.value ?? 0) < 0 ? " is-loss" : ""}`}
                style={
                  step === null
                    ? undefined
                    : ({
                        "--heat": `var(--cl-heat-${step})`,
                        "--heat-text": `var(--cl-heat-${step}-text, var(--cl-heat-text))`,
                      } as React.CSSProperties)
                }
                // One stop for Tab; the arrow keys move from there.
                tabIndex={cell.day === current.day ? 0 : -1}
                data-shown={cell.day === current.day ? "" : undefined}
                onFocus={() => setShown(cell.day)}
                onMouseEnter={() => setShown(cell.day)}
              >
                <span className="sr-only">
                  Day {cell.day}: {result(cell)}
                </span>
                {/* Each cell says its result, as the dashboard's do; today
                    and the days to come say their day, today over "now". */}
                <span aria-hidden="true">
                  {cell.kind === "today" || cell.kind === "future"
                    ? cell.day
                    : cell.kind === "missing"
                      ? "–"
                      : cell.kind === "played"
                        ? cellText(cell.value ?? 0)
                        : null}
                </span>
                {cell.kind === "today" ? <span aria-hidden="true">now</span> : null}
              </li>
            );
          })}
        </ol>
      </div>
    </div>
  );
}
