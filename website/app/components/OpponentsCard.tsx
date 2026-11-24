import { CARDS } from "../lib/dashboard";
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
import { CardHead, CardNote, growFromButton, InfoTip } from "./DashboardParts";
import { DashboardIcon } from "./DashboardIcon";
import { ShieldIcon, Star, Stars, TrophyIcon } from "./GameIcons";
import { signedNumber as signed } from "../lib/number-format";
import { timeFormatter } from "../lib/clock-format";

/**
 * One earlier Legend day on Bases you attacked:
 *
 * - `today`: no earlier day is picked; the card's own data shows.
 * - `unread`: not read yet; it is read once clicking pauses for 0.4 seconds.
 * - `asking`: its read is on its way; a day has one read at a time.
 * - `read`: its bases are kept while the account's tab shows.
 * - `failed`: it could not be read; picking it again asks again.
 *
 * Events: picking a day, the pause ending, and the read's answer. The
 * dashboard owns the days, as its follow-up reads build the card again.
 */
export type BasesDayRead = "today" | "unread" | "asking" | "read" | "failed";

export function basesDayRead<Bases>(
  day: number | null,
  read: ReadonlyMap<number, Bases | null>,
  asking: ReadonlySet<number>,
): BasesDayRead {
  if (day === null) return "today";
  if (read.get(day)) return "read";
  if (asking.has(day)) return "asking";
  return read.has(day) ? "failed" : "unread";
}

const STRENGTH_LABELS: Record<BaseStrength, string> = {
  hard: "Hard",
  average: "Average",
  easy: "Easy",
  unavailable: "No rating",
  early: "Too early",
};
const STRENGTH_ORDER: BaseStrength[] = [
  "hard",
  "average",
  "easy",
  "unavailable",
  "early",
];
const ATTACKS = 8;
const PHONE_ROWS = 3;
const BASES_PAUSE_MS = 400;

export interface BasesResponse {
  bases: PastBases | null;
}

export function basesPath(tag: string, day: number): string {
  return `/resources/players/${encodeURIComponent(tag)}/bases?day=${day}`;
}

export function useBasesDay(tag: string) {
  const [chosen, setChosen] = useState<number | null>(null);
  const [read, setRead] = useState<Map<number, PastBases | null>>(new Map());
  const asking = useRef(new Set<number>());
  const pause = useRef<ReturnType<typeof setTimeout>>(undefined);
  const controllers = useRef(new Set<AbortController>());
  useEffect(
    () => () => {
      clearTimeout(pause.current);
      for (const controller of controllers.current) controller.abort();
      controllers.current.clear();
    },
    [],
  );
  return {
    chosen,
    read,
    choose(next: number | null) {
      setChosen(next);
      clearTimeout(pause.current);
      const state = basesDayRead(next, read, asking.current);
      if (next === null || state === "read" || state === "asking") return;
      setRead((old) => {
        const kept = new Map(old);
        kept.delete(next);
        return kept;
      });
      pause.current = setTimeout(() => {
        asking.current.add(next);
        const controller = new AbortController();
        controllers.current.add(controller);
        const timeout = setTimeout(() => controller.abort(), 15_000);
        // Not a router fetcher: its failed read would replace the whole page with an error.
        void fetch(basesPath(tag, next), {
          headers: { Accept: "application/json" },
          signal: controller.signal,
        })
          .then((response) => {
            if (!response.ok) throw new Error(String(response.status));
            return response.json() as Promise<BasesResponse>;
          })
          .then(({ bases }) => bases ?? null)
          .catch(() => null)
          .finally(() => {
            clearTimeout(timeout);
            controllers.current.delete(controller);
          })
          .then((bases) => {
            asking.current.delete(next);
            setRead((old) => new Map(old).set(next, bases));
          });
      }, BASES_PAUSE_MS);
    },
  };
}

export type BasesDay = ReturnType<typeof useBasesDay>;

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

function legendsLost({ defenses, legends }: OpponentRow): number | null {
  return legends?.defenses && legends.lost !== null
    ? Math.round((legends.lost * defenses.length) / legends.defenses)
    : null;
}

function trophiesLine(row: OpponentRow): string {
  const count = row.defenses.length;
  const average = legendsLost(row);
  const early = count < BASE_STRENGTH_MIN_DEFENSES;
  return [
    `lost ${signed(-trophiesGivenUp(row.defenses))} in ${count} defense${count === 1 ? "" : "s"}`,
    ...(average !== null ? [`Legend avg ${signed(-average)}`] : []),
    ...(!early && average === null ? ["no comparison yet"] : []),
  ].join(" · ");
}

function versus(diff: number, unit: string): string {
  if (diff === 0) return "same as Legend avg";
  return `${Math.abs(diff)}${unit} ${diff > 0 ? "above" : "below"} Legend avg`;
}

