import { type ReactNode, useRef, useState } from "react";
import { Link } from "react-router";

import type {
  ClockData,
  CutoffsData,
  GoalData,
  GroupWatchData,
  RivalRow,
  RivalsData,
} from "../lib/dashboard-cards";
import { around, goalPlan } from "../lib/dashboard-cards";
import { canonicalPlayerPath } from "../lib/player-tag";
import {
  CardHead,
  CardNote,
  Name,
  Switch,
  count,
  percent,
  signedOr,
  tone,
} from "./DashboardParts";
import { LegendClock, zoneName } from "./LegendClock";

/** Card: the Legend clock, in the device's time zone. */
export function ClockCard({
  data,
  nowMs,
  timeZone,
}: {
  data: ClockData;
  nowMs: number | null;
  timeZone: string;
}) {
  // The dial's times are the device's zone, named once in the header.
  return (
    <>
      <CardHead
        title="Legend clock"
        meta={<span title="Times on the dial">{zoneName(timeZone)}</span>}
      />
      <LegendClock battles={data.battles} nowMs={nowMs} timeZone={timeZone} />
    </>
  );
}

function topLabel(rank: number): string {
  return `Top ${rank.toLocaleString("en-US")}`;
}

/** Card: the trophies a day needed to finish the Season at the goal rank. */
export function GoalCard({ data }: { data: GoalData }) {
  const [shield, setShield] = useState(0);
  const plan = goalPlan(data);
  const title = topLabel(data.rank);
  // The plan's menu, once there is a plan, at the header's end.
  const head = (meta?: ReactNode) => (
    <CardHead
      title={
        <>
          {"Goal "}
          <small className="dash-muted">· {title}</small>
        </>
      }
      meta={meta}
    />
  );
  if (data.finished === 0) {
    return (
      <>
        {head()}
        <CardNote>Needs one full day. The plan starts after the first Reset.</CardNote>
      </>
    );
  }
  if (plan === null) {
    return (
      <>
        {head()}
        <CardNote>
          {data.line === null || data.lineThen === null
            ? `The ${title} line is not on a saved Reset board yet.`
            : "Your trophies at the last Reset are not known yet."}
        </CardNote>
      </>
    );
  }
  const days = `${data.window} day${data.window === 1 ? "" : "s"}`;
  // Days off picked (none at first) set the trophies a day needed and the verdict.
  const picked = plan.shields.find((item) => item.days === shield);
  const need = picked?.need ?? plan.need;
  const status = !picked
    ? plan.status
    : need <= 0 && plan.gap >= 0
      ? "ahead"
      : picked.onPace
        ? "on"
        : "behind";
  const rank = `#${data.rank.toLocaleString("en-US")}`;
  const skip = picked?.days ?? 0;
  return (
    <>
      {head(
        plan.shields.length > 0 ? (
          <SkipPlan
            days={skip}
            options={[0, ...plan.shields.map((item) => item.days)]}
            choose={setShield}
          />
        ) : null,
      )}
      {/* The verdict in its colour and the gap to the goal line at the last
          Reset, then the two paces as bars; the chart shows both. */}
      <p className={`goal-headline goal-pace-${status}`}>
        <b>
          {status === "ahead" ? (
            "Already ahead"
          ) : status === "on" ? (
            "On pace"
          ) : (
            <>
              Behind by {Math.round(need - plan.pace)}
              <span aria-hidden="true">/day</span>
              <span className="sr-only"> a day</span>
            </>
          )}
        </b>{" "}
        <span>
          <span aria-hidden="true">· </span>
          {plan.gap === 0
            ? `Level with ${rank}`
            : `${Math.abs(plan.gap).toLocaleString("en-US")} ${plan.gap > 0 ? "ahead of" : "short of"} ${rank}`}
          <small className="goal-when"> at last Reset</small>
        </span>
      </p>
      <PaceBars need={Math.max(0, need)} pace={plan.pace} />
      <p className="sr-only">
        Doing is over {days} · {plan.daysLeft} day{plan.daysLeft === 1 ? "" : "s"} left
      </p>
      {/* The line's own pace, which the chart's dashed line draws. */}
      <p className="sr-only">
        {title} line {signedOr(plan.drift)} a day over {days}
      </p>
      <GoalChart data={data} plan={plan} rest={skip} rank={rank} />
    </>
  );
}

