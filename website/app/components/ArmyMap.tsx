import { useCallback, useState } from "react";

import { UnitArt } from "./ArmyPortraits";
import { starRate, type ArmyRow } from "../lib/army-stats";

/** Ten-point steps that hold every rate shown, with room at both ends. */
function rateBand(rates: number[], average: number) {
  const all = [...rates, average].map((rate) => rate * 100);
  const low = Math.max(0, Math.floor((Math.min(...all) - 3) / 10) * 10);
  const high = Math.min(100, Math.ceil((Math.max(...all) + 3) / 10) * 10);
  return { low, high: high > low ? high : low + 10 };
}

/** Round usage steps; the axis starts at the biggest one under every army. */
const STEPS = [0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5];

interface Box {
  left: number;
  right: number;
  top: number;
  bottom: number;
}
const apart = (a: Box, b: Box, room = 0) =>
  a.right + room <= b.left ||
  b.right + room <= a.left ||
  a.bottom + room <= b.top ||
  b.bottom + room <= a.top;

/** Where a name can sit: across from the icon, then which height. */
const SIDES = {
  right: ["right", 0],
  left: ["left", 0],
  above: ["middle", -1],
  below: ["middle", 1],
  "right-up": ["right", -0.5],
  "right-down": ["right", 0.5],
  "left-up": ["left", -0.5],
  "left-down": ["left", 0.5],
} as const;
type Side = keyof typeof SIDES;

/** The four quarters the average lines make, named in their outer corners. */
const QUARTERS = [
  ["top-left", "Niche but strong"],
  ["top-right", "Popular and strong"],
  ["bottom-left", "Niche, weaker"],
  ["bottom-right", "Popular, weaker"],
] as const;
type Corner = (typeof QUARTERS)[number][0];

type Point = {
  key: string;
  name: string;
  number: string;
  usage: number;
  x: number;
  y: number;
};

/**
 * What the chart says where, worked out in pixels for a plot this size.
 * Each quarter's name shows in its outer corner when it fits inside its
 * quarter clear of every icon. Then each army is named beside its icon, the
 * most-used first: beside it if it fits, else over or under it, else level
 * with its top or bottom edge; never over the average lines or a quarter's
 * name, and kept well apart from other icons and names so each name reads
 * as its own icon's. Where its name can't go, its number from the table
 * does; with no room for that either, it waits for a point or a tap.
 */
function layout(
  points: Point[],
  guides: { x: number; y: number },
  plot: { width: number; height: number; icon: number; char: number },
  named: boolean,
) {
  const half = plot.icon / 2;
  const at = (point: { x: number; y: number }) => ({
    cx: (point.x / 100) * plot.width,
    cy: (point.y / 100) * plot.height,
  });
  const icons = points.map((point) => {
    const { cx, cy } = at(point);
    const box = { left: cx - half, right: cx + half, top: cy - half, bottom: cy + half };
    return { key: point.key, ...box };
  });
  const lines = at(guides);
  const crossing = [
    { left: -1e4, right: 1e4, top: lines.cy, bottom: lines.cy },
    { left: lines.cx, right: lines.cx, top: -1e4, bottom: 1e4 },
  ];
  // A quarter's name is small: about 5.8 pixels a letter, 14 tall, 4 in.
  const quarters = new Set<Corner>();
  const names: Box[] = [];
  for (const [corner, words] of QUARTERS) {
    const width = words.length * 5.8;
    const left = corner.endsWith("left") ? 4 : plot.width - 4 - width;
    const top = corner.startsWith("top") ? 4 : plot.height - 18;
    const box = { left, right: left + width, top, bottom: top + 14 };
    const inside = corner.endsWith("left")
      ? box.right + 4 <= lines.cx
      : box.left - 4 >= lines.cx;
    const level = corner.startsWith("top")
      ? box.bottom + 4 <= lines.cy
      : box.top - 4 >= lines.cy;
    if (inside && level && icons.every((icon) => apart(box, icon, 4))) {
      quarters.add(corner);
      names.push(box);
    }
  }
  const labels = new Map<string, { side: Side; text: string }>();
  for (const point of named ? [...points].sort((a, b) => b.usage - a.usage) : []) {
    const { cx, cy } = at(point);
    for (const text of [point.name, point.number]) {
      const width = text.length * plot.char + 4;
      const box = ([across, height]: (typeof SIDES)[Side]): Box => {
        const left =
          across === "right"
            ? cx + half + 3
            : across === "left"
              ? cx - half - 3 - width
              : cx - width / 2;
        // Over or under the icon clears it; level with an edge centres on it.
        const middle = cy + height * (Math.abs(height) === 1 ? half + 10 : plot.icon);
        return { left, right: left + width, top: middle - 8, bottom: middle + 8 };
      };
      const side = (Object.keys(SIDES) as Side[]).find((each) => {
        const name = box(SIDES[each]);
        return (
          name.left >= 0 &&
          name.right <= plot.width + 20 &&
          name.top >= -4 &&
          name.bottom <= plot.height + 4 &&
          crossing.every((line) => apart(name, line, 2)) &&
          names.every((other) => apart(name, other, 3)) &&
          icons.every((other) => other.key === point.key || apart(name, other, 10))
        );
      });
      if (side === undefined) continue;
      names.push(box(SIDES[side]));
      labels.set(point.key, { side, text });
      break;
    }
  }
  return { labels, quarters };
}

