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
  return value > 0
    ? `+${formatCount(value)}`
    : value < 0
      ? `−${formatCount(-value)}`
      : formatCount(value);
}

export function valueTone(value: number | null): string {
  if (value === null || value === 0) return "score-neutral";
  return value > 0 ? "score-positive" : "score-negative";
}

/** The change a day's bar shows: its saved change, or today's battles so far. */
function barChange(day: RankedDaySummary, isCurrentDay: boolean): number | null {
  return isCurrentDay && day.trophyChange === null && day.battlesComplete
    ? presentDay(dayEvidence(day), true).battleNet
    : day.trophyChange;
}

/** How far the log bars' shared track reaches each side of zero, in trophies. */
interface BarScale {
  up: number;
  down: number;
}

/** What a day's bar draws: its attacks' gain, its defenses' loss and its net. */
interface DayBar {
  gain: number;
  loss: number;
  net: number | null;
}

function dayBar(day: RankedDaySummary, isCurrentDay: boolean): DayBar {
  return {
    gain: day.offense.trophyGain ?? 0,
    loss: day.defense.trophyLoss ?? 0,
    net: barChange(day, isCurrentDay),
  };
}

/**
 * The track runs from the Season's biggest defense loss to its biggest attack
 * gain, so every day's two halves compare on one scale. It is at least 50
 * wide, so a quiet Season's small days stay small.
 */
function barScale(bars: DayBar[]): BarScale {
  const up = Math.max(0, ...bars.map(({ gain, net }) => Math.max(gain, net ?? 0)));
  const down = Math.max(0, ...bars.map(({ loss, net }) => Math.max(loss, -(net ?? 0))));
  return { up: Math.max(up, 50 - down), down };
}

/**
 * A day's split bar from the track's zero: attacks right in green, defenses
 * left in red, and a tick where the net lands.
 */
function swing({ gain, loss, net }: DayBar, { up, down }: BarScale) {
  const span = up + down;
  return {
    "--zero": down / span,
    "--gain": gain / span,
    "--loss": loss / span,
    "--net": (down + (net ?? 0)) / span,
    "--tick": net === null ? "0px" : "3px",
  } as React.CSSProperties;
}