/**
 * The days the Clasher plans to sit out under a shield, behind a small menu
 * in the header: "Plan: skip 0 days ▾", one line on what it means, then the
 * choices. Picking one closes it.
 */
function SkipPlan({
  days,
  options,
  choose,
}: {
  days: number;
  options: number[];
  choose: (days: number) => void;
}) {
  const menu = useRef<HTMLDetailsElement>(null);
  return (
    <details className="goal-plan" ref={menu}>
      <summary>
        Plan: skip <b>{days}</b> day{days === 1 ? "" : "s"}
        <span aria-hidden="true"> ▾</span>
      </summary>
      <div className="goal-plan-menu">
        <p>Days you&apos;ll sit out under a shield; the rest make up the need.</p>
        <Switch
          label="Days to skip"
          value={String(days)}
          onChange={(value) => {
            choose(Number(value));
            if (menu.current) menu.current.open = false;
          }}
          options={options.map((item) => [String(item), String(item)] as const)}
        />
      </div>
    </details>
  );
}

/**
 * The trophies a day needed and those you're doing, as two bars from zero
 * on one scale: the need in the goal line's ink, yours in ember.
 */
function PaceBars({ need, pace }: { need: number; pace: number }) {
  const low = Math.min(0, need, pace);
  const high = Math.max(1, need, pace);
  const at = (value: number) => (100 * (value - low)) / (high - low);
  const bar = (value: number) => ({
    left: `${Math.min(at(0), at(value))}%`,
    width: `${Math.abs(at(value) - at(0))}%`,
  });
  return (
    <div className="goal-pace">
      {(
        [
          ["Need", need, "goal-pace-need"],
          ["Doing", pace, "goal-pace-you"],
        ] as const
      ).map(([name, value, className]) => (
        <p key={name} className={className}>
          <span>{name}</span>
          <span className="goal-pace-track" aria-hidden="true">
            <i style={bar(value)} />
          </span>
          <b>{perDay(value)}</b>
        </p>
      ))}
    </div>
  );
}

/** "+44/day", said "+44 a day". */
function perDay(value: number) {
  return (
    <>
      {signedOr(value)}
      <span aria-hidden="true">/day</span>
      <span className="sr-only"> a day</span>
    </>
  );
}

/** Two round trophy counts inside low–high, as far apart as a round step allows. */
function gridValues(low: number, high: number): number[] {
  for (const step of [1000, 500, 250, 200, 100, 50, 25, 10, 5]) {
    const first = Math.ceil(low / step) * step;
    const last = Math.floor(high / step) * step;
    if (last > first) return [first, last];
  }
  return [];
}

/**
 * You (ember) against the goal line (ink) over the days the pace covers,
 * then both carried on to the Season's end, dashed, at their own paces: you
 * at yours, resting the days skipped last, and the line at its own. Every
 * point is a Reset board, so the dashed line splitting what happened from
 * the projection is the last Reset, named under it. Days skipped are a shaded
 * band at the end, where your line goes flat; each projected end is named
 * beside its ring, two round trophy counts on the left.
 */
