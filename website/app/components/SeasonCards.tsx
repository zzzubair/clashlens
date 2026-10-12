import { useState } from "react";

import type {
  HeatCell,
  HeatMode,
  SeasonData,
  SeasonDay,
  SeasonGraphData,
} from "../lib/dashboard-cards";
import {
  DAILY_BATTLES,
  SEASON_DAYS,
  heatCells,
  heatColour,
  heatValue,
  isShieldDay,
  offenseConsistency,
  recentForm,
} from "../lib/dashboard-cards";
import {
  CardHead,
  CardNote,
  Switch,
  count,
  lastDaysLabel,
  percent,
  signedOr,
  tone,
} from "./DashboardParts";
import { starText } from "./LegendClock";

const DAYS_OPTIONS = [
  ["3", "3 days"],
  ["7", "7 days"],
] as const;
type DaysOption = (typeof DAYS_OPTIONS)[number][0];

const FIRST_DAY_NOTE = "Results start after the Season's first Reset.";

/** Card: every day of the Season as a square, ranked against your own days. */
export function HeatMapCard({ data }: { data: SeasonData }) {
  const [mode, setMode] = useState<HeatMode>("net");
  const [picked, setPicked] = useState<number | null>(null);
  const cells = heatCells(data, mode);
  const byDay = new Map(data.days.map((day) => [day.day, day]));
  const pickedDay = picked === null ? null : (byDay.get(picked) ?? null);
  return (
    <>
      <CardHead
        title="Season heat map"
        meta={
          <Switch
            label="Show"
            value={mode}
            onChange={setMode}
            options={[
              ["net", "Net"],
              ["offense", "Offense"],
              ["defense", "Defense"],
            ]}
          />
        }
      />
      <ol className="heat-grid" aria-label={`Season days, ${mode}`}>
        {cells.map((cell) => (
          <HeatSquare
            key={cell.day}
            cell={cell}
            picked={picked === cell.day}
            onPick={() => setPicked(picked === cell.day ? null : cell.day)}
          />
        ))}
      </ol>
      <p className="dash-muted heat-tip" aria-live="polite">
        {pickedDay
          ? `Day ${pickedDay.day} · ended on ${count(pickedDay.endTrophies)}${isShieldDay(pickedDay) ? " · no battles" : ""}`
          : data.today !== null && data.today > 1
            ? "Tap a day for details"
            : FIRST_DAY_NOTE}
      </p>
      <dl className="heat-summary">
        {pickedDay ? (
          <DayNumbers day={pickedDay} />
        ) : (
          <SeasonNumbers data={data} mode={mode} />
        )}
      </dl>
    </>
  );
}

function HeatSquare({
  cell,
  picked,
  onPick,
}: {
  cell: HeatCell;
  picked: boolean;
  onPick: () => void;
}) {
  const label =
    cell.kind === "today"
      ? "today, not finished yet"
      : cell.kind === "future"
        ? "not played yet"
        : cell.kind === "missing"
          ? "no data"
          : cell.kind === "shield"
            ? "no battles"
            : signedOr(cell.value);
  if (cell.kind === "future" || cell.kind === "today" || cell.kind === "missing") {
    return (
      <li
        className={`heat-day heat-${cell.kind}`}
        aria-label={`Day ${cell.day}: ${label}`}
      >
        <small>{cell.day}</small>
        {cell.kind === "today" ? "…" : cell.kind === "missing" ? "–" : ""}
      </li>
    );
  }
  const style =
    cell.shade === null
      ? undefined
      : (() => {
          const [red, green, blue] = heatColour(cell.shade);
          return {
            background: `rgb(${red} ${green} ${blue})`,
            color: readableOn(red, green, blue),
          };
        })();
  return (
    <li className={`heat-day heat-${cell.kind}`} style={style}>
      <button
        type="button"
        aria-pressed={picked}
        aria-label={`Day ${cell.day}: ${label}`}
        onClick={onPick}
      >
        <small>{cell.day}</small>
        {signedOr(cell.value)}
      </button>
    </li>
  );
}

/** White or black, whichever reads better on the colour (at least 4.5 to 1). */
function readableOn(red: number, green: number, blue: number): string {
  const linear = (channel: number) => {
    const value = channel / 255;
    return value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  };
  const light = 0.2126 * linear(red) + 0.7152 * linear(green) + 0.0722 * linear(blue);
  return 1.05 / (light + 0.05) >= (light + 0.05) / 0.05 ? "#fff" : "#000";
}

function Pair({
  label,
  value,
  className,
}: {
  label: string;
  value: string;
  className?: string;
}) {
  return (
    <div>
      <dt>{label}</dt>
      <dd className={className}>{value}</dd>
    </div>
  );
}

