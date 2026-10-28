import { useEffect } from "react";
import { useLocation, useSearchParams } from "react-router";

import type {
  HistoricalSeasonDayEntry,
  HistoricalSeasonSummary,
  WebsiteErrorResponse,
} from "../lib/contracts";
import { legendDayKey, presentDay } from "../lib/player-lookup-text";
import { seasonDates } from "../lib/season-end";
import { gapLabel, inGap, logGaps, pastSeasonShape } from "../lib/season-shape";
import { DayMark, DayStatusNote, provisional } from "./DayStatus";
import { ErrorNotice } from "./ErrorNotice";
import {
  LogDate,
  LogHead,
  LogRank,
  formatCount,
  formatSigned,
  legendDayDate,
  rankMoves,
  validDay,
  valueTone,
} from "./LegendLog";
import { SeasonStory } from "./SeasonStory";
import { SeasonSummary, per, type SummarySide } from "./SeasonSummary";

type PastShape = ReturnType<typeof pastSeasonShape>;

/** A chosen past Season: its finish, then its saved days in the log's short rows. */
export function SelectedSeason({
  seasonId,
  summary,
  error,
  menu,
}: {
  seasonId: string;
  summary: HistoricalSeasonSummary | null;
  error: WebsiteErrorResponse | null;
  menu: React.ReactNode;
}) {
  if (summary !== null) {
    const shape = summary.source === "tracked_summary" ? pastSeasonShape(summary) : null;
    return (
      <>
        <SeasonFinish summary={summary} shape={shape} menu={menu} />
        {shape ? <SeasonStory shape={shape} title="The Season in Legend I" /> : null}
        {shape ? <SeasonDays summary={summary} shape={shape} /> : null}
      </>
    );
  }
  return (
    <section className="data-section" aria-labelledby="historical-title">
      <div className="section-heading">
        <h2 id="historical-title">{seasonDates(seasonId)}</h2>
        {menu}
      </div>
      {error ? <ErrorNotice error={error} /> : null}
      <p className="section-note">Results for {seasonDates(seasonId)} are unavailable.</p>
    </section>
  );
}

function SeasonFinish({
  summary,
  shape,
  menu,
}: {
  summary: HistoricalSeasonSummary;
  shape: PastShape | null;
  menu: React.ReactNode;
}) {
  const tracked = summary.source === "tracked_summary";
  // A Season the player left or joined partway counts its Legend I days
  // only; one they left ends on the last Legend I total, with no final
  // placement (owner decisions, 28 October 2026).
  const partial = shape?.partial ? shape : null;
  const lastRun = partial?.runs.at(-1);
  const left = lastRun?.dropped ? lastRun : null;
  const { expected, saved } = partial?.legendDays ?? { expected: 0, saved: 0 };
  const side = (
    count: number | null,
    stars: Record<string, number | null>,
    unknown: number | null,
    trophies: number | null,
    played: "attacks" | "defenses",
  ): SummarySide => ({
    count,
    stars: [0, 1, 2, 3].map((star) => stars[star] ?? null),
    unknown,
    // Days without a battle on this side, such as shielded days, don't count.
    perDay: per(
      trophies,
      summary.daysObserved -
        summary.dailyEntries.filter((day) => day[played] === 0).length,
    ),
    perBattle: per(trophies, count),
  });
  return (
    <SeasonSummary
      title={seasonDates(summary.seasonId)}
      controls={menu}
      {...(partial
        ? {
            meta:
              saved < expected
                ? `${saved} of ${expected} Legend I days recorded`
                : `${expected} Legend I ${expected === 1 ? "day" : "days"} · all recorded`,
          }
        : tracked &&
          summary.coverageState !== "complete" && {
            meta: `${summary.daysObserved} of 28 days recorded`,
          })}
      rank={
        left
          ? ["Final rank", `Left Legend I after Day ${left.last}`]
          : ["Final rank", finalRank(summary)]
      }
      finalRank={!left}
      trophies={
        left
          ? [
              "Last Legend I trophies",
              formatCount(partial?.lastLegendDay?.endTrophies ?? null),
            ]
          : [
              "Final trophies",
              formatCount(summary.officialHistory?.eodTrophies ?? summary.endTrophies),
            ]
      }
      {...(tracked && {
        attack: side(
          summary.attackCount,
          summary.attackStars,
          summary.attackStarsUnknown,
          summary.attackGain,
          "attacks",
        ),
        defense: side(
          summary.defenseCount,
          summary.defenseStars,
          summary.defenseStarsUnknown,
          summary.defenseLoss,
          "defenses",
        ),
      })}
    />
  );
}

// The official in-game placement, never Clash Lens's own leaderboard position.
function finalRank(summary: HistoricalSeasonSummary): string {
  const placement = summary.officialHistory?.finalPlacement ?? null;
  return placement === null ? "Not published yet" : `#${formatCount(placement)}`;
}