function GoalChart({
  data,
  plan,
  rest,
  rank,
}: {
  data: GoalData;
  plan: NonNullable<ReturnType<typeof goalPlan>>;
  rest: number;
  rank: string;
}) {
  if (data.line === null || data.lineThen === null) return null;
  const { line, lineThen } = data;
  const width = 320;
  const height = 168;
  // The plot, with room on the left for the trophy counts, below for the
  // days and on the right for the ends' names, "#200 6,441" at about 7 a
  // character.
  const left = 40;
  const right = width - 40 - 7 * Math.max(3, rank.length);
  const top = 10;
  const bottom = height - 24;
  const steps = data.history.length - 1;
  const total = steps + plan.daysLeft;
  const you = data.history.at(-1) as number;
  const past = data.history.map(
    (_, index) => lineThen + ((line - lineThen) * index) / Math.max(1, steps),
  );
  const played = plan.daysLeft - rest;
  const youEnd = you + plan.pace * played;
  const lineEnd = line + plan.drift * plan.daysLeft;
  const known = data.history.filter((value): value is number => value !== null);
  const low = Math.min(...known, ...past, youEnd, lineEnd);
  const high = Math.max(...known, ...past, youEnd, lineEnd);
  const x = (day: number) => left + (day * (right - left)) / Math.max(1, total);
  const y = (value: number) =>
    bottom - ((value - low) / (high - low || 1)) * (bottom - top);
  const path = (points: [number, number | null][]) =>
    points
      .filter((point): point is [number, number] => point[1] !== null)
      .map(
        ([day, value], index) =>
          `${index ? "L" : "M"}${x(day).toFixed(1)} ${y(value).toFixed(1)}`,
      )
      .join(" ");
  const now = x(steps);
  const skipFrom = x(steps + played);
  const firstDay = data.finished - steps + 1;
  // Along the foot: "Last Reset" (about 67 wide) centred on its line and
  // "Season end" (about 74) under the ends, a word space apart at least;
  // when they'd come closer, Last Reset ends at its line and Season end
  // starts after it. The first day shows when it clears Last Reset.
  const apart = right - now >= 84;
  const resetLeft = apart ? now - 34 : now - 70;
  const ends = [
    { name: "You", value: youEnd, you: true },
    { name: rank, value: lineEnd, you: false },
  ].sort((a, b) => b.value - a.value);
  // Each name level with its ring, the two pushed apart when they'd touch.
  const [upper, lower] = ends.map((end) => y(end.value)) as [number, number];
  const spread = Math.max(0, 18 - (lower - upper)) / 2;
  const nameY = [upper - spread, lower + spread];
  return (
    <svg
      className="goal-chart"
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={`${data.name} against the ${rank} line from Day ${firstDay}, carried on to the Season's end: you ${Math.round(youEnd).toLocaleString("en-US")}, ${rank} ${Math.round(lineEnd).toLocaleString("en-US")}`}
    >
      {/* The days still to play, faintly shaded, from the last Reset; the
          days skipped, at their end, shaded more and named. */}
      <rect
        className="goal-future"
        x={now}
        y={top}
        width={right - now}
        height={bottom - top}
      />
      {rest > 0 ? (
        <>
          <rect
            className="goal-skip"
            x={skipFrom}
            y={top}
            width={right - skipFrom}
            height={bottom - top}
          />
          <text
            className="goal-axis goal-skip-label"
            x={(skipFrom + right) / 2}
            y={bottom - 6}
            textAnchor="middle"
          >
            Skip
          </text>
        </>
      ) : null}
      {gridValues(low, high).map((value) => (
        <g key={value}>
          <line className="goal-grid" x1={left} x2={right} y1={y(value)} y2={y(value)} />
          <text
            className="goal-axis goal-axis-trophies"
            x={left - 6}
            y={y(value) + 4}
            textAnchor="end"
          >
            {value.toLocaleString("en-US")}
          </text>
        </g>
      ))}
      <line className="goal-today" x1={now} x2={now} y1={top} y2={bottom + 4} />
      <path className="goal-line" d={path(past.map((value, index) => [index, value]))} />
      <path
        className="goal-line goal-guess"
        d={path([
          [steps, line],
          [total, lineEnd],
        ])}
      />
      <path
        className="goal-you"
        d={path(data.history.map((value, index) => [index, value]))}
      />
      <path
        className="goal-you goal-guess"
        d={path([
          [steps, you],
          [steps + played, youEnd],
          [total, youEnd],
        ])}
      />
      {/* Where each stood at the last Reset, then where each is projected to finish. */}
      <circle className="goal-end" cx={now} cy={y(line)} r="3" />
      <circle className="goal-end goal-end-you" cx={now} cy={y(you)} r="3.5" />
      <circle className="goal-end goal-end-guess" cx={right} cy={y(lineEnd)} r="3.5" />
      <circle
        className="goal-end goal-end-you goal-end-guess"
        cx={right}
        cy={y(youEnd)}
        r="4"
      />
      {ends.map((end, index) => (
        <text
          key={end.name}
          className={end.you ? "goal-name goal-name-you" : "goal-name"}
          x={right + 8}
          y={(nameY[index] as number) + 5}
        >
          {end.name} <tspan>{Math.round(end.value).toLocaleString("en-US")}</tspan>
        </text>
      ))}
      {resetLeft - left >= 50 ? (
        <text className="goal-axis" x={left} y={height - 6} textAnchor="start">
          Day {firstDay}
        </text>
      ) : null}
      <text
        className="goal-axis goal-axis-today"
        x={apart ? now : now - 3}
        y={height - 6}
        textAnchor={apart ? "middle" : "end"}
      >
        Last Reset
      </text>
      <text
        className="goal-axis"
        x={apart ? right : Math.max(now + 3, right - 33)}
        y={height - 6}
        textAnchor={apart ? "middle" : "start"}
      >
        Season end
      </text>
    </svg>
  );
}

