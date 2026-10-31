import type { DayResult } from "../lib/group-comparison";
import { dayLabel, dayRange } from "./DayScale";

/** One player's line: who, and the days the table's gain counts. */
export interface GainLine {
  tag: string;
  name: string;
  you: boolean;
  days: DayResult[];
}

/** Lighting a player up: while a mouse points at them, or picked with a
 *  click or tap until picked again. */
export interface Light {
  hover: (tag: string | null) => void;
  pin: (tag: string) => void;
}

/** A mouse pointing in and out; a tap is only a pick, so it can't leave a
 *  light stuck on that no pointer will ever leave. */
export function pointed(light: Light, tag: string) {
  return {
    onPointerEnter: (event: React.PointerEvent) => {
      if (event.pointerType === "mouse") light.hover(tag);
    },
    onPointerLeave: (event: React.PointerEvent) => {
      if (event.pointerType === "mouse") light.hover(null);
    },
  };
}

/** A day the gain counts: finished, or still settling, with a result. */
function counted(day: DayResult): boolean {
  return (day.state === "complete" || day.state === "correcting") && day.net !== null;
}

/** Trophies gained since the period began, after each day: the same days,
 *  added the same way, as the table's gain, so each line ends on it. */
function climb(days: DayResult[]): number[] {
  const points = [0];
  for (const day of days) points.push(points.at(-1)! + (counted(day) ? day.net! : 0));
  return points;
}

type End = { tag: string; at: number };

/**
 * Every line's label by its own line's end, pushed apart just enough not to
 * overlap: a crowd of ends spreads evenly round its middle, a little past
 * the plot's top and bottom at most. `gap` is a label's height as a share of
 * the plot's; null when the labels can't all fit, and go in a list instead.
 */
function stacked(ends: End[], gap: number) {
  const [low, high] = [-4, 104];
  if (ends.length * gap > high - low) return null;
  const groups: { members: End[]; top: number }[] = [];
  for (const end of [...ends].sort((a, b) => a.at - b.at)) {
    groups.push({ members: [end], top: end.at });
    while (groups.length > 1) {
      const [before, last] = groups.slice(-2) as [(typeof groups)[0], (typeof groups)[0]];
      if (before.top + before.members.length * gap <= last.top) break;
      const members = [...before.members, ...last.members];
      const middle = members.reduce((sum, member) => sum + member.at, 0) / members.length;
      groups.splice(-2, 2, { members, top: middle - ((members.length - 1) * gap) / 2 });
    }
  }
  // Held inside the plot, a crowd at an edge pushes back into the next.
  const at = new Map<string, number>();
  let floor = low;
  const placed = groups.flatMap(({ members, top }) =>
    members.map((member, index) => ({ member, at: top + index * gap })),
  );
  placed.forEach((label, index) => {
    const ceiling = high - (placed.length - 1 - index) * gap;
    label.at = Math.min(Math.max(label.at, floor), ceiling);
    floor = label.at + gap;
    at.set(label.member.tag, label.at);
  });
  return at;
}

/** Round steps for the trophy ticks: two to four across the chart's height. */
function tickStep(height: number): number {
  const rough = height / 4;
  const size = 10 ** Math.floor(Math.log10(rough));
  return ([1, 2, 5, 10].find((step) => step * size >= rough) ?? 10) * size;
}

function signed(value: number): string {
  const text = Math.abs(value).toLocaleString("en");
  return value > 0 ? `+${text}` : value < 0 ? `−${text}` : text;
}

/**
 * Every player's trophies gained over the days compared, one line each from
 * 0, on one scale: Your lines in two ember shades, the rest quiet. Every line
 * is named at its end, the names stacked with short leaders where ends
 * crowd, or listed under the chart when they can't all fit beside it.
 * Pointing at a line, its name or its row in the table lights that player up.
 */