/**
 * The armies at a glance: how much each is used (across, each step a
 * multiple, so the many little-used armies spread out) against how often it
 * three-stars (up), over faint lines every ten points and a dashed cross at
 * the Legend average 3-star rate and the average army's use, its four
 * quarters named. A wide screen draws each army as its troops, named beside
 * them where the name fits; a phone draws each as its number in the table
 * under the chart. Pointing at or tapping an army names it with its numbers.
 */
export function ArmyMap({
  rows,
  usageEnd,
  threeStar,
}: {
  /** Named armies only, in the table's order, numbered as the table numbers them. */
  rows: ArmyRow[];
  /** The usage bars' scale end, so the chart and the table end alike. */
  usageEnd: number;
  /** The Legend average 3-star rate over every attack shown. */
  threeStar: number;
}) {
  const [named, setNamed] = useState<string | null>(null);
  // The plot's real size once on screen, so names are placed for the width
  // they get on a tablet or a small laptop, not only the widest.
  const [size, setSize] = useState<{ width: number; height: number } | null>(null);
  const measure = useCallback((plot: HTMLDivElement | null) => {
    if (plot === null) return;
    const observer = new ResizeObserver(([entry]) =>
      setSize({ width: entry!.contentRect.width, height: entry!.contentRect.height }),
    );
    observer.observe(plot);
    return () => observer.disconnect();
  }, []);
  const used = rows.filter((row) => row.usageRate > 0);
  if (used.length < 2 || usageEnd <= 0) return null;
  const { low, high } = rateBand(
    used.map((row) => starRate(row, 3)),
    threeStar,
  );
  const least = Math.min(...used.map((row) => row.usageRate));
  // Below the scale's end too, so armies all used alike still spread out.
  const start =
    [...STEPS].reverse().find((step) => step <= least && step < usageEnd) ?? least / 2;
  const span = Math.log(usageEnd / start) || 1;
  // A little room at both ends so no icon sits on the edge.
  const x = (usage: number) =>
    3 + Math.min(1, Math.max(0, Math.log(usage / start) / span)) * 94;
  const y = (rate: number) => ((high - rate) / (high - low)) * 100;
  const averageUse = used.reduce((sum, row) => sum + row.usageRate, 0) / used.length;
  const percent = (value: number) => `${(value * 100).toFixed(1)}%`;
  const tick = (value: number) => `${Number((value * 100).toPrecision(2))}%`;
  // The round steps between the ends, kept clear of the end's own label.
  const steps = STEPS.filter((step) => step > start && step < usageEnd / 1.6);
  const rateStep = high - low > 50 ? 20 : 10;
  const rates: number[] = [];
  for (let rate = low; rate <= high; rate += rateStep) rates.push(rate);
  const points = used.map((row) => ({
    key: row.key,
    name: row.label,
    number: String(rows.indexOf(row) + 1),
    usage: row.usageRate,
    x: x(row.usageRate),
    y: y(starRate(row, 3) * 100),
  }));
  const guides = { x: x(averageUse), y: y(threeStar * 100) };
  // Until it is measured, a wide screen's plot is about 1,060 by 240
  // pixels and a phone's 290 by 192, where each army is its number, so
  // needs no name beside it.
  const wide = layout(
    points,
    guides,
    { width: size?.width ?? 1060, height: size?.height ?? 240, icon: 32, char: 6.7 },
    true,
  );
  const narrow = layout(
    points,
    guides,
    { width: size?.width ?? 290, height: size?.height ?? 192, icon: 20, char: 0 },
    false,
  );
  return (
    <figure className="army-map">
      <figcaption>
        <span className="army-map-title">Usage vs 3-star rate</span>
        <span className="army-map-key" aria-hidden="true">
          Averages: {percent(threeStar)} 3-star, {percent(averageUse)} use
        </span>
      </figcaption>
      <p className="sr-only">
        {used
          .map(
            (row) =>
              `${row.label}: ${percent(row.usageRate)} use, ${percent(starRate(row, 3))} 3-star`,
          )
          .join("; ")}
      </p>
      <div className="army-map-plot" aria-hidden="true" ref={measure}>
        {rates.map((rate) => (
          <span key={rate} className="army-map-rate" style={{ top: `${y(rate)}%` }}>
            {rate > low && rate < high ? <span className="army-map-grid" /> : null}
            <span className="army-map-tick" data-axis="rate">
              {rate}%
            </span>
          </span>
        ))}
        <span
          className="army-map-cross"
          data-axis="rate"
          style={{ top: `${guides.y}%` }}
        />
        <span
          className="army-map-cross"
          data-axis="use"
          style={{ left: `${guides.x}%` }}
        />
        {[start, ...steps, usageEnd].map((value) => (
          <span
            key={value}
            className="army-map-tick"
            data-axis="use"
            style={{ left: `${x(value)}%` }}
          >
            {tick(value)}
          </span>
        ))}
        <span className="army-map-axis" data-axis="use">
          Usage (log scale)
        </span>
        <span className="army-map-axis" data-axis="rate">
          3-star rate
        </span>
        {QUARTERS.map(([corner, words]) => (
          <span
            key={corner}
            className="army-map-quarter"
            data-corner={corner}
            data-wide={wide.quarters.has(corner) || undefined}
            data-narrow={narrow.quarters.has(corner) || undefined}
          >
            {words}
          </span>
        ))}
        {used.map((row, index) => {
          const point = points[index]!;
          const label = (mode: "wide" | "narrow") => {
            const placed = (mode === "wide" ? wide : narrow).labels.get(row.key);
            return placed === undefined ? null : (
              <span className="army-map-label" data-mode={mode} data-side={placed.side}>
                {placed.text}
              </span>
            );
          };
          return (
            <span
              key={row.key}
              className="army-map-point"
              data-named={named === row.key || undefined}
              // A name near either edge opens inward, so it stays on screen.
              data-edge={point.x > 70 ? "end" : point.x < 30 ? "start" : undefined}
              style={{ left: `${point.x}%`, top: `${point.y}%` }}
              onClick={() => setNamed(named === row.key ? null : row.key)}
              onMouseLeave={() => setNamed(null)}
            >
              <UnitArt unitKey={row.key} />
              <span className="army-map-number">{point.number}</span>
              {label("wide")}
              <span className="army-map-name">
                {row.label} · {percent(row.usageRate)} · {percent(starRate(row, 3))}
              </span>
            </span>
          );
        })}
      </div>
    </figure>
  );
}
