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
import { LegendClock } from "./LegendClock";

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
        {timeZone}
      </p>
    </>
  );
}

function topLabel(rank: number): string {
  return `Top ${rank.toLocaleString("en-US")}`;
}

/** Card: the trophies a day needed to finish the Season at the goal rank. */
export function GoalCard({ data }: { data: GoalData }) {
  const [shield, setShield] = useState(1);
  const plan = goalPlan(data);
  const title = topLabel(data.rank);
  const head = <CardHead title="Goal" meta="from last Reset" />;
  if (data.finished === 0) {
    return (
      <>
        {head}
        <b className="goal-title">{title}</b>
        <p className="goal-need">
          <b className="form-big">—</b> <span className="dash-muted">a day needed</span>
        </p>
        <dl className="goal-rows">
          <div>
            <dt>Days left</dt>
            <dd>28</dd>
          </div>
        </dl>
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
  const picked = plan.shields.find((item) => item.days === shield) ?? plan.shields.at(-1);
  return (
    <>
      {head}
      <b className="goal-title">{title}</b>
      <p className="goal-need">
        <span>
          <b className="form-big">{signedOr(Math.max(0, plan.need))}</b>{" "}
          <span className="dash-muted">a day needed</span>
        </span>
        <span className={`goal-pace goal-pace-${plan.status}`}>
          {plan.status === "ahead"
            ? "✓ Already ahead"
            : plan.status === "on"
              ? "✓ On pace"
              : `Behind pace by ${Math.round(plan.need - plan.pace)} a day`}
        </span>
      </p>
      <dl className="goal-rows">
        <div>
          <dt>Gap at last Reset</dt>
          <dd>{signedOr(plan.gap)}</dd>
        </div>
        <div>
          <dt>Days left</dt>
          <dd>{plan.daysLeft}</dd>
        </div>
        <div>
          <dt>
            {title} line moving ({days})
          </dt>
          <dd>{signedOr(plan.drift)} a day</dd>
        </div>
        <div>
          <dt>Your pace ({days})</dt>
          <dd>{signedOr(plan.pace)} a day</dd>
        </div>
        {picked ? (
          <div>
            <dt className="goal-shield">
              Shield days{" "}
              <Switch
                label="Shield days"
                value={String(picked.days)}
                onChange={(value) => setShield(Number(value))}
                options={plan.shields.map(
                  (item) => [String(item.days), String(item.days)] as const,
                )}
              />
            </dt>
            <dd>
              {signedOr(Math.max(0, picked.need))} ·{" "}
              <span className={picked.onPace ? "positive" : "negative"}>
                {picked.onPace
                  ? "still on pace"
                  : `behind by ${Math.round(picked.need - plan.pace)}`}
              </span>
            </dd>
          </div>
        ) : null}
      </dl>
      <GoalChart data={data} title={title} />
    </>
  );
}

/** You (solid) against the goal line (dashed) over the days the pace covers. */
function GoalChart({ data, title }: { data: GoalData; title: string }) {
  if (data.line === null || data.lineThen === null) return null;
  const width = 320;
  const height = 60;
  const right = 236;
  const steps = data.history.length - 1;
  const line = data.history.map(
    (_, index) =>
      (data.lineThen as number) +
      (((data.line as number) - (data.lineThen as number)) * index) / Math.max(1, steps),
  );
  const known = data.history.filter((value): value is number => value !== null);
  const low = Math.min(...known, ...line);
  const high = Math.max(...known, ...line);
  const x = (index: number) => 4 + (index * (right - 4)) / Math.max(1, steps);
  const y = (value: number) =>
    height - 8 - ((value - low) / (high - low || 1)) * (height - 16);
  const path = (values: (number | null)[]) =>
    values
      .map((value, index) => (value === null ? null : `${x(index)} ${y(value)}`))
      .filter(Boolean)
      .map((point, index) => `${index ? "L" : "M"}${point}`)
      .join(" ");
  const you = data.history.at(-1) ?? null;
  let lineLabel = y(data.line) + 4;
  let youLabel = you === null ? lineLabel + 13 : y(you) + 4;
  if (Math.abs(lineLabel - youLabel) < 13) {
    if (lineLabel <= youLabel) youLabel = lineLabel + 13;
    else lineLabel = youLabel + 13;
    if (Math.max(lineLabel, youLabel) > height) {
      lineLabel -= 13;
      youLabel -= 13;
    }
  }
  return (
    <svg
      className="goal-chart"
      viewBox={`0 0 ${width} ${height}`}
      role="img"
      aria-label={`${data.name} against the ${title} line`}
    >
      <path className="goal-line" d={path(line)} />
      <path className="goal-you" d={path(data.history)} />
      <text className="goal-label" x={right + 8} y={lineLabel}>
        {title}
      </text>
      <text className="goal-label goal-label-you" x={right + 8} y={youLabel}>
        {data.name.length > 12 ? `${data.name.slice(0, 11)}…` : data.name}
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

function RivalTable({ rows, live }: { rows: RivalRow[]; live: boolean }) {
  return (
    <table className="rival-table">
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
        {rows.map((row) => (
          <tr key={row.tag} className={row.you ? "rival-you" : undefined}>
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
            <td className={tone(row.offense)}>{signedOr(row.offense)}</td>
            <td className={tone(row.defense === null ? null : -Math.abs(row.defense))}>
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
        </span>
      </div>
      {shown && shown.rows.length ? (
        <RivalTable rows={shown.rows} live={view === "live"} />
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
              <th scope="col">Avg gain</th>
              <th scope="col">Hit rate</th>
              <th scope="col">Avg offense</th>
              <th scope="col">Avg defense</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((row, at) => (
              <tr key={row.tag} className={row.you ? "rival-you" : undefined}>
                <td>
                  <Name>{`${first + at + 1}. ${row.name}`}</Name>
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