export function GainLines({
  lines,
  dayStarts,
  lit: picked,
  light,
}: {
  lines: GainLine[];
  /** The days compared, one a segment, first to last. */
  dayStarts: string[];
  lit: string | null;
  light: Light;
}) {
  const yours = lines.filter((line) => line.you).map((line) => line.tag);
  const series = lines.map((line) => ({
    ...line,
    points: climb(line.days),
    // Your first player in ember, a second in a lighter or deeper ember.
    shade: line.you ? (yours.indexOf(line.tag) === 0 ? "1" : "2") : undefined,
  }));
  // No one has a counted day yet: nothing to draw, so no chart at all.
  if (!lines.some((line) => line.days.some(counted))) return null;
  // A player with no line here, lit from the table, lights nothing.
  const lit = series.some((line) => line.tag === picked) ? picked : null;
  const values = series.flatMap((line) => line.points);
  const top = Math.max(0, ...values);
  const bottom = Math.min(0, ...values);
  const height = top - bottom || 1;
  // Up from the bottom edge, with a little room so no line rides the edge.
  const y = (value: number) => 4 + ((top - value) / height) * 92;
  const x = (index: number, count: number) => (index / Math.max(1, count - 1)) * 100;
  const step = tickStep(height);
  const ticks: number[] = [];
  for (let tick = Math.ceil(bottom / step) * step; tick <= top; tick += step)
    if (tick !== 0) ticks.push(tick);
  const ends = series.map((line) => ({ tag: line.tag, at: y(line.points.at(-1)!) }));
  // A label is about 1.1rem tall: 11rem of chart on wide screens, 8rem on a
  // phone, where the names go under the chart sooner so the lines keep
  // the width.
  const wide = stacked(ends, 10.5);
  const narrow = stacked(ends, 14);
  const list = [wide ? null : "wide", narrow ? null : "narrow"].filter(Boolean).join(" ");
  // A short leader joins a nudged label to its line's end.
  const leaders = (at: Map<string, number> | null, mode: "wide" | "narrow") =>
    ends.flatMap((end) => {
      const label = at?.get(end.tag);
      return label === undefined || Math.abs(label - end.at) < 1.5
        ? []
        : [
            <line
              key={`${mode}-${end.tag}`}
              data-mode={mode}
              x1="0"
              x2="100"
              y1={end.at}
              y2={label}
            />,
          ];
    });
  // Your lines over the rest, and the lit one over all.
  const drawn = [...series].sort(
    (a, b) =>
      Number(a.tag === lit) - Number(b.tag === lit) || Number(a.you) - Number(b.you),
  );
  const lighting = (tag: string) => ({
    ...pointed(light, tag),
    onClick: () => light.pin(tag),
  });
  const range = dayRange(dayStarts);
  // Every day's date on a wide screen; a phone has room for the first,
  // middle and last.
  const keyDays = new Set([
    0,
    Math.floor((dayStarts.length - 1) / 2),
    dayStarts.length - 1,
  ]);
  return (
    <figure
      className="gain-lines"
      data-lit={lit === null ? undefined : ""}
      data-list={list || undefined}
      data-two-yours={yours.length > 1 || undefined}
    >
      <figcaption>
        <span className="gain-lines-title">
          {`Trophies gained${range ? `, ${range}` : ""}`}
        </span>
        <span className="gain-lines-key" aria-hidden="true">
          <span data-you="">You</span>
          <span>Others</span>
        </span>
      </figcaption>
      <p className="sr-only">
        {series.map((line) => `${line.name} ${signed(line.points.at(-1)!)}`).join(", ")}
      </p>
      <div className="gain-lines-plot" aria-hidden="true">
        <svg viewBox="0 0 100 100" preserveAspectRatio="none">
          {ticks.map((tick) => (
            <line
              key={tick}
              className="gain-lines-grid"
              x1="0"
              x2="100"
              y1={y(tick)}
              y2={y(tick)}
            />
          ))}
          <line className="gain-lines-zero" x1="0" x2="100" y1={y(0)} y2={y(0)} />
          {drawn.map((line) => {
            const path = line.points
              .map(
                (value, index) =>
                  `${index ? "L" : "M"}${x(index, line.points.length)} ${y(value)}`,
              )
              .join(" ");
            return (
              <g
                key={line.tag}
                className="gain-line"
                data-shade={line.shade}
                data-lit={line.tag === lit || undefined}
                {...lighting(line.tag)}
              >
                <path className="gain-line-hit" d={path} />
                <path d={path} />
              </g>
            );
          })}
        </svg>
        {[0, ...ticks].map((tick) => (
          <span key={tick} className="gain-lines-tick" style={{ top: `${y(tick)}%` }}>
            {signed(tick)}
          </span>
        ))}
        {dayStarts.map((start, index) => (
          <span
            key={start}
            className="gain-lines-day"
            data-key={keyDays.has(index) || undefined}
            style={{ left: `${((index + 0.5) / dayStarts.length) * 100}%` }}
          >
            {dayLabel(start)}
          </span>
        ))}
        <svg
          className="gain-lines-leaders"
          viewBox="0 0 100 100"
          preserveAspectRatio="none"
        >
          {leaders(wide, "wide")}
          {leaders(narrow, "narrow")}
        </svg>
        {series.map((line) => (
          <span
            key={line.tag}
            className="gain-lines-end"
            data-shade={line.shade}
            data-lit={line.tag === lit || undefined}
            style={
              {
                "--wide-at": wide ? `${wide.get(line.tag)}%` : undefined,
                "--narrow-at": narrow ? `${narrow.get(line.tag)}%` : undefined,
              } as React.CSSProperties
            }
            {...lighting(line.tag)}
          >
            <span className="gain-lines-name">{line.name}</span>{" "}
            {signed(line.points.at(-1)!)}
          </span>
        ))}
      </div>
      {list ? (
        <ul className="gain-lines-list" aria-hidden="true">
          {[...series]
            .sort((a, b) => b.points.at(-1)! - a.points.at(-1)!)
            .map((line) => (
              <li
                key={line.tag}
                data-shade={line.shade}
                data-lit={line.tag === lit || undefined}
                {...lighting(line.tag)}
              >
                <span className="gain-lines-name">{line.name}</span>{" "}
                {signed(line.points.at(-1)!)}
              </li>
            ))}
        </ul>
      ) : null}
    </figure>
  );
}