// Each saved day as one short row, like the Daily Legend log; opening it shows
// its evidence and the rest of its numbers. A day link opens its day.
function SeasonDays({
  summary,
  shape,
}: {
  summary: HistoricalSeasonSummary;
  shape: PastShape;
}) {
  const selectedDay = validDay(useSearchParams()[0].get("day"));
  // Days outside Legend I, or before sign-up, as one row each stretch.
  const gaps = shape.partial ? logGaps(shape) : [];
  const rows = [
    ...summary.dailyEntries
      .filter((day) => !inGap(gaps, shape, day))
      .map((day) => ({ start: Date.parse(day.period.split(" – ")[0]), day })),
    ...gaps.map((gap) => ({ start: gap.lastStart, gap })),
  ].sort((a, b) => a.start - b.start);
  const moves = rankMoves(summary.dailyEntries);
  const { hash } = useLocation();
  // A day link opens at its row unless the link names a place on the page.
  useEffect(() => {
    if (!hash) document.getElementById(`legend-day-${selectedDay}`)?.scrollIntoView();
  }, [hash, selectedDay]);
  return (
    <section className="data-section" aria-label="Daily trophy totals">
      {selectedDay &&
      !summary.dailyEntries.some((day) => legendDayKey(day.period) === selectedDay) ? (
        <p className="section-note" role="status">
          No saved Legend log for {legendDayDate(selectedDay)}.
        </p>
      ) : null}
      <div className="log-card log-days">
        <LogHead />
        {rows.map((row) =>
          "gap" in row && row.gap ? (
            <p key={`gap-${row.start}`} className={`log-gap log-gap-${row.gap.kind}`}>
              {gapLabel(shape, row.gap)}
            </p>
          ) : "day" in row && row.day ? (
            <SeasonDay
              key={`${row.day.period}-${row.day.dayNumber ?? "unknown"}`}
              day={row.day}
              selected={legendDayKey(row.day.period) === selectedDay}
              rankMove={
                row.day.dayNumber === null ? undefined : moves.get(row.day.dayNumber)
              }
            />
          ) : null,
        )}
      </div>
    </section>
  );
}

function SeasonDay({
  day,
  selected,
  rankMove,
}: {
  day: HistoricalSeasonDayEntry;
  selected: boolean;
  rankMove?: number;
}) {
  const { status, reasons, battleNet } = presentDay(
    {
      net: day.netChange,
      state: day.state,
      confidence: day.confidence,
      coverage: day.coverage,
      codes: day.flags,
      attackGain: day.attackGain,
      defenseLoss: day.defenseLoss,
      attacks: day.attacks,
      defenses: day.defenses,
    },
    false,
  );
  const defense = day.defenseLoss === null ? null : -day.defenseLoss;
  return (
    <details
      className="log-day"
      {...(selected && {
        id: `legend-day-${legendDayKey(day.period)}`,
        "aria-current": "date",
        open: true,
      })}
    >
      <summary className="log-row">
        <span className="log-when">
          <LogDate period={day.period} />
          <span className="log-day-meta">
            <small>
              {day.dayNumber === null ? "Day unknown" : `Day ${day.dayNumber}`}
            </small>
            <DayMark status={status} />
          </span>
        </span>
        <span className="log-trophies">
          <span className="log-stat">
            <small>Starting trophies</small>
            <strong>{formatCount(day.startTrophies)}</strong>
          </span>
          <span className="log-stat log-end">
            <small>End of day</small>
            <strong>{provisional(formatCount(day.endTrophies), day.eodState)}</strong>
          </span>
        </span>
        <span className="log-stat log-net">
          <small>Trophy change</small>
          <strong className={valueTone(day.netChange)}>
            {formatSigned(day.netChange)}
          </strong>
        </span>
        <LogRank rank={day.resetRank ?? null} move={rankMove} />
        <span className="log-battles">
          <span className="log-stat">
            <small>Attacks</small>
            <strong className={valueTone(day.attackGain)}>
              {formatSigned(day.attackGain)}
            </strong>
          </span>
          <span className="log-stat">
            <small>Defenses</small>
            <strong className={valueTone(defense)}>{formatSigned(defense)}</strong>
          </span>
        </span>
      </summary>
      <div className="log-open">
        <DayStatusNote status={status} reasons={reasons} />
        <dl className="log-more">
          <div>
            <dt title="Recorded attacks minus recorded defenses, without the automatic defense loss at Reset.">
              Recorded battle net
            </dt>
            <dd>{formatSigned(battleNet)}</dd>
          </div>
          <div>
            <dt>EOD change from previous day</dt>
            <dd>{provisional(formatSigned(day.eodChange), day.eodChangeState)}</dd>
          </div>
          <div>
            <dt>Attacks recorded</dt>
            <dd>{formatCount(day.attacks)}</dd>
          </div>
          <div>
            <dt>Defenses recorded</dt>
            <dd>{formatCount(day.defenses)}</dd>
          </div>
          <div>
            <dt>Adjustment</dt>
            <dd>{formatAdjustment(day)}</dd>
          </div>
        </dl>
      </div>
    </details>
  );
}

function formatAdjustment(day: HistoricalSeasonDayEntry): string {
  if (!day.hasAdjustment) return "No";
  if (day.adjustmentTotal === null) return "Yes · unknown amount";
  return `Yes · ${formatSigned(day.adjustmentTotal)}`;
}