function compared(row: OpponentRow, mode: BasesMode): [number, number | null] {
  const { defenses, legends } = row;
  if (mode === "held")
    return [
      Math.round(heldShare(defenses) * 100),
      legends?.defenses ? Math.round((100 * legends.held) / legends.defenses) : null,
    ];
  return [
    defenses.length ? trophiesGivenUp(defenses) / defenses.length : 0,
    legends?.defenses && legends.lost !== null ? legends.lost / legends.defenses : null,
  ];
}

function strengthLine(
  row: OpponentRow,
  mode: BasesMode,
  strength: BaseStrength,
  earlier: boolean,
): string {
  const count = row.defenses.length;
  if (strength === "early")
    return earlier
      ? `only ${count} defense${count === 1 ? "" : "s"}`
      : `rated after ${BASE_STRENGTH_MIN_DEFENSES} defenses`;
  const [own, average] = compared(row, mode);
  const said = mode === "held" ? `held ${own}%` : `lost ${Math.round(own)} a defense`;
  if (strength === "unavailable" || average === null) return `${said}, no Legend avg yet`;
  return `${said}, ${versus(Math.round(own - average), mode === "held" ? " pts" : "")}`;
}

function shortResult(row: OpponentRow, mode: BasesMode, strength: BaseStrength) {
  const [own, average] = compared(row, mode);
  if (strength === "early" || strength === "unavailable" || average === null) return null;
  const diff = Math.round(own - average);
  return mode === "held" ? `${signed(diff)} pts` : signed(-diff);
}

function SlotsKey() {
  return (
    <p className="opponents-key" aria-hidden="true">
      <span>
        <i className="opponents-block is-tripled">
          <Star won />
        </i>
        Tripled
      </span>
      <span>
        <i className="opponents-block is-held">
          <ShieldIcon />
        </i>
        Held
      </span>
      <span>
        <i className="opponents-block" />
        Not yet
      </span>
      <span>
        <i className="opponents-block is-yours" />
        You
      </span>
    </p>
  );
}

const METER: Record<BaseStrength, number> = {
  hard: 3,
  average: 2,
  easy: 1,
  unavailable: 0,
  early: 0,
};

