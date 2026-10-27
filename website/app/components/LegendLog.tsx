import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router";

import type { ArmyUnitNames } from "../lib/army";
import type { PlayerPage, RankedBattleEvent, RankedDaySummary } from "../lib/contracts";
import { copyArmyLink } from "../lib/game-links";
import { dayEnd, dayEvidence, legendDayKey, presentDay } from "../lib/player-lookup-text";
import { canonicalPlayerPath } from "../lib/player-tag";
import { ArmyPortraits } from "./ArmyPortraits";
import { DayMark, DayStatusNote } from "./DayStatus";
import { FoldedBattles } from "./FoldedBattles";
import { linkedBattleId } from "./LinkedBattle";
import { LocalTimestamp } from "./Provenance";

const dateFormatter = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "short",
  year: "numeric",
  timeZone: "UTC",
});

export function validDay(day: string | null): string | null {
  return day && /^\d{4}-\d{2}-\d{2}$/.test(day) && !Number.isNaN(Date.parse(day))
    ? day
    : null;
}

/** A Legend day's date, such as "26 Oct 2026", from its period or its key. */
export function legendDayDate(period: string): string {
  const date = new Date(period.split(" – ")[0]);
  return Number.isNaN(date.getTime())
    ? "Date unavailable"
    : dateFormatter.format(date).replace("Sept", "Sep");
}

export function formatCount(value: number | null): string {
  return value === null ? "Unknown" : (value || 0).toLocaleString("en-GB");
}

export function formatSigned(value: number | null): string {
  if (value === null) return "Unknown";
  return value > 0 ? `+${formatCount(value)}` : formatCount(value);
}

export function valueTone(value: number | null): string {
  if (value === null || value === 0) return "score-neutral";
  return value > 0 ? "score-positive" : "score-negative";
}

// A day's attacks and defenses as shares of the most 8 battles can move.
function swing(attacks: number | null, defenses: number | null) {
  const share = (value: number | null) => Math.min(Math.abs(value ?? 0) / 320, 1);
  return { "--att": share(attacks), "--def": share(defenses) } as React.CSSProperties;
}

function recordedCount(value: number | null): string {
  return value === null ? "Count unknown" : `${formatCount(value)} recorded`;
}

/** The date in a log row: day and month shown, the year for screen readers. */
export function LogDate({ period }: { period: string }) {
  const label = legendDayDate(period);
  const year = label.lastIndexOf(" ");
  return year < 0 ? (
    <strong>{label}</strong>
  ) : (
    <strong>
      {label.slice(0, year)}
      <span className="sr-only">{label.slice(year)}</span>
    </strong>
  );
}

/** Places a day's Reset rank climbed since the day before's (a fall is negative). */
export function rankMoves(
  days: Array<{ dayNumber: number | null; resetRank?: number | null }>,
): Map<number, number> {
  const ranks = new Map(days.map((day) => [day.dayNumber, day.resetRank ?? null]));
  const moves = new Map<number, number>();
  for (const { dayNumber, resetRank } of days) {
    const before = dayNumber === null ? null : ranks.get(dayNumber - 1);
    if (dayNumber !== null && before != null && resetRank != null)
      moves.set(dayNumber, before - resetRank);
  }
  return moves;
}

/** A day's Reset rank on a rank plate, gold to bronze in the top 3, over its move. */
export function LogRank({ rank, move }: { rank: number | null; move?: number }) {
  const places = formatCount(Math.abs(move ?? 0));
  return (
    <span
      className="log-stat log-rank"
      data-podium-rank={rank !== null && rank <= 3 ? rank : undefined}
    >
      <small>Reset rank</small>
      <strong className={rank === null ? "stat-unavailable" : "rank-mark"}>
        {formatCount(rank)}
      </strong>
      {move ? (
        <span className={`log-rank-move ${valueTone(move)}`}>
          <span aria-hidden="true">
            {move > 0 ? "▲" : "▼"}
            {places}
          </span>
          <span className="sr-only">
            {`, ${move > 0 ? "up" : "down"} ${places} since the day before`}
          </span>
        </span>
      ) : null}
    </span>
  );
}

