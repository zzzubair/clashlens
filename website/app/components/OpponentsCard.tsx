import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router";

import type { BaseStrength, BasesMode, OpponentRow } from "../lib/dashboard";
import {
  BASE_STRENGTH_MIN_DEFENSES,
  baseStrength,
  trophiesGivenUp,
} from "../lib/dashboard";
import type { PastBases } from "../lib/dashboard-cards";
import { addToGroupPath, canonicalPlayerPath } from "../lib/player-tag";
import { CardHead, CardNote, updatedAgo } from "./DashboardParts";
import { signed, starText, timeFormatter } from "./LegendClock";
import { SaveAttacked, useNotebookOn } from "./NotebookSaveSheet";

const STRENGTH_LABELS: Record<BaseStrength, string> = {
  hard: "Hard",
  average: "Average",
  easy: "Easy",
  unavailable: "–",
  early: "Too early",
};
const STRENGTH_ORDER: BaseStrength[] = [
  "hard",
  "average",
  "easy",
  "unavailable",
  "early",
];
/** A Legend day has 8 attacks. */
const ATTACKS = 8;
/** An earlier day is read once clicking stops this long, not each day passed. */
const BASES_PAUSE_MS = 400;

export interface BasesResponse {
  bases: PastBases | null;
}

export function basesPath(tag: string, day: number): string {
  return `/resources/players/${encodeURIComponent(tag)}/bases?day=${day}`;
}

/**
 * The earlier Legend day chosen on Bases you attacked and each earlier day
 * read for `tag`, kept while its account tab is shown: null when it could not
 * be read. The dashboard holds them, as its follow-up reads build the card
 * again.
 */
export function useBasesDay(tag: string) {
  // The earlier day chosen; null for today, whichever day today is.
  const [chosen, setChosen] = useState<number | null>(null);
  const [read, setRead] = useState<Map<number, PastBases | null>>(new Map());
  // Days whose read is on its way: a day has one read at a time.
  const asking = useRef(new Set<number>());
  const pause = useRef<ReturnType<typeof setTimeout>>(undefined);
  useEffect(() => () => clearTimeout(pause.current), []);
  return {
    chosen,
    read,
    choose(next: number | null) {
      setChosen(next);
      clearTimeout(pause.current);
      if (next === null || read.get(next) || asking.current.has(next)) return;
      // A failed read is asked again.
      setRead((old) => {
        const kept = new Map(old);
        kept.delete(next);
        return kept;
      });
      pause.current = setTimeout(() => {
        asking.current.add(next);
        // Not a router fetcher: its failed read would replace the whole page with an error.
        void fetch(basesPath(tag, next), { headers: { Accept: "application/json" } })
          .then((response) => response.json() as Promise<BasesResponse>)
          .then(({ bases }) => bases ?? null)
          .catch(() => null)
          .then((bases) => {
            asking.current.delete(next);
            setRead((old) => new Map(old).set(next, bases));
          });
      }, BASES_PAUSE_MS);
    },
  };
}

export type BasesDay = ReturnType<typeof useBasesDay>;

/** ‹ Day 9 ›: steps through this Season's Legend days up to today. */
function DayStepper({
  day,
  today,
  choose,
}: {
  day: number;
  today: number;
  choose: (day: number) => void;
}) {
  return (
    <span className="dash-switch bases-day" role="group" aria-label="Legend day">
      <button
        type="button"
        aria-label="Earlier Legend day"
        disabled={day <= 1}
        onClick={() => choose(day - 1)}
      >
        ‹
      </button>
      <span className="bases-day-label" aria-live="polite">
        Day {day}
        {day === today ? <span className="dash-muted"> · today</span> : null}
      </span>
      <button
        type="button"
        aria-label="Later Legend day"
        disabled={day >= today}
        onClick={() => choose(day + 1)}
      >
        ›
      </button>
    </span>
  );
}

function heldShare(defenses: OpponentRow["defenses"]): number {
  if (defenses.length === 0) return 0;
  return defenses.filter((defense) => defense.stars < 3).length / defenses.length;
}

/**
 * Trophies mode's line: the trophies the base lost today, what Legends at
 * similar trophies lost over as many defenses, then the defense count both
 * cover, such as "lost −185 · avg −194 · in 6 defenses".
 */