/** Card: trophies needed for each picked Top line, with your account slotted in. */
export function CutoffsCard({ data }: { data: CutoffsData }) {
  const [view, setView] = useState<"live" | "reset">("live");
  const shown = view === "live" || data.reset === null ? data.live : data.reset;
  const rows = [
    ...shown.lines.map((line) => ({
      key: `top-${line.top}`,
      label: topLabel(line.top),
      trophies: line.trophies,
      you: false,
    })),
    { key: "you", label: data.name, trophies: shown.you, you: true },
  ].sort(
    (a, b) =>
      (b.trophies ?? Number.NEGATIVE_INFINITY) - (a.trophies ?? Number.NEGATIVE_INFINITY),
  );
  return (
    <>
      <CardHead
        title="Cutoffs"
        meta={
          <Switch
            label="View"
            value={view}
            onChange={setView}
            options={[
              ["live", "Live"],
              ["reset", "Last Reset"],
            ]}
          />
        }
      />
      {view === "reset" && data.reset === null ? (
        <CardNote>The first Reset board of the Season is not saved yet.</CardNote>
      ) : (
        <ol className="cutoff-rows">
          {rows.map((row) => {
            const gap =
              row.you || row.trophies === null || shown.you === null
                ? null
                : shown.you - row.trophies;
            return (
              <li key={row.key} className={row.you ? "cutoff-you" : undefined}>
                {row.you ? <Name>{row.label}</Name> : <span>{row.label}</span>}
                <b>{count(row.trophies)}</b>
                <span className={tone(gap)}>{gap === null ? "" : signedOr(gap)}</span>
              </li>
            );
          })}
        </ol>
      )}
    </>
  );
}

/** A phone shows this many rivals either side of you before Show all. */
const PHONE_RIVALS = 2;

/** Whether a phone hides a row until Show all: more than 2 places from you. */
function farFromYou(rows: RivalRow[], index: number): boolean {
  const you = rows.findIndex((row) => row.you);
  return you >= 0 && Math.abs(index - you) > PHONE_RIVALS;
}