/** The labels over a log's rows, for sighted readers; each row names its own values. */
export function LogHead() {
  return (
    <div className="log-head" aria-hidden="true">
      <span>Date</span>
      <span>Day</span>
      <span>End of day</span>
      <span />
      <span>Attacks</span>
      <span>Defenses</span>
      <span>Change</span>
      <span>Reset rank</span>
    </div>
  );
}

/**
 * The current Season's Legend days: today's day first on its own card, then
 * every other day, each one short row that opens to its battles.
 */
export function LegendLog({
  history,
  player,
  now,
  today,
  openDay,
}: {
  history: Array<{ day: RankedDaySummary; seasonDay: string }>;
  player: PlayerPage;
  now: number;
  today: RankedDaySummary | null;
  openDay: string | null;
}) {
  const todayKey = today ? legendDayKey(today.period) : null;
  const moves = rankMoves(history.map(({ day }) => day));
  const day = ({ day, seasonDay }: (typeof history)[number]) => (
    <LegendDay
      key={legendDayKey(day.period)}
      day={day}
      player={player}
      now={now}
      seasonDay={seasonDay}
      isCurrentDay={legendDayKey(day.period) === todayKey}
      openDay={openDay}
      rankMove={day.dayNumber === null ? undefined : moves.get(day.dayNumber)}
    />
  );
  const current = history.filter(({ day }) => legendDayKey(day.period) === todayKey);
  const ended = history.filter(({ day }) => legendDayKey(day.period) !== todayKey);
  return (
    <>
      {current.length > 0 ? (
        <div className="log-card log-today">{current.map(day)}</div>
      ) : null}
      <div className="log-card log-days">
        {ended.length > 0 ? <LogHead /> : null}
        {ended.map(day)}
      </div>
    </>
  );
}