function trophiesLine(row: OpponentRow): string {
  const count = row.defenses.length;
  const { legends } = row;
  const average =
    legends?.defenses && legends.lost !== null
      ? (legends.lost * count) / legends.defenses
      : null;
  const early = count < BASE_STRENGTH_MIN_DEFENSES;
  return [
    `lost ${signed(-trophiesGivenUp(row.defenses))}`,
    ...(average !== null ? [`avg ${signed(-Math.round(average))}`] : []),
    `in ${count} defense${count === 1 ? "" : "s"}`,
    ...(early
      ? [`needs ${BASE_STRENGTH_MIN_DEFENSES}+ defenses`]
      : average === null
        ? ["no comparison yet"]
        : []),
  ].join(" · ");
}

/**
 * Card 3: one row per attack today, hardest bases first, and with the day
 * button the same for an earlier Legend day of this Season. Held % mode compares
 * the base's held share today with today's among tracked Legend players at
 * similar trophies, and shows "–" when there are too few of them. Trophies
 * mode compares the trophies it gave up a defense with what those players
 * lost a defense, and shows the total it lost today beside what they lost
 * on average over as many defenses.
 * Colours follow the attacker: green is good for you, red is bad. With the
 * Notebook on, Save keeps a base in the Notebook of `tag`'s Clasher.
 */