function RivalTable({
  rows,
  live,
  allShown,
}: {
  rows: RivalRow[];
  live: boolean;
  allShown: boolean;
}) {
  return (
    <table className={allShown ? "rival-table rival-all" : "rival-table"}>
      <thead>
        <tr>
          <th scope="col">Rank</th>
          <th scope="col">Player</th>
          <th scope="col">Trophies</th>
          <th scope="col">{live ? "Today" : "A day"}</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row, index) => {
          const net = netOf(row);
          return (
            <tr
              key={row.tag}
              className={
                row.you ? "rival-you" : farFromYou(rows, index) ? "rival-far" : undefined
              }
            >
              <td>
                <b className="rank-mark">#{count(row.rank)}</b>
                {row.moved ? (
                  <span
                    className={`rival-moved ${row.moved > 0 ? "positive" : "negative"}`}
                  >
                    {row.moved > 0 ? "▲" : "▼"}
                    {count(Math.abs(row.moved))}
                  </span>
                ) : null}
              </td>
              <td>
                {/* The name opens the player, and its link covers the row. */}
                <Link className="rival-link" to={canonicalPlayerPath(row.tag)}>
                  <Name>{row.name}</Name>
                </Link>
              </td>
              <td>{count(row.trophies)}</td>
              <td className={tone(net)}>{signedOr(net)}</td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/** Offense and defense together: the day's net, or the Season's net a day. */
function netOf(row: RivalRow): number | null {
  if (row.offense === null && row.defense === null) return null;
  return Math.round((row.offense ?? 0) - Math.abs(row.defense ?? 0));
}

/** Card: the leaderboard just above and below you, live or at the last Reset. */
export function RivalsCard({ data }: { data: RivalsData }) {
  const [view, setView] = useState<"live" | "season">("live");
  // A phone shows you and 2 either side until asked for every rival.
  const [allShown, setAllShown] = useState(false);
  const shown = view === "live" ? data.live : data.season;
  return (
    <>
      {/* When the numbers start, said once: today's battles and places moved
          since Reset, or the board as it stood at the last Reset. */}
      <CardHead
        title={
          <>
            {"Rivals watch "}
            <small className="dash-muted">
              · {view === "live" ? "since Reset" : "at last Reset"}
            </small>
          </>
        }
        meta={
          <Switch
            label="View"
            value={view}
            onChange={setView}
            options={[
              ["live", "Live"],
              ["season", "Season"],
            ]}
          />
        }
      />
      {view === "season" && data.season ? (
        <p className="sr-only">Season averages over {data.season.days} days</p>
      ) : null}
      {shown && shown.rows.length ? (
        <>
          <RivalTable rows={shown.rows} live={view === "live"} allShown={allShown} />
          {shown.rows.some((_, index) => farFromYou(shown.rows, index)) ? (
            <button
              type="button"
              className="show-all opponents-more"
              aria-expanded={allShown}
              onClick={() => setAllShown(!allShown)}
            >
              {allShown ? "Show fewer" : `Show all ${shown.rows.length}`}
            </button>
          ) : null}
        </>
      ) : (
        <CardNote>
          {view === "live"
            ? "You are not on the live leaderboard right now."
            : "The board at the last Reset does not have you yet."}
        </CardNote>
      )}
    </>
  );
}

/** Card: how you compare with one of your groups. */
export function GroupWatchCard({
  data,
  onPick,
  saving,
}: {
  data: GroupWatchData;
  onPick: (groupId: string) => void;
  saving: boolean;
}) {
  const picker =
    data.groups.length > 1 ? (
      <select
        className="group-picker"
        aria-label="Watchlist"
        value={data.groupId ?? ""}
        disabled={saving}
        onChange={(event) => onPick(event.currentTarget.value)}
      >
        {data.groups.map((group) => (
          <option key={group.id} value={group.id}>
            {group.name} ({group.members})
          </option>
        ))}
      </select>
    ) : data.groups[0] ? (
      <span className="dash-muted">
        <Name>{data.groups[0].name}</Name>
      </span>
    ) : null;
  if (data.groupId === null) {
    return (
      <>
        <CardHead title="Watchlist" />
        <CardNote>
          Make a watchlist first, then this card compares you with its players.
        </CardNote>
        <Link
          className="button button-secondary dash-small-button"
          to="/account/watchlists"
        >
          Make a watchlist
        </Link>
      </>
    );
  }
  const index = data.rows.findIndex((row) => row.you);
  const shown = around(data.rows, index, 3, 3);
  const first = data.rows.indexOf(shown[0]!);
  return (
    <>
      <CardHead title="Watchlist" meta={picker} />
      <p className="dash-muted dash-card-sub">
        {data.window === "7" ? "Last 7 days" : `Whole Season · ${data.days} days`}
      </p>
      {index >= 0 ? (
        <table className="group-table">
          <thead>
            <tr>
              <th scope="col">Player</th>
              <th scope="col">Trophies</th>
              <th scope="col" aria-sort="descending">
                Avg gain <span aria-hidden="true">↓</span>
              </th>
              <th scope="col">Hit rate</th>
              <th scope="col">Avg offense</th>
              <th scope="col">Avg defense</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((row, at) => (
              <tr key={row.tag} className={row.you ? "rival-you" : undefined}>
                <td>
                  <Link className="rival-link" to={canonicalPlayerPath(row.tag)}>
                    <Name>{`${first + at + 1}. ${row.name}`}</Name>
                  </Link>
                </td>
                <td>{count(row.trophies)}</td>
                <td className={tone(row.gain)}>{signedOr(row.gain)}</td>
                <td>{percent(row.hitRate)}</td>
                <td className="positive">{signedOr(row.offense)}</td>
                <td className="negative">{signedOr(row.defense)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <CardNote>Your numbers could not be loaded.</CardNote>
      )}
      <Link className="dash-card-link" to={`/account/watchlists/${data.groupId}`}>
        Open full comparison →
      </Link>
    </>
  );
}