function DayNumbers({ day }: { day: SeasonDay }) {
  return (
    <>
      <Pair label="Offense" value={signedOr(day.offense)} className="positive" />
      <Pair label="Attacks" value={count(day.attacks)} />
      <Pair label="Defense" value={signedOr(day.defense)} className="negative" />
      <Pair label="Defenses" value={count(day.defenses)} />
      <Pair label="Net" value={signedOr(day.net)} className={tone(day.net)} />
    </>
  );
}

function SeasonNumbers({ data, mode }: { data: SeasonData; mode: HeatMode }) {
  const played = data.days.filter(
    (day) => !isShieldDay(day) && heatValue(day, mode) !== null,
  );
  if (played.length === 0) return null;
  const value = (day: SeasonDay) => heatValue(day, mode) as number;
  const best = played.reduce((top, day) => (value(day) > value(top) ? day : top));
  const worst = played.reduce((low, day) => (value(day) < value(low) ? day : low));
  const at = (day: SeasonDay) => `${signedOr(value(day))} · D${day.day}`;
  if (mode === "net") {
    const nets = played.map(value);
    return (
      <>
        <Pair
          label="Up days"
          value={String(nets.filter((net) => net > 0).length)}
          className="positive"
        />
        <Pair label="Best" value={at(best)} className={tone(value(best))} />
        <Pair
          label="Down days"
          value={String(nets.filter((net) => net < 0).length)}
          className="negative"
        />
        <Pair label="Worst" value={at(worst)} className={tone(value(worst))} />
        <Pair label="No change" value={String(nets.filter((net) => net === 0).length)} />
      </>
    );
  }
  const side = mode === "offense" ? "positive" : "negative";
  const average = played.reduce((sum, day) => sum + value(day), 0) / played.length;
  const shields = data.days.filter(isShieldDay).length;
  return (
    <>
      <Pair label="Avg a day" value={signedOr(average)} className={side} />
      <Pair label="Best" value={at(best)} className={side} />
      <Pair label="Days played" value={String(played.length)} />
      <Pair label="Worst" value={at(worst)} className={side} />
      <Pair label="Shield days" value={String(shields)} />
    </>
  );
}

const GRAPH = { left: 56, right: 730, top: 12, bottom: 190, width: 760, height: 220 };

/** Card: trophies or rank at every Reset this Season. */
export function SeasonGraphCard({ data }: { data: SeasonGraphData }) {
  const [mode, setMode] = useState<"trophies" | "rank">("trophies");
  const [tip, setTip] = useState<number | null>(null);
  const value = (day: SeasonDay) => (mode === "trophies" ? day.endTrophies : day.rank);
  const points = data.days
    .filter((day) => value(day) !== null)
    .map((day) => ({ day: day.day, value: value(day) as number }));
  const ranked = data.days.filter((day) => day.rank !== null);
  const best = ranked.length
    ? ranked.reduce((top, day) =>
        (day.rank as number) < (top.rank as number) ? day : top,
      )
    : null;
  const last = data.lastSeason;
  return (
    <>
      <CardHead
        title="Season graph"
        meta={
          <Switch
            label="Show"
            value={mode}
            onChange={setMode}
            options={[
              ["trophies", "Trophies"],
              ["rank", "Rank"],
            ]}
          />
        }
      />
      {points.length === 0 ? (
        <CardNote>
          {data.today !== null && data.today > 1
            ? "No Reset results saved this Season yet."
            : FIRST_DAY_NOTE}
        </CardNote>
      ) : (
        <SeasonChart
          points={points}
          mode={mode}
          tip={tip}
          onTip={setTip}
          days={data.days}
        />
      )}
      <div className="graph-foot">
        <div>
          <span className="dash-label">Best rank this Season</span>
          <b>{best ? `#${count(best.rank)} · Day ${best.day}` : "–"}</b>
        </div>
        <div>
          <span className="dash-label">Last Season finish</span>
          <b>{last ? `#${count(last.rank)} · ${count(last.trophies)}` : "–"}</b>
        </div>
      </div>
    </>
  );
}