export function OpponentsCard({
  tag,
  dayNumber,
  days,
  rows: todayRows,
  timeZone,
  mode = "held",
  nowMs = null,
  comparedAtMs = null,
}: {
  /** The dashboard's account, whose attacks these are. */
  tag?: string;
  /** Today's Legend day; null hides the day button. */
  dayNumber: number | null;
  days: BasesDay;
  rows: OpponentRow[] | null;
  timeZone: string;
  mode?: BasesMode;
  nowMs?: number | null;
  /** When the comparisons with Legends at similar trophies were read. */
  comparedAtMs?: number | null;
}) {
  const navigate = useNavigate();
  const notebookOn = useNotebookOn();
  const [saving, setSaving] = useState<OpponentRow | null>(null);
  const format = timeFormatter(timeZone);
  const { chosen, read } = days;
  const earlier = chosen !== null && dayNumber !== null && chosen < dayNumber;
  const day = earlier ? chosen : dayNumber;
  // Undefined while it loads.
  const past = earlier ? read.get(chosen) : undefined;
  const loading = earlier && !read.has(chosen);
  const rows = earlier ? (past?.rows ?? null) : todayRows;
  const when = earlier ? "that day" : "today";
  // The oldest opponent or comparison read, so the card never looks newer than it is.
  const observed = earlier ? [] : (rows ?? []).flatMap((row) => row.observedAtMs ?? []);
  if (!earlier && rows?.length && comparedAtMs !== null) observed.push(comparedAtMs);
  const updated = observed.length ? updatedAgo(Math.min(...observed), nowMs) : null;
  const head = (
    <CardHead
      title={
        <>
          {"Bases you attacked "}
          <small className="dash-muted">
            {/* Without the day button, the title says which day. */}
            {[
              ...(day === null ? ["today"] : []),
              ...(rows && !loading ? [`${rows.length} of ${ATTACKS}`] : []),
            ]
              .map((part) => `· ${part}`)
              .join(" ")}
          </small>
        </>
      }
      meta={
        day !== null && dayNumber !== null ? (
          <span className="bases-day-meta">
            {updated ? <span>{updated}</span> : null}
            <DayStepper
              day={day}
              today={dayNumber}
              choose={(next) => days.choose(next === dayNumber ? null : next)}
            />
          </span>
        ) : (
          updated
        )
      }
    />
  );
  const note = loading
    ? `Loading Day ${day}…`
    : earlier && past === null
      ? `Day ${day}'s bases couldn't be read. Try again in a minute.`
      : !rows
        ? earlier
          ? `Day ${day}'s battles aren't saved.`
          : "Today's battles aren't saved yet."
        : rows.length === 0
          ? earlier
            ? `No attacks on Day ${day}.`
            : "No attacks yet today."
          : null;
  if (note !== null || !rows)
    return (
      <>
        {head}
        <CardNote>{note}</CardNote>
      </>
    );
  // Hardest first within each strength: held most, or gave up least a defense.
  const hardness = (defenses: OpponentRow["defenses"]) =>
    mode === "held"
      ? heldShare(defenses)
      : defenses.length && -trophiesGivenUp(defenses) / defenses.length;
  const sorted = rows
    .map((row) => ({ row, strength: baseStrength(row.defenses, row.legends, mode) }))
    .sort(
      (a, b) =>
        STRENGTH_ORDER.indexOf(a.strength) - STRENGTH_ORDER.indexOf(b.strength) ||
        hardness(b.row.defenses) - hardness(a.row.defenses),
    );
  const canSave = notebookOn && tag !== undefined && day !== null;
  return (
    <>
      {head}
      {canSave ? (
        <SaveAttacked tag={tag} day={day} row={saving} onClose={() => setSaving(null)} />
      ) : null}
      <div className="opponents">
        <div className="opponents-head" aria-hidden="true">
          <span>Player · trophies at Reset</span>
          <span>Your hit</span>
          <span>Their defenses {when}</span>
          <span>Base strength · vs Legends at similar trophies {when}</span>
          <span />
        </div>
        {sorted.map(({ row, strength }, index) => {
          const held = row.defenses.filter((defense) => defense.stars < 3).length;
          const open = () => void navigate(canonicalPlayerPath(row.tag));
          return (
            <div
              key={`${row.tag}-${index}`}
              className="opponents-row"
              role="link"
              tabIndex={0}
              aria-label={`${row.name ?? row.tag}: open player page`}
              onClick={open}
              onKeyDown={(event) => {
                if (event.key === "Enter" && event.target === event.currentTarget) open();
              }}
            >
              <div className="opponents-name">
                <bdi className="dash-name" title={row.name ?? row.tag}>
                  {row.name ?? row.tag}
                </bdi>
                <span className="dash-muted">
                  {row.resetTrophies === null
                    ? "–"
                    : row.resetTrophies.toLocaleString("en-US")}
                </span>
              </div>
              <div>
                <span className="dash-stars">{starText(row.hit.stars)}</span>{" "}
                {row.hit.destruction}%{" "}
                <b className={row.hit.trophyChange > 0 ? "positive" : ""}>
                  {signed(row.hit.trophyChange)}
                </b>
                <span className="dash-muted"> · {format.format(row.hit.at)}</span>
              </div>
              <ol
                className="opponents-blocks"
                aria-label={`held ${held} of ${row.defenses.length}`}
              >
                {row.defenses.map((defense, at) => (
                  <li
                    key={at}
                    className={`opponents-block ${defense.stars === 3 ? "is-tripled" : "is-held"}${defense.yours ? " is-yours" : ""}`}
                    title={`${defense.stars === 3 ? "tripled" : "held"}${defense.yours ? " · your attack" : ""}`}
                  />
                ))}
              </ol>
              <div className="opponents-strength">
                <span className={`dash-strength dash-strength-${strength}`}>
                  {earlier && strength === "early"
                    ? "Too few"
                    : STRENGTH_LABELS[strength]}
                </span>
                <span className="dash-muted">
                  {mode === "held"
                    ? `held ${held} of ${row.defenses.length} · ${
                        row.defenses.length < BASE_STRENGTH_MIN_DEFENSES
                          ? `needs ${BASE_STRENGTH_MIN_DEFENSES}+ defenses`
                          : row.legends?.defenses
                            ? `${Math.round(heldShare(row.defenses) * 100)}% vs ${Math.round((100 * row.legends.held) / row.legends.defenses)}%`
                            : "no comparison yet"
                      }`
                    : trophiesLine(row)}
                </span>
              </div>
              <div className="opponents-action">
                {canSave ? (
                  <button
                    type="button"
                    className="button dash-small-button"
                    aria-label={`Save ${row.name ?? row.tag}'s base to your Notebook`}
                    onClick={(event) => {
                      event.stopPropagation();
                      setSaving(row);
                    }}
                  >
                    Save
                  </button>
                ) : null}
                <Link
                  className="button button-secondary dash-small-button"
                  to={addToGroupPath(row.tag)}
                  onClick={(event) => event.stopPropagation()}
                >
                  Add to watchlist
                </Link>
              </div>
            </div>
          );
        })}
      </div>
    </>
  );
}
