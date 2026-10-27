import { useEffect } from "react";
import { useLocation, useSearchParams } from "react-router";

import type {
  HistoricalSeasonDayEntry,
  HistoricalSeasonSummary,
  WebsiteErrorResponse,
} from "../lib/contracts";
import { legendDayKey, presentDay } from "../lib/player-lookup-text";
import { seasonDates } from "../lib/season-end";
import { DayMark, DayStatusNote, provisional } from "./DayStatus";
import { ErrorNotice } from "./ErrorNotice";
import {
  LogDate,
  LogHead,
  formatCount,
  formatSigned,
  legendDayDate,
  validDay,
  valueTone,
} from "./LegendLog";
import { SeasonSummary, per, type SummarySide } from "./SeasonSummary";

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
    return (
      <>
        <SeasonFinish summary={summary} menu={menu} />
        {summary.source === "tracked_summary" ? <SeasonDays summary={summary} /> : null}
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
  menu,
}: {
  summary: HistoricalSeasonSummary;
  menu: React.ReactNode;
}) {
  const tracked = summary.source === "tracked_summary";
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
      {...(tracked &&
        summary.coverageState !== "complete" && {
          meta: `${summary.daysObserved} of 28 days recorded`,
        })}
      rank={["Final rank", finalRank(summary)]}
      finalRank
      trophies={[
        "Final trophies",
        formatCount(summary.officialHistory?.eodTrophies ?? summary.endTrophies),
      ]}
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
function SeasonDays({ summary }: { summary: HistoricalSeasonSummary }) {
  const selectedDay = validDay(useSearchParams()[0].get("day"));
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
        {summary.dailyEntries.map((day) => (
          <SeasonDay
            key={`${day.period}-${day.dayNumber ?? "unknown"}`}
            day={day}
            selected={legendDayKey(day.period) === selectedDay}
          />
        ))}
      </div>
    </section>
  );
}

function SeasonDay({
  day,
  selected,
}: {
  day: HistoricalSeasonDayEntry;
  selected: boolean;
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
        <span className="log-stat log-rank">
          <small>Reset rank</small>
          <strong className={day.resetRank == null ? "stat-unavailable" : undefined}>
            {formatCount(day.resetRank ?? null)}
          </strong>
        </span>
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