// Out of the day's 8, so a missing battle reads as a gap in the records.
function recordedCount(value: number | null): string {
  return value === null
    ? "Count unknown"
    : `${formatCount(value)}${value <= 8 ? " of 8" : ""} recorded`;
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

/**
 * The labels over a log's rows, for sighted readers; each row names its own
 * values. `show` keeps only the wide screen's columns or the phone's line.
 */
export function LogHead({
  show,
  scale,
}: {
  show?: "columns" | "phone";
  /** The bars' track, marked over their column by its zero and its ends. */
  scale?: BarScale;
}) {
  const zero = scale ? scale.down / (scale.up + scale.down) : 0;
  return (
    <div className="log-head" data-show={show} aria-hidden="true">
      <span>Date</span>
      <span>End of day</span>
      {scale ? (
        <span
          className="log-head-scale"
          style={{ "--zero": zero } as React.CSSProperties}
        >
          {/* The bars' key, in the words of the columns they draw. */}
          <span className="log-head-key">
            <i className="log-key-loss" />
            Defenses
            <i className="log-key-gain" />
            Attacks
            <i className="log-key-net" />
            Change
          </span>
          {/* An end label too close to the zero would sit on it. */}
          {zero >= 0.2 ? (
            <span className="log-head-low">{formatSigned(-scale.down)}</span>
          ) : null}
          <span className="log-head-zero">0</span>
          {zero <= 0.8 ? (
            <span className="log-head-high">{formatSigned(scale.up)}</span>
          ) : null}
        </span>
      ) : (
        <span />
      )}
      <span>Attacks</span>
      <span>Defenses</span>
      <span>Change</span>
      <span>Reset rank</span>
      <span className="log-head-phone">
        <span />
        <span>Battles</span>
        <span>Change</span>
        <span>Rank</span>
      </span>
    </div>
  );
}

/** How many ended days a long log shows before its "Show all" fold. */
const LATEST = 7;

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
  gaps = [],
}: {
  history: Array<{ day: RankedDaySummary; seasonDay: string }>;
  player: PlayerPage;
  now: number;
  today: RankedDaySummary | null;
  openDay: string | null;
  /** Rows standing for days with no Legend I battles, each placed by its last day. */
  gaps?: Array<{ start: number; label: string; kind: "outside" | "waiting" }>;
}) {
  const todayKey = today ? legendDayKey(today.period) : null;
  const moves = rankMoves(history.map(({ day }) => day));
  // Every day's bar shares one scale, so the Season's days compare at a glance.
  const scale = barScale(
    history.map(({ day }) => dayBar(day, legendDayKey(day.period) === todayKey)),
  );
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
      scale={scale}
    />
  );
  const current = history.filter(({ day }) => legendDayKey(day.period) === todayKey);
  const ended = history.filter(({ day }) => legendDayKey(day.period) !== todayKey);
  // Newest first, as the days are.
  const rows = [
    ...ended.map((entry) => ({
      start: Date.parse(entry.day.period.split(" – ")[0]),
      entry,
    })),
    ...gaps.map((gap) => ({ start: gap.start, gap })),
  ].sort((a, b) => b.start - a.start);
  const row = (row: (typeof rows)[number]) =>
    "gap" in row && row.gap ? (
      <p key={`gap-${row.start}`} className={`log-gap log-gap-${row.gap.kind}`}>
        {row.gap.label}
      </p>
    ) : "entry" in row && row.entry ? (
      day(row.entry)
    ) : null;
  // A long log starts with its latest week; the rest sit one tap away, in a
  // fold that also opens without JavaScript. A linked day there opens it.
  let seen = 0;
  const fold =
    ended.length > LATEST + 3
      ? rows.findIndex((r) => "entry" in r && ++seen > LATEST)
      : -1;
  const older = fold < 0 ? [] : rows.slice(fold);
  const linked = older.some(
    (r) => "entry" in r && r.entry && legendDayKey(r.entry.day.period) === openDay,
  );
  return (
    <>
      {/* On a wide screen the columns are named over today's row too. */}
      {current.length > 0 ? (
        <>
          <LogHead show="columns" scale={scale} />
          <div className="log-card log-today">{current.map(day)}</div>
        </>
      ) : null}
      <div className="log-card log-days">
        {ended.length > 0 ? (
          <LogHead show={current.length > 0 ? "phone" : undefined} scale={scale} />
        ) : null}
        {(fold < 0 ? rows : rows.slice(0, fold)).map(row)}
        {older.length > 0 ? (
          <details className="log-older" open={linked || undefined}>
            <summary>
              <span className="log-older-show">Show all {ended.length} days</span>
              <span className="log-older-hide">Show the latest {LATEST} days</span>
            </summary>
            {older.map(row)}
          </details>
        ) : null}
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
  scale,
}: {
  day: RankedDaySummary;
  player: PlayerPage;
  now: number;
  seasonDay: string;
  isCurrentDay: boolean;
  openDay: string | null;
  rankMove?: number;
  scale: BarScale;
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
    // Phones show an ended day's start in its open row; wider rows have no room.
    day.startTrophies != null ? (
      <span key="start" className="log-fact-start">
        Started at <b>{day.startTrophies.toLocaleString("en-GB")}</b>
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
  // Where the start came from sits with the day's status, behind its mark.
  const source = day.startTrophiesSource ? (
    <p>
      <strong>Start: {day.startTrophiesSource}.</strong>{" "}
      {day.startTrophiesSource === "Season rule"
        ? "Every Legend I player starts a Season on 5,000."
        : day.startTrophiesCalculation
          ? `${day.startTrophiesCalculation.trophies.toLocaleString("en-GB")} − (${formatSigned(day.startTrophiesCalculation.netChange)}) = ${day.startTrophies?.toLocaleString("en-GB")}.`
          : "The previous day's calculated end. No reading from the game has confirmed it yet."}
    </p>
  ) : null;
  return (
    <details
      className={isCurrentDay ? "log-day log-day-live" : "log-day"}
      id={`legend-day-${dayKey}`}
      open={openDay === dayKey}
    >
      <summary className="log-row" style={swing(dayBar(day, isCurrentDay), scale)}>
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
        {/* The day's start leads; its status stays one quiet mark beside it. */}
        <div className="log-open-head">
          {facts.length > 0 ? <p className="log-facts">{facts}</p> : null}
          <BattleZone day={day} />
          {isCurrentDay ? (
            reasons.length > 0 || source ? (
              // Today's why-not-final waits behind one line.
              <details className="day-status" data-status="Live">
                <summary>Final result confirmed after Reset</summary>
                <div className="day-status-tip">
                  {reasons.length > 0 ? <p>{reasons.join(" ")}</p> : null}
                  {source}
                </div>
              </details>
            ) : null
          ) : (
            // An ended day's status, quiet. "Verified" says what a reading
            // confirmed: the day's result, which can stand while a battle's
            // own record is missing.
            <details className="day-status" data-status={status}>
              <summary>{status === "Verified" ? "Day result verified" : status}</summary>
              <div className="day-status-tip">
                <DayStatusNote status={status} reasons={reasons} />
                {source}
              </div>
            </details>
          )}
        </div>
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

const STAR_TEXT = ["☆☆☆", "★☆☆", "★★☆", "★★★"];

/**
 * Today's 8 attacks and 8 defenses as slots, as the dashboard's Legend day
 * card draws them: stars over the trophies each one moved, in the order
 * played, with dashed slots still to play.
 */
export function TodaySlots({ day }: { day: RankedDaySummary }) {
  const defense = day.defense.trophyLoss === null ? null : -day.defense.trophyLoss;
  const sides = [
    ["attack", "Attacks", day.offense.attacks, day.offense.trophyGain, day.offenseEvents],
    ["defense", "Defenses", day.defense.defenses, defense, day.defenseEvents],
  ] as const;
  return (
    <div className="player-today" role="group" aria-label="Today's battles">
      {/* Names the day the slots belong to, apart from the Season chart under them. */}
      <p className="today-day">
        {day.dayNumber === null ? "Legend day" : `Legend day ${day.dayNumber}`}
        <span> · today</span>
      </p>
      {sides.map(([side, title, saved, total, events]) => {
        const played = [...events].sort(
          (a, b) => Date.parse(a.battleTimestamp) - Date.parse(b.battleTimestamp),
        );
        const count = saved ?? played.length;
        return (
          <div key={side} className={`today-side today-side-${side}`}>
            <p className="today-head" aria-hidden="true">
              <span className="today-title">{title}</span> {count}/8
              {count > 0 ? (
                <b className={valueTone(total)}>{formatSigned(total)}</b>
              ) : null}
            </p>
            <ol className="today-slots" aria-label={`${count} of 8 ${side}s today`}>
              {Array.from({ length: 8 }, (_, index) => {
                const battle = played[index];
                return battle ? (
                  <li key={index} className="today-slot" data-stars={battle.stars}>
                    <span className="sr-only">
                      {battle.stars} stars, {formatSigned(battle.trophyChange)} trophies
                    </span>
                    <span className="today-stars" aria-hidden="true">
                      {STAR_TEXT[Math.max(0, Math.min(3, battle.stars))]}
                    </span>
                    <span aria-hidden="true">{formatSigned(battle.trophyChange)}</span>
                  </li>
                ) : (
                  <li
                    key={index}
                    className="today-slot today-slot-empty"
                    aria-hidden="true"
                  />
                );
              })}
            </ol>
          </div>
        );
      })}
    </div>
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
      {battles ? <BattleZone day={battles} /> : null}
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
      <div className="battle-side-head">
        <h3>
          {title} <strong className={valueTone(total)}>{formatSigned(total)}</strong>{" "}
          <small>{recordedCount(count)}</small>
        </h3>
      </div>
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
  // The whole line is one button that opens the army; their log is one more
  // tap, so a thumb on the name never leaves the page by accident.
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
            <BattleTime value={event.battleTimestamp} />
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

let clock: Intl.DateTimeFormat | null = null;

/** A battle's time of day: UTC as the server wrote it, then the viewer's own. */
function BattleTime({ value }: { value: string }) {
  const [local, setLocal] = useState<string | null>(null);
  useEffect(() => {
    const date = new Date(value);
    clock ??= new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit" });
    setLocal(Number.isNaN(date.getTime()) ? "Unknown" : clock.format(date));
  }, [value]);
  const utc = Number.isNaN(Date.parse(value))
    ? "Unknown"
    : new Date(value).toISOString().slice(11, 16);
  return (
    <time className="local-time" dateTime={value} title={`${utc} UTC`}>
      {local ?? utc}
    </time>
  );
}

/** An open day's time zone, named once so its battle rows show only the time. */
function BattleZone({ day }: { day: RankedDaySummary }) {
  const first = day.offenseEvents[0] ?? day.defenseEvents[0];
  return first ? (
    <small className="battle-zone">
      Times in <TimeZone at={first.battleTimestamp} />
    </small>
  ) : null;
}

/** The zone a list's times are in, such as GMT: UTC until the page starts. */
function TimeZone({ at }: { at: string }) {
  const [zone, setZone] = useState("UTC");
  useEffect(() => {
    const date = Number.isNaN(Date.parse(at)) ? new Date() : new Date(at);
    const name = new Intl.DateTimeFormat("en-GB", { timeZoneName: "short" })
      .formatToParts(date)
      .find((part) => part.type === "timeZoneName")?.value;
    if (name) setZone(name);
  }, [at]);
  return <>{zone}</>;
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