function SeasonChart({
  points,
  mode,
  tip,
  onTip,
  days,
}: {
  points: { day: number; value: number }[];
  mode: "trophies" | "rank";
  tip: number | null;
  onTip: (day: number | null) => void;
  days: SeasonDay[];
}) {
  const { left, right, top, bottom } = GRAPH;
  const values = points.map((point) => point.value);
  const low =
    mode === "trophies"
      ? Math.min(4950, ...values)
      : Math.max(1, Math.min(...values) * 0.6);
  const high =
    mode === "trophies" ? Math.max(...values) + 60 : Math.max(...values) * 1.25 + 1;
  const x = (day: number) => left + ((day - 1) * (right - left)) / (SEASON_DAYS - 1);
  // Trophies rise upwards; rank 1 sits at the top on a log scale.
  const y =
    mode === "trophies"
      ? (value: number) => bottom - ((value - low) / (high - low)) * (bottom - top)
      : (value: number) =>
          top +
          ((Math.log(value) - Math.log(low)) / (Math.log(high) - Math.log(low))) *
            (bottom - top);
  const ticks =
    mode === "trophies"
      ? Array.from({ length: 12 }, (_, index) => 5000 + index * 200).filter(
          (tick) => tick >= low && tick <= high,
        )
      : [1, 10, 50, 100, 250, 500, 1000, 2000, 5000, 10000].filter(
          (tick) => tick >= low && tick <= high,
        );
  let solid = "";
  let dotted = "";
  points.forEach((point, index) => {
    const previous = points[index - 1];
    const at = `${x(point.day).toFixed(1)} ${y(point.value).toFixed(1)}`;
    if (!previous) solid += `M${at}`;
    else if (point.day === previous.day + 1) solid += ` L${at}`;
    else {
      // A missing day between two saved ones is drawn dotted.
      dotted += `M${x(previous.day).toFixed(1)} ${y(previous.value).toFixed(1)} L${at} `;
      solid += ` M${at}`;
    }
  });
  const tipPoint = points.find((point) => point.day === tip);
  const tipDay = days.find((day) => day.day === tip);
  return (
    <div className="graph">
      <svg
        className="graph-svg"
        viewBox={`0 0 ${GRAPH.width} ${GRAPH.height}`}
        role="img"
        aria-label={`Your ${mode} at each Reset this Season`}
      >
        {ticks.map((tick) => (
          <g key={tick}>
            <line className="graph-grid" x1={left} x2={right} y1={y(tick)} y2={y(tick)} />
            <text className="graph-axis" x={left - 6} y={y(tick) + 4} textAnchor="end">
              {mode === "trophies" ? count(tick) : `#${count(tick)}`}
            </text>
          </g>
        ))}
        {[1, 7, 14, 21, 28].map((day) => (
          <text
            key={day}
            className="graph-axis"
            x={x(day)}
            y={bottom + 18}
            textAnchor="middle"
          >
            Day {day}
          </text>
        ))}
        <path className="graph-gap" d={dotted} />
        <path className="graph-line" d={solid} />
        {points.map((point) => (
          <circle
            key={point.day}
            className="graph-point"
            cx={x(point.day)}
            cy={y(point.value)}
            r={tip === point.day ? 7 : 5}
            tabIndex={0}
            role="button"
            aria-label={`Day ${point.day}: ${mode === "trophies" ? count(point.value) : `#${count(point.value)}`}`}
            onMouseEnter={() => onTip(point.day)}
            onMouseLeave={() => onTip(null)}
            onFocus={() => onTip(point.day)}
            onBlur={() => onTip(null)}
            onClick={() => onTip(point.day)}
          />
        ))}
      </svg>
      {tipPoint && tipDay ? (
        <span
          className="graph-tip"
          style={{
            left: `${(x(tipPoint.day) / GRAPH.width) * 100}%`,
            top: `${(y(tipPoint.value) / GRAPH.height) * 100}%`,
          }}
        >
          Day {tipDay.day} ·{" "}
          {mode === "trophies" ? count(tipDay.endTrophies) : `#${count(tipDay.rank)}`}
        </span>
      ) : null}
    </div>
  );
}

function Trend({
  now,
  before,
  size,
  digits = 1,
}: {
  now: number | null;
  before: number | null | undefined;
  size: number;
  digits?: number;
}) {
  if (before === undefined) return null;
  if (now === null || before === null) return <span className="form-trend" />;
  const better = now >= before;
  return (
    <span className={`form-trend ${better ? "positive" : "negative"}`}>
      {better ? "▲ better" : "▼ worse"} by {Math.abs(now - before).toFixed(digits)}
      <small>vs the {size} days before</small>
    </span>
  );
}

/** Card: your average day over the last 3 or 7 days, against the days before. */
export function RecentFormCard({ data }: { data: SeasonData }) {
  const [days, setDays] = useState<DaysOption>("7");
  const size = Number(days);
  const form = recentForm(data, size);
  const before = form?.before ?? undefined;
  return (
    <>
      <CardHead
        title="Recent form"
        meta={
          <Switch label="Days" value={days} onChange={setDays} options={DAYS_OPTIONS} />
        }
      />
      {form === null ? (
        <CardNote>
          {data.today !== null && data.today > 1
            ? `No results saved for the last ${size} days yet.`
            : FIRST_DAY_NOTE}
        </CardNote>
      ) : (
        <>
          <p className="dash-muted dash-card-sub">
            {lastDaysLabel(size, form.window.span)} · Days {form.window.first}–
            {form.window.last}
            {form.counted < form.window.span
              ? ` · ${form.counted} of ${form.window.span} recorded`
              : ""}
          </p>
          <div className="form-row">
            <div>
              <span className="dash-label">Trophies a day</span>
              <b className={`form-big ${tone(form.now.net)}`}>
                {signedOr(form.now.net, 1)}
              </b>
              <small className="dash-muted">{signedOr(form.total)} total</small>
            </div>
            <Trend now={form.now.net} before={before && before.net} size={size} />
          </div>
          <div className="form-row">
            <div>
              <span className="dash-label">Attack</span>
              <b className="form-big positive">{signedOr(form.now.perAttack, 1)}</b>
              <small className="dash-muted">
                per attack · <b>{percent(form.now.tripled)} tripled</b>
              </small>
            </div>
            <Trend
              now={form.now.perAttack}
              before={before && before.perAttack}
              size={size}
            />
          </div>
          <div className="form-row">
            <div>
              <span className="dash-label">Defense</span>
              <b className="form-big negative">{signedOr(form.now.perDefense, 1)}</b>
              <small className="dash-muted">
                per defense · <b>{percent(form.now.held)} held</b>
              </small>
            </div>
            <Trend
              now={form.now.perDefense}
              before={before && before.perDefense}
              size={size}
            />
          </div>
          {before === undefined ? (
            <p className="dash-card-foot">
              Compares with the {size} days before from Day {2 * size + 1}.
            </p>
          ) : null}
        </>
      )}
    </>
  );
}

const STAR_CLASSES = ["stars-3", "stars-2", "stars-1", "stars-0"] as const;

/** Card: how often you triple, and what the misses cost. */
export function OffenseConsistencyCard({ data }: { data: SeasonData }) {
  const [days, setDays] = useState<DaysOption>("7");
  const size = Number(days);
  const stats = offenseConsistency(data, size);
  return (
    <>
      <CardHead
        title="Offense consistency"
        meta={
          <Switch label="Days" value={days} onChange={setDays} options={DAYS_OPTIONS} />
        }
      />
      {stats === null ? (
        <CardNote>
          {data.today !== null && data.today > 1
            ? `No results saved for the last ${size} days yet.`
            : FIRST_DAY_NOTE}
        </CardNote>
      ) : (
        <>
          <p className="dash-muted dash-card-sub">
            {lastDaysLabel(size, stats.window.span)} · {stats.attacks} attacks
          </p>
          <div className="consistency-rate">
            <b className="form-big">{percent(stats.triples / stats.slots)}</b>
            <span className="dash-muted">
              hit rate · {stats.triples} of {stats.slots} tripled
            </span>
          </div>
          <div className="consistency-bar" aria-hidden="true">
            {stats.stars.map((amount, index) =>
              amount ? (
                <i
                  key={index}
                  className={STAR_CLASSES[index]}
                  style={{ width: `${(amount / Math.max(1, stats.attacks)) * 100}%` }}
                />
              ) : null,
            )}
          </div>
          <ul className="consistency-split">
            {stats.stars.map((amount, index) => (
              <li key={index} className={STAR_CLASSES[index]}>
                <b>{amount}</b>
                {index === 3 ? "0★" : starText(3 - index).replaceAll("☆", "")}
              </li>
            ))}
          </ul>
          <dl className="consistency-rows">
            <div>
              <dt>Average per attack</dt>
              <dd>{signedOr(stats.perAttack, 1)}</dd>
            </div>
            <div>
              <dt>Average when not tripled</dt>
              <dd>{signedOr(stats.perMiss, 1)}</dd>
            </div>
            <div>
              <dt>Trophies missed vs all triples</dt>
              <dd className="negative">{signedOr(stats.missed)}</dd>
            </div>
          </dl>
          <div className="consistency-days">
            <span className="dash-muted">Triples each day, out of {DAILY_BATTLES}</span>
            <ol>
              {stats.perDay.map((day) => (
                <li
                  key={day.day}
                  title={
                    day.triples === null
                      ? `Day ${day.day}: not available, some attacks may be missing`
                      : `Day ${day.day}: ${day.triples} of ${DAILY_BATTLES} tripled`
                  }
                >
                  <span className="consistency-box">
                    {day.triples === null ? null : (
                      <i style={{ height: `${(day.triples / DAILY_BATTLES) * 100}%` }} />
                    )}
                    <b>{count(day.triples)}</b>
                  </span>
                  D{day.day}
                </li>
              ))}
            </ol>
          </div>
        </>
      )}
    </>
  );
}