function LegendDay({
  day,
  player,
  now,
  seasonDay,
  isCurrentDay,
  openDay,
  rankMove,
}: {
  day: RankedDaySummary;
  player: PlayerPage;
  now: number;
  seasonDay: string;
  isCurrentDay: boolean;
  openDay: string | null;
  rankMove?: number;
}) {
  const dayKey = legendDayKey(day.period);
  const dayLabel = legendDayDate(day.period);
  const shown = presentDay(dayEvidence(day), isCurrentDay);
  const end = dayEnd(day, player, now);
  const { status, reasons } = isCurrentDay ? shown : end;
  const { battleNet } = shown;
  const defense = day.defense.trophyLoss === null ? null : -day.defense.trophyLoss;
  const soFar = isCurrentDay && day.trophyChange === null && day.battlesComplete;
  // Phones show one side of an open day at a time: Attacks, unless only
  // defenses are saved yet. A link to a defense opens on Defenses; the server
  // never sees the #battle- part, so it is read after hydration.
  const [side, setSide] = useState<"attack" | "defense">(() =>
    day.offenseEvents.length === 0 && day.defenseEvents.length > 0 ? "defense" : "attack",
  );
  const { hash } = useLocation();
  const defenseIds = day.defenseEvents.map((event) => event.battleId).join(" ");
  useEffect(() => {
    const linked = linkedBattleId(hash);
    if (linked && defenseIds.split(" ").includes(linked)) setSide("defense");
  }, [hash, defenseIds]);
  const facts = [
    day.startTrophiesSource ? (
      <span
        key="source"
        title={
          day.startTrophiesSource === "Season rule"
            ? "Every Legend I player starts a Season on 5,000"
            : day.startTrophiesCalculation
              ? undefined
              : "The previous day's calculated end. No reading from the game has confirmed it yet."
        }
      >
        Start: {day.startTrophiesSource}
      </span>
    ) : null,
    day.automaticDefenseLoss ? (
      <span key="automatic" title="Taken by the game at Reset for defenses not played">
        {formatSigned(-day.automaticDefenseLoss)} automatic loss
      </span>
    ) : null,
    !soFar && day.resetAdjustment ? (
      <span key="reset">
        {formatSigned(day.resetAdjustment.amount)} {day.resetAdjustment.kind} reset
      </span>
    ) : null,
  ].filter(Boolean);
  return (
    <details
      className={isCurrentDay ? "log-day log-day-live" : "log-day"}
      id={`legend-day-${dayKey}`}
      open={openDay === dayKey}
    >
      <summary className="log-row" style={swing(day.offense.trophyGain, defense)}>
        <span className="log-when">
          <LogDate period={day.period} />
          <span className="log-day-meta">
            <small>{seasonDay}</small>
            {isCurrentDay ? <LiveBadge /> : <DayMark status={status} />}
          </span>
        </span>
        <span className="log-trophies">
          <span className="log-stat">
            <small>Starting trophies</small>
            <strong
              className={day.startTrophies == null ? "stat-unavailable" : undefined}
              title={
                day.startTrophiesCalculation
                  ? `${day.startTrophiesCalculation.trophies.toLocaleString("en-GB")} − (${formatSigned(day.startTrophiesCalculation.netChange)}) = ${day.startTrophies?.toLocaleString("en-GB")}`
                  : undefined
              }
            >
              {day.startTrophies == null
                ? "Unavailable"
                : day.startTrophies.toLocaleString("en-GB")}
            </strong>
          </span>
          <span className="log-stat log-end">
            <small>End of day</small>
            <strong
              className={
                isCurrentDay || end.trophies === null ? "stat-unavailable" : undefined
              }
            >
              {isCurrentDay
                ? "After Reset"
                : end.trophies === null
                  ? "Unavailable"
                  : end.trophies.toLocaleString("en-GB")}
            </strong>
          </span>
        </span>
        <span className="log-stat log-net">
          <small>Trophy change</small>
          {soFar ? (
            <>
              <strong className={valueTone(battleNet)}>{formatSigned(battleNet)}</strong>
              <span>so far</span>
            </>
          ) : (
            <strong className={valueTone(day.trophyChange)}>
              {formatSigned(day.trophyChange)}
            </strong>
          )}
        </span>
        {isCurrentDay ? (
          <span className="log-stat log-rank">
            <small>Reset rank</small>
            <strong className="stat-unavailable">After Reset</strong>
          </span>
        ) : (
          <LogRank rank={day.resetRank ?? null} move={rankMove} />
        )}
        <span className="log-battles">
          <span className="log-stat">
            <small>Attacks</small>
            <strong className={valueTone(day.offense.trophyGain)}>
              {formatSigned(day.offense.trophyGain)}
            </strong>
          </span>
          <span className="log-stat">
            <small>Defenses</small>
            <strong className={valueTone(defense)}>{formatSigned(defense)}</strong>
          </span>
          {!soFar && day.trophyChange === null && battleNet !== null ? (
            <span>{formatSigned(battleNet)} from battles</span>
          ) : null}
        </span>
      </summary>
      <div className="log-open">
        {facts.length > 0 ? <p className="log-facts">{facts}</p> : null}
        {isCurrentDay ? (
          reasons.length > 0 ? (
            // Today's why-not-final waits behind one line.
            <details className="log-note">
              <summary>Final result confirmed after Reset</summary>
              <p>{reasons.join(" ")}</p>
            </details>
          ) : null
        ) : (
          <DayStatusNote status={status} reasons={reasons} />
        )}
        <fieldset className="battle-switch">
          <legend className="sr-only">Show</legend>
          {(
            [
              ["attack", "Attacks", day.offense.trophyGain],
              ["defense", "Defenses", defense],
            ] as const
          ).map(([value, title, total]) => (
            <label key={value}>
              <input
                type="radio"
                name={`side-${dayKey}`}
                value={value}
                checked={side === value}
                onChange={() => setSide(value)}
              />
              {title} <strong className={valueTone(total)}>{formatSigned(total)}</strong>
            </label>
          ))}
        </fieldset>
        <FoldedBattles open={openDay === dayKey} day={day}>
          <BattleList
            title="Attacks"
            total={day.offense.trophyGain}
            count={day.offense.attacks}
            events={day.offenseEvents}
            day={dayKey}
            dayLabel={dayLabel}
            units={player.armyUnits ?? {}}
            live={isCurrentDay}
          />
          <BattleList
            title="Defenses"
            total={defense}
            count={day.defense.defenses}
            events={day.defenseEvents}
            day={dayKey}
            dayLabel={dayLabel}
            units={player.armyUnits ?? {}}
            live={isCurrentDay}
          />
        </FoldedBattles>
      </div>
    </details>
  );
}

