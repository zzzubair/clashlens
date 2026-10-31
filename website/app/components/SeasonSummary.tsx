import { Fragment, useState } from "react";

import type { RankedDaySummary } from "../lib/contracts";
import { heatCells, type HeatCell, type SeasonDay } from "../lib/dashboard-cards";
import { ShieldIcon, Star, SwordIcon } from "./GameIcons";
import { HeatFoot, HeatKey, SeasonHeatGrid, heatResult } from "./SeasonHeatGrid";

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
// How much of a side's known battles one star count is, as its bar's width.
const share = (stars: Array<number | null>, star: number) => {
  const known = stars.reduce<number>((sum, each) => sum + (each ?? 0), 0);
  return {
    "--share": known && stars[star] ? `${(100 * stars[star]) / known}%` : "0%",
  } as React.CSSProperties;
};

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
  heatMap,
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
  /** The Season heat map, beside the headline on a wide screen. */
  heatMap?: React.ReactNode;
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
      className="data-section season-summary open-board"
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
      {heatMap}
      {attack && defense ? (
        // The attack and defense sides with their averages, one block so a
        // wide screen can set it beside the headline. A phone keeps them
        // folded under the headline and heat map; wider screens always show
        // them (season-summary.css).
        <details className="season-summary-sides">
          <summary className="show-all">Stars and averages</summary>
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
                      {stars}-star
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
                  // The side and its total, then each star count over its own
                  // bar, as long as its share of the side's battles, so no
                  // count needs matching to a colour.
                  <Fragment key={label}>
                    <tr data-side={label}>
                      <th scope="row">
                        {label === "Attacks" ? <SwordIcon /> : <ShieldIcon />}
                        {label}
                      </th>
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
                        {/* Each count under its number of stars, one star
                            drawn: 3★ reads faster than three outlines. */}
                        {[3, 2, 1, 0].map((stars) => (
                          <span
                            key={stars}
                            data-stars={stars}
                            data-empty={side.stars[stars] ? undefined : ""}
                            style={share(side.stars, stars)}
                          >
                            {stars}
                            <Star won={stars > 0} />
                            <b>{count(side.stars[stars] ?? null)}</b>
                          </span>
                        ))}
                      </td>
                    </tr>
                  </Fragment>
                ))}
              </tbody>
            </table>
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
        </details>
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

/** Each saved day of the Season as the heat map reads it, by its Season day. */
export function heatDays(
  history: Array<{ day: RankedDaySummary }>,
  seasonStart: number,
): SeasonDay[] {
  return history.map(({ day }): SeasonDay => {
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
}

/** Whether a Season's heat map draws: not before its first Reset (Day 1). */
export const heatMapShown = (today: number) => today >= 2;

/**
 * The Season heat map in its one form (SeasonHeatGrid), each day's trophy
 * change against the player's own days. A picked or pointed day reads out
 * with its date over the key. A `still` map, such as a profile card's, is
 * the grid alone. Nothing shows before the first Reset.
 */
export function SeasonHeatMap({
  days,
  seasonStart,
  today,
  still = false,
}: {
  days: SeasonDay[];
  seasonStart: number;
  today: number;
  still?: boolean;
}) {
  const [picked, setPicked] = useState<number | null>(null);
  const [pointed, setPointed] = useState<number | null>(null);
  if (!heatMapShown(today)) return null;
  const cells = heatCells({ today, days }, "net");
  const label =
    "Each day's trophy change this Season, coloured against the player's own days: best green, worst red";
  if (still) return <SeasonHeatGrid cells={cells} label={label} loss />;
  const cell = cells.find((each) => each.day === (pointed ?? picked)) ?? null;
  const current = cells.find((each) => each.kind === "today") ?? null;
  const say = (each: HeatCell) =>
    `Day ${each.day} · ${shortDate(seasonStart + (each.day - 1) * DAY)} · ${each.kind === "today" ? "in progress" : each.kind === "played" ? `${heatResult(each)} trophies` : heatResult(each)}`;
  return (
    <div className="season-heat-map">
      <SeasonHeatGrid
        cells={cells}
        label={label}
        loss
        picked={picked}
        onPick={(day) => setPicked(picked === day ? null : day)}
        onPoint={setPointed}
      />
      <HeatFoot
        readout={cell ? say(cell) : null}
        fallback={current ? say(current) : null}
      >
        <HeatKey />
      </HeatFoot>
    </div>
  );
}