export function OpponentsCard({
  dayNumber,
  days,
  rows: todayRows,
  timeZone,
  mode = "held",
}: {
  dayNumber: number | null;
  days: BasesDay;
  rows: OpponentRow[] | null;
  timeZone: string;
  mode?: BasesMode;
}) {
  const navigate = useNavigate();
  const [allShown, setAllShown] = useState(false);
  const format = timeFormatter(timeZone);
  const { chosen, read } = days;
  const earlier = chosen !== null && dayNumber !== null && chosen < dayNumber;
  const day = earlier ? chosen : dayNumber;
  const past = earlier ? read.get(chosen) : undefined;
  const loading = earlier && !read.has(chosen);
  const rows = earlier ? (past?.rows ?? null) : todayRows;
  const shown = !loading && rows !== null && rows.length > 0;
  const head = (
    <CardHead
      icon={CARDS["bases"].icon}
      title={
        <>
          {"Bases you attacked "}
          <small className="dash-muted">
            {[
              ...(day === null ? ["today"] : []),
              ...(rows && !loading ? [`${rows.length} of ${ATTACKS}`] : []),
            ]
              .map((part) => `· ${part}`)
              .join(" ")}
          </small>
        </>
      }
      info={
        shown ? (
          <InfoTip label="What Strength means">
            <p>
              Strength compares{" "}
              {mode === "held"
                ? "the share of defenses this base held (not tripled)"
                : "the trophies this base lost a defense"}{" "}
              with Legends at similar trophies.
            </p>
            <p>
              + means harder than average:{" "}
              {mode === "held"
                ? "+42 pts held 42 points more often."
                : "+8 lost 8 fewer a defense."}
            </p>
          </InfoTip>
        ) : null
      }
      meta={
        <>
          {shown ? <span className="dash-card-ref">vs Legend avg</span> : null}
          {day !== null && dayNumber !== null ? (
            <DayStepper
              day={day}
              today={dayNumber}
              choose={(next) => days.choose(next === dayNumber ? null : next)}
            />
          ) : null}
        </>
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
        {earlier && past === null && day !== null ? (
          <button
            type="button"
            className="button button-secondary dash-small-button"
            onClick={() => days.choose(day)}
          >
            Try again
          </button>
        ) : null}
      </>
    );
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
  return (
    <>
      {head}
      <SlotsKey />
      <div className={allShown ? "opponents opponents-all" : "opponents"}>
        <div className="opponents-head" aria-hidden="true">
          <span>Player</span>
          <span>Your hit</span>
          <span>Their defenses</span>
          <span>
            Strength
            <small>
              {mode === "held" ? "held, pts vs avg" : "trophies a defense vs avg"}
            </small>
          </span>
        </div>
        {sorted.map(({ row, strength }, index) => {
          const held = row.defenses.filter((defense) => defense.stars < 3).length;
          const verdict = !["early", "unavailable"].includes(strength);
          const exact =
            mode === "held"
              ? `held ${held} of ${row.defenses.length}${
                  row.defenses.length < BASE_STRENGTH_MIN_DEFENSES
                    ? ""
                    : row.legends?.defenses
                      ? ` · ${Math.round(heldShare(row.defenses) * 100)}% vs Legend avg ${Math.round((100 * row.legends.held) / row.legends.defenses)}%`
                      : " · no comparison yet"
                }`
              : trophiesLine(row);
          const line = strengthLine(row, mode, strength, earlier);
          const result = shortResult(row, mode, strength);
          const open = () => void navigate(canonicalPlayerPath(row.tag));
          const name = row.name ?? row.tag;
          return (
            <div
              key={`${row.tag}-${index}`}
              className="opponents-row"
              role="link"
              tabIndex={0}
              aria-label={`${name}: open player page`}
              onClick={open}
              onKeyDown={(event) => {
                if (event.key === "Enter" && event.target === event.currentTarget) open();
              }}
            >
              <div className="opponents-name">
                <bdi className="dash-name" title={name}>
                  {name}
                </bdi>
                <span className="opponents-trophies" title="Trophies at Reset">
                  <TrophyIcon />
                  {row.resetTrophies === null
                    ? "–"
                    : row.resetTrophies.toLocaleString("en-US")}
                </span>
              </div>
              <div className="opponents-hit">
                <span>
                  <Stars count={row.hit.stars} /> {row.hit.destruction}%
                </span>{" "}
                <span>
                  <b className={row.hit.trophyChange > 0 ? "positive" : ""}>
                    {signed(row.hit.trophyChange)}
                  </b>
                  <span className="opponents-time"> · {format.format(row.hit.at)}</span>
                </span>
              </div>
              <ol
                className="opponents-blocks"
                aria-label={`held ${held} of ${row.defenses.length}`}
              >
                {Array.from(
                  { length: Math.max(ATTACKS, row.defenses.length) },
                  (_, at) => {
                    const defense = row.defenses[at];
                    if (!defense) return <li key={at} className="opponents-block" />;
                    const tripled = defense.stars === 3;
                    const said = `${defense.stars} star${defense.stars === 1 ? "" : "s"} ${tripled ? "tripled" : "held"}${defense.yours ? ", your attack" : ""}`;
                    return (
                      <li
                        key={at}
                        className={`opponents-block ${tripled ? "is-tripled" : "is-held"}${defense.yours ? " is-yours" : ""}`}
                        title={said}
                      >
                        {tripled ? <Star won /> : <ShieldIcon />}
                        <span className="sr-only">{said}</span>
                      </li>
                    );
                  },
                )}
              </ol>
              <details
                className="opponents-strength"
                name="opponents-exact"
                onClick={(event) => event.stopPropagation()}
              >
                <summary title={`${line} · ${exact}`} onClick={growFromButton}>
                  <span
                    className="opponents-meter"
                    data-bars={METER[strength]}
                    aria-hidden="true"
                  >
                    <i />
                    <i />
                    <i />
                  </span>
                  <b className={`opponents-verdict${verdict ? "" : " is-unrated"}`}>
                    {earlier && strength === "early"
                      ? "Too few"
                      : STRENGTH_LABELS[strength]}
                  </b>
                  {result ? (
                    <span className="opponents-result" aria-hidden="true">
                      {result}
                    </span>
                  ) : null}
                  <span className="sr-only">{line}</span>
                </summary>
                <p className="opponents-exact">{exact}</p>
              </details>
              <div className="opponents-action">
                <Link
                  className="opponents-watch"
                  to={addToGroupPath(row.tag)}
                  aria-label={`Add ${name} to watchlist`}
                  title="Add to watchlist"
                  onClick={(event) => event.stopPropagation()}
                >
                  <DashboardIcon name="eye" />
                  <i className="opponents-watch-plus" aria-hidden="true">
                    <DashboardIcon name="plus" />
                  </i>
                </Link>
              </div>
            </div>
          );
        })}
      </div>
      {sorted.length > PHONE_ROWS ? (
        <button
          type="button"
          className="show-all opponents-more"
          aria-expanded={allShown}
          onClick={() => setAllShown(!allShown)}
        >
          {allShown ? "Show fewer" : `Show all ${sorted.length}`}
        </button>
      ) : null}
    </>
  );
}
