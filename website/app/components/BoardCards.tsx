import { useState } from "react";
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
  updatedAgo,
} from "./DashboardParts";
import { DashboardIcon } from "./DashboardIcon";
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
  return (
    <>
      <CardHead title="Legend clock" meta={updatedAgo(data.observedAtMs, nowMs)} />
      <LegendClock battles={data.battles} nowMs={nowMs} timeZone={timeZone} />
      <p className="dash-card-foot">
        <span className="sr-only">Times in your zone: </span>
        {zoneName(timeZone)}
      </p>
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
  const head = <CardHead title="Goal" meta="from last Reset" />;
  if (data.finished === 0) {
    return (
      <>
        {head}
        <b className="goal-title">{title}</b>
        <p className="goal-need">
          <b className="form-big">—</b>{" "}
          <span className="dash-muted">a day needed · 28 days left</span>
        </p>
        <CardNote>Needs one full day. The plan starts after the first Reset.</CardNote>
      </>
    );
  }
  if (plan === null) {
    return (
      <>
        {head}
        <b className="goal-title">{title}</b>
        <CardNote>
          {data.line === null || data.lineThen === null
            ? `The ${title} line is not on a saved Reset board yet.`
            : "Your trophies at the last Reset are not known yet."}
        </CardNote>
      </>
    );
  }
  const days = `${data.window} day${data.window === 1 ? "" : "s"}`;
  // Shield days picked (none at first) set the trophies a day needed and the verdict.
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
  return (
    <>
      {head}
      {/* One headline: the verdict in its colour, then the gap to the goal
          line at the last Reset. */}
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
          <span className="sr-only"> at last Reset</span>
        </span>
      </p>
      <p className="goal-need">
        <b className="form-big">{signedOr(Math.max(0, need))}</b>{" "}
        <span className="dash-muted">
          a day needed · {plan.daysLeft} day{plan.daysLeft === 1 ? "" : "s"} left
        </span>
      </p>
      <GoalRace
        need={Math.max(0, need)}
        pace={plan.pace}
        days={days}
        on={status !== "behind"}
      />
      {plan.shields.length > 0 ? (
        <div className="goal-shield">
          {/* Days the Clasher will sit out under a shield; the rest make up the need. */}
          <span className="goal-rest" title="Days you sit out under a shield, no battles">
            Rest days
          </span>
          <Switch
            label="Rest days: days you sit out under a shield, no battles"
            value={String(picked?.days ?? 0)}
            onChange={(value) => setShield(Number(value))}
            options={[
              ["0", "None"],
              ...plan.shields.map(
                (item) => [String(item.days), String(item.days)] as const,
              ),
            ]}
          />
        </div>
      ) : null}
      {/* The line's own pace, which the chart's dashed line draws. */}
      <p className="sr-only">
        {title} line {signedOr(plan.drift)} a day over {days}
      </p>
      <GoalChart data={data} title={title} />
    </>
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

/** Your pace as a bar on a scale from 0 to twice the trophies a day needed,
 *  so the need's tick sits in the middle and a full half reads as on pace. */
function GoalRace({
  need,
  pace,
  days,
  on,
}: {
  need: number;
  pace: number;
  days: string;
  on: boolean;
}) {
  // Whole trophies, so the end reads as twice the need shown.
  const top = need > 0 ? 2 * Math.max(1, Math.round(need)) : Math.max(2 * pace, 1);
  const at = (value: number) => `${Math.min(1, Math.max(0, value) / top) * 100}%`;
  return (
    <div className="goal-race">
      <span>
        Your pace <b>{perDay(pace)}</b>
        <span className="sr-only"> over {days}</span>
      </span>
      <span
        className="goal-race-track"
        data-on={on || undefined}
        aria-hidden="true"
        style={{ "--pace": at(pace), "--need": at(need) } as React.CSSProperties}
      >
        <i />
      </span>
      {/* The scale's two ends; the tick between them is the need shown above. */}
      <span className="goal-race-scale" aria-hidden="true">
        {need > 0 ? <span>0</span> : null}
        <span>{signedOr(top)}</span>
      </span>
    </div>
  );
}

/** You (solid) against the goal line (dashed) over the days the pace covers,
 *  each line's end labelled with its name and trophies. */
function GoalChart({ data, title }: { data: GoalData; title: string }) {
  if (data.line === null || data.lineThen === null) return null;
  const width = 320;
  const height = 132;
  const left = 52;
  // Room on the right for the longer end label, "Top 200 6,264".
  const right = width - 60 - title.length * 7.5;
  const axis = height + 16;
  const steps = data.history.length - 1;
  const line = data.history.map(
    (_, index) =>
      (data.lineThen as number) +
      (((data.line as number) - (data.lineThen as number)) * index) / Math.max(1, steps),
  );
  const known = data.history.filter((value): value is number => value !== null);
  const low = Math.min(...known, ...line);
  const high = Math.max(...known, ...line);
  const x = (index: number) => left + (index * (right - left)) / Math.max(1, steps);
  const y = (value: number) =>
    height - 10 - ((value - low) / (high - low || 1)) * (height - 30);
  const path = (values: (number | null)[]) =>
    values
      .map((value, index) => (value === null ? null : `${x(index)} ${y(value)}`))
      .filter(Boolean)
      .map((point, index) => `${index ? "L" : "M"}${point}`)
      .join(" ");
  // Faint lines at up to three round trophy counts give the chart a scale.
  const step = [10, 20, 50, 100, 200, 500, 1000, 2000].find(
    (size) => Math.floor(high / size) - Math.ceil(low / size) < 3,
  );
  const grid =
    step === undefined || high === low
      ? []
      : Array.from(
          { length: Math.floor(high / step) - Math.ceil(low / step) + 1 },
          (_, index) => (Math.ceil(low / step) + index) * step,
        );
  const you = data.history.at(-1) ?? null;
  // Each end's label is its trophies, level with the dot.
  let lineLabel = y(data.line);
  let youLabel = you === null ? lineLabel + 20 : y(you);
  // Labels closer than a label apart are pushed apart, and back inside the chart.
  const gap = 24;
  if (Math.abs(lineLabel - youLabel) < gap) {
    const middle = (lineLabel + youLabel) / 2;
    const [upper, lower] = [middle - gap / 2, middle + gap / 2];
    const shift = Math.max(0, lower - (height - 10)) + Math.min(0, upper - 12);
    if (lineLabel <= youLabel) [lineLabel, youLabel] = [upper - shift, lower - shift];
    else [youLabel, lineLabel] = [upper - shift, lower - shift];
  }
  const endLabel = (at: number, value: number, you: boolean) => (
    <text
      className={you ? "goal-label goal-label-you" : "goal-label"}
      x={right + 10}
      y={at + 6}
    >
      <tspan className="goal-axis">{you ? "You" : title} </tspan>
      {value.toLocaleString("en-US")}
    </text>
  );
  return (
    <svg
      className="goal-chart"
      // Room under the axis words for their descenders.
      viewBox={`0 0 ${width} ${axis + 6}`}
      role="img"
      aria-label={`${data.name} against the ${title} line`}
    >
      {grid.map((value) => (
        <g key={value}>
          <path className="goal-grid" d={`M${left} ${y(value)}H${right}`} />
          <text className="goal-axis" x={left - 6} y={y(value) + 4} textAnchor="end">
            {value.toLocaleString("en-US")}
          </text>
        </g>
      ))}
      <path className="goal-line" d={path(line)} />
      <path className="goal-you" d={path(data.history)} />
      <circle className="goal-end" cx={right} cy={y(data.line)} r="3.5" />
      {you === null ? null : (
        <circle className="goal-end goal-end-you" cx={right} cy={y(you)} r="4" />
      )}
      {endLabel(lineLabel, data.line, false)}
      {you === null ? null : endLabel(youLabel, you, true)}
      {steps > 0 ? (
        <text className="goal-axis" x={left} y={axis - 2}>
          {steps === 1 ? "1 day earlier" : `${steps} days earlier`}
        </text>
      ) : null}
      {/* Not now: the end is the last Reset, so it can differ from live trophies. */}
      <text className="goal-axis" x={right} y={axis - 2} textAnchor="middle">
        at last Reset
      </text>
    </svg>
  );
}

/** Card: trophies needed for each picked Top line, with your account slotted in. */
export function CutoffsCard({
  data,
  nowMs,
}: {
  data: CutoffsData;
  nowMs: number | null;
}) {
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
        meta={view === "live" ? updatedAgo(data.live.observedAtMs, nowMs) : null}
      />
      <Switch
        label="View"
        value={view}
        onChange={setView}
        options={[
          ["live", "Live"],
          ["reset", "Last Reset"],
        ]}
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
          <th scope="col">Hit rate</th>
          <th scope="col">{live ? "Offense" : "Offense a day"}</th>
          <th scope="col">{live ? "Defense" : "Defense a day"}</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row, index) => (
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
            <td>{percent(row.hitRate)}</td>
            {/* The icons stand in for the column headings on a phone. */}
            <td className={tone(row.offense)}>
              <DashboardIcon name="swords" />
              {signedOr(row.offense)}
            </td>
            <td className={tone(row.defense === null ? null : -Math.abs(row.defense))}>
              <DashboardIcon name="shield" />
              {signedOr(row.defense)}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** Card: the leaderboard just above and below you, live or at the last Reset. */
export function RivalsCard({ data, nowMs }: { data: RivalsData; nowMs: number | null }) {
  const [view, setView] = useState<"live" | "season">("live");
  // A phone shows you and 2 either side until asked for every rival.
  const [allShown, setAllShown] = useState(false);
  const shown = view === "live" ? data.live : data.season;
  return (
    <>
      <CardHead
        title="Rivals watch"
        meta={view === "live" ? updatedAgo(data.live?.observedAtMs ?? null, nowMs) : null}
      />
      <div className="dash-card-tools-row">
        <Switch
          label="View"
          value={view}
          onChange={setView}
          options={[
            ["live", "Live"],
            ["season", "Season"],
          ]}
        />
        <span className="dash-muted">
          {view === "live"
            ? "Today so far"
            : `Board at the last Reset · Season averages${data.season ? ` · ${data.season.days} days` : ""}`}
          {/* Names the arrows beside each rank. */}
          {shown?.rows.some((row) => row.moved) ? (
            <span className="rival-moved-key">
              {" · "}
              <span aria-hidden="true">
                <b className="positive">▲</b>
                <b className="negative">▼</b>
              </span>{" "}
              places since Reset
            </span>
          ) : null}
        </span>
      </div>
      {shown && shown.rows.length ? (
        <>
          <RivalTable rows={shown.rows} live={view === "live"} allShown={allShown} />
          {shown.rows.some((_, index) => farFromYou(shown.rows, index)) ? (
            <button
              type="button"
              className="button button-secondary opponents-more"
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