function LiveBadge() {
  return (
    <span className="log-live" aria-label="Today's Legend day, in progress">
      <span className="log-live-dot" aria-hidden="true" />
      In progress
    </span>
  );
}

// A linked day's Attacks and Defenses, or a note when the page no longer has them.
export function DayBattles({
  day,
  battles,
  units,
}: {
  day: string;
  battles: RankedDaySummary | undefined;
  units: ArmyUnitNames;
}) {
  return (
    <section className="data-section day-battles" aria-labelledby="day-battles-title">
      <h2 id="day-battles-title">Battles on {legendDayDate(day)}</h2>
      {battles ? (
        <div className="log-card battle-sides">
          {(["Attacks", "Defenses"] as const).map((title) => (
            <BattleList
              key={title}
              title={title}
              total={
                title === "Attacks"
                  ? battles.offense.trophyGain
                  : battles.defense.trophyLoss === null
                    ? null
                    : -battles.defense.trophyLoss
              }
              count={
                title === "Attacks" ? battles.offense.attacks : battles.defense.defenses
              }
              events={title === "Attacks" ? battles.offenseEvents : battles.defenseEvents}
              day={day}
              dayLabel={legendDayDate(day)}
              units={units}
            />
          ))}
        </div>
      ) : (
        <p className="section-note">
          Attacks and defenses from this day are no longer shown here.
        </p>
      )}
    </section>
  );
}

/**
 * One side of a day: its total, then one short row per battle. A day has 8
 * of each; the slots not recorded share one line. Each army, the opponent's
 * tag, their log and Copy army stay one tap away, so the result leads the row.
 */
function BattleList({
  title,
  total,
  count,
  events,
  day,
  dayLabel,
  units,
  live = false,
}: {
  title: "Attacks" | "Defenses";
  total: number | null;
  count: number | null;
  events: RankedBattleEvent[];
  day: string;
  dayLabel: string;
  units: ArmyUnitNames;
  /** Today's empty slots are not recorded yet, not missing. */
  live?: boolean;
}) {
  const [shown, setShown] = useState<ReadonlySet<string>>(new Set());
  const toggle = (battleId: string) =>
    setShown((current) => {
      const next = new Set(current);
      if (!next.delete(battleId)) next.add(battleId);
      return next;
    });
  // Show every saved battle; the game sometimes returns more than eight.
  const missing = 8 - events.length;
  const side = title === "Attacks" ? "attack" : "defense";
  return (
    <section className={`battle-side battle-side-${side}`} aria-label={title}>
      <h3>
        {title} <strong className={valueTone(total)}>{formatSigned(total)}</strong>{" "}
        <small>{recordedCount(count)}</small>
      </h3>
      <ol className="battle-list">
        {events.map((event) => (
          <BattleRow
            key={event.battleId}
            event={event}
            side={side}
            day={day}
            dayLabel={dayLabel}
            units={units}
            open={shown.has(event.battleId)}
            onToggle={() => toggle(event.battleId)}
          />
        ))}
        {missing > 0 ? (
          <li className="battle-missing">
            {missing} {missing === 1 ? side : `${side}s`}{" "}
            {live ? "not recorded yet" : "not recorded"}
          </li>
        ) : null}
      </ol>
    </section>
  );
}

function BattleRow({
  event,
  side,
  day,
  dayLabel,
  units,
  open,
  onToggle,
}: {
  event: RankedBattleEvent;
  side: "attack" | "defense";
  day: string;
  dayLabel: string;
  units: ArmyUnitNames;
  open: boolean;
  onToggle: () => void;
}) {
  const opponent = event.opponent.name ?? event.opponent.tag;
  const what = event.armyShareCode ? "army" : "opponent";
  // The whole line opens the army; their log is one more tap, so a thumb on
  // the name never leaves the page by accident.
  return (
    <li className={`battle-row battle-row-${side}`} id={`battle-${event.battleId}`}>
      <div className="battle-line">
        <span className="sr-only">
          {event.stars} stars, {event.destructionPercentage}% destruction,{" "}
          {formatSigned(event.trophyChange)} trophies
        </span>
        <span className="battle-score" aria-hidden="true">
          <Stars count={event.stars} />
          <small>{event.destructionPercentage}%</small>
        </span>
        <span className="battle-who">
          {/* A nameless opponent shows its tag once, as the name. */}
          <bdi className="battle-name">{opponent}</bdi>
          <small>
            <LocalTimestamp value={event.battleTimestamp} timeOnly />
            {event.perspectiveDisagreement ? (
              <span className="battle-disagreement">Result awaiting confirmation</span>
            ) : null}
          </small>
        </span>
        <strong
          className={`battle-change ${valueTone(event.trophyChange)}`}
          aria-hidden="true"
        >
          {formatSigned(event.trophyChange)}
        </strong>
        <button
          type="button"
          className="battle-toggle"
          aria-expanded={open}
          aria-controls={`more-${event.battleId}`}
          onClick={onToggle}
        >
          {event.armyShareCode ? (
            <span className="battle-toggle-word" aria-hidden="true">
              Army
            </span>
          ) : null}
          <svg viewBox="0 0 16 16" aria-hidden="true" focusable="false">
            <path d="M4 6l4 4 4-4" />
          </svg>
          <span className="sr-only">{open ? `Hide ${what}` : `Show ${what}`}</span>
        </button>
      </div>
      {/* Their log link is always in the page, so it is there before scripts run. */}
      <div className="battle-more" id={`more-${event.battleId}`} hidden={!open}>
        <p>
          {event.opponent.name ? (
            <span className="player-tag">{event.opponent.tag}</span>
          ) : null}
          <Link
            className="battle-link"
            to={`${canonicalPlayerPath(event.opponent.tag)}?day=${day}#battle-${encodeURIComponent(event.battleId)}`}
            aria-label={`View ${opponent}'s Legend log for ${dayLabel}`}
          >
            Their Legend log
          </Link>
          {open && event.armyShareCode ? (
            <a
              className="battle-copy"
              href={copyArmyLink(event.armyShareCode)}
              target="_blank"
              rel="noopener noreferrer"
              aria-label={`Copy army from the battle against ${opponent}, opens in a new tab`}
            >
              Copy army
            </a>
          ) : null}
        </p>
        {open && event.armyShareCode ? (
          <ArmyPortraits
            id={`army-${event.battleId}`}
            code={event.armyShareCode}
            names={units}
          />
        ) : null}
      </div>
    </li>
  );
}

// Three stars like the game's: gold when won, grey when not.
const STAR = "M6.5 .5 8 4.6l4.4.2L9 7.5l1.1 4.2-3.6-2.4-3.6 2.4L4 7.5.6 4.8 5 4.6Z";

function Stars({ count }: { count: number }) {
  return (
    <svg
      className="battle-stars"
      viewBox="0 0 41 13"
      aria-hidden="true"
      focusable="false"
    >
      {[0, 1, 2].map((star) => (
        <path
          key={star}
          d={STAR}
          transform={`translate(${star * 14} 0)`}
          className={star < count ? "star-won" : undefined}
        />
      ))}
    </svg>
  );
}
