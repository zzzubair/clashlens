import { Fragment, useCallback, useState, type CSSProperties } from "react";

import { UnitArt } from "./ArmyPortraits";
import { starRate, type ArmyRow } from "../lib/army-stats";

/** A place on the plot as a CSS percentage, rounded so the server's and the
 *  browser's last-digit float differences never change the page. */
const place = (percent: number) => `${percent.toFixed(3)}%`;

/** Every rate shown with a little room at both ends, and the round step
 *  its labels take: the plot fits the armies, so rates bunched within a few
 *  points spread out over its whole height. */
function rateBand(rates: number[], average: number) {
  const all = [...rates, average].map((rate) => rate * 100);
  const low = Math.max(0, Math.min(...all) - 0.75);
  const high = Math.min(100, Math.max(...all) + 0.75);
  const span = high - low;
  const step = span <= 6 ? 1 : span <= 12 ? 2 : span <= 30 ? 5 : 10;
  return { low, high, step };
}

/** Round usage steps; the axis takes the smallest that gives at most six. */
const STEPS = [0.01, 0.02, 0.05, 0.1, 0.2];

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
  ["top-left", "Less used, more 3-stars"],
  ["top-right", "More used, more 3-stars"],
  ["bottom-left", "Less used, fewer 3-stars"],
  ["bottom-right", "More used, fewer 3-stars"],
] as const;
type Corner = (typeof QUARTERS)[number][0];

type Point = {
  key: string;
  name: string;
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
 * as its own icon's. Where its name can't go, it waits for a point or a
 * tap.
 */
function layout(
  points: Point[],
  guides: { x: number; y: number },
  plot: { width: number; height: number; icon: number; char: number },
) {
  const half = plot.icon / 2;
  const at = (point: { x: number; y: number }) => ({
    cx: (point.x / 100) * plot.width,
    cy: (point.y / 100) * plot.height,
  });
  // Icons that would cover each other are nudged apart until each shows
  // whole, each kept as near its true spot as it can be and inside the plot.
  const gap = plot.icon * 1.2;
  const truth = points.map(at);
  const moved = truth.map((spot) => ({ ...spot }));
  for (let round = 0; round < 120; round++) {
    for (let a = 0; a < moved.length; a++) {
      for (let b = a + 1; b < moved.length; b++) {
        const one = moved[a]!;
        const two = moved[b]!;
        let dx = two.cx - one.cx;
        let dy = two.cy - one.cy;
        let distance = Math.hypot(dx, dy);
        if (distance >= gap) continue;
        if (distance < 0.01) [dx, dy, distance] = [Math.cos(a + b), Math.sin(a + b), 1];
        const push = (gap - distance) / 2 / distance;
        one.cx -= dx * push;
        one.cy -= dy * push;
        two.cx += dx * push;
        two.cy += dy * push;
      }
    }
    moved.forEach((spot, index) => {
      if (round < 100) {
        spot.cx += (truth[index]!.cx - spot.cx) * 0.04;
        spot.cy += (truth[index]!.cy - spot.cy) * 0.04;
      }
      spot.cx = Math.min(plot.width - half, Math.max(half, spot.cx));
      spot.cy = Math.min(plot.height - half, Math.max(half, spot.cy));
    });
  }
  // Its true spot is marked only where it shows clear of the icon.
  const spots = new Map(
    points.map((point, index) => {
      const spot = moved[index]!;
      const away = Math.hypot(spot.cx - truth[index]!.cx, spot.cy - truth[index]!.cy);
      return [
        point.key,
        {
          x: (spot.cx / plot.width) * 100,
          y: (spot.cy / plot.height) * 100,
          away: away > half + 4,
        },
      ];
    }),
  );
  const icons = points.map((point, index) => {
    const { cx, cy } = moved[index]!;
    const box = { left: cx - half, right: cx + half, top: cy - half, bottom: cy + half };
    return { key: point.key, ...box };
  });
  const lines = at(guides);
  const crossing = [
    { left: -1e4, right: 1e4, top: lines.cy, bottom: lines.cy },
    { left: lines.cx, right: lines.cx, top: -1e4, bottom: 1e4 },
  ];
  // A quarter's name is small: about 6.4 pixels a letter, 14 tall, 4 in.
  const quarters = new Set<Corner>();
  const names: Box[] = [];
  for (const [corner, words] of QUARTERS) {
    const width = words.length * 6.4;
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
  const labels = new Map<
    string,
    {
      side: Side;
      text: string;
      shift: { x: number; y: number };
      leader: { x1: number; y1: number; x2: number; y2: number } | null;
    }
  >();
  for (const point of [...points].sort((a, b) => b.usage - a.usage)) {
    const { cx, cy } = moved[points.indexOf(point)]!;
    const text = point.name;
    const width = text.length * plot.char + 4;
    // Where a name can go: beside its icon first, then a step further out,
    // where a hairline leads back to the icon.
    const shifted = (side: Side, far: number) => {
      const [across, height] = SIDES[side];
      return {
        x: across === "right" ? far : across === "left" ? -far : 0,
        y: across === "middle" ? Math.sign(height) * far : Math.sign(height) * far * 0.6,
      };
    };
    const box = (side: Side, far: number): Box => {
      const [across, height] = SIDES[side];
      const shift = shifted(side, far);
      const left =
        (across === "right"
          ? cx + half + 3
          : across === "left"
            ? cx - half - 3 - width
            : cx - width / 2) + shift.x;
      // Over or under the icon clears it; level with an edge centres on it.
      const middle =
        cy + height * (Math.abs(height) === 1 ? half + 10 : plot.icon) + shift.y;
      return { left, right: left + width, top: middle - 8, bottom: middle + 8 };
    };
    const fits = (name: Box) =>
      name.left >= 0 &&
      name.right <= plot.width + 20 &&
      name.top >= -4 &&
      name.bottom <= plot.height + 4 &&
      crossing.every((line) => apart(name, line, 2)) &&
      names.every((other) => apart(name, other, 3)) &&
      icons.every((other) => other.key === point.key || apart(name, other, 6));
    const choice = [0, 24, 44]
      .flatMap((far) => (Object.keys(SIDES) as Side[]).map((side) => ({ side, far })))
      .find(({ side, far }) => fits(box(side, far)));
    if (choice === undefined) continue;
    const name = box(choice.side, choice.far);
    names.push(name);
    // The hairline runs from the icon to the nearest point of its name.
    const to = {
      x: Math.min(name.right, Math.max(name.left, cx)),
      y: Math.min(name.bottom, Math.max(name.top, cy)),
    };
    labels.set(point.key, {
      side: choice.side,
      text,
      shift: shifted(choice.side, choice.far),
      leader:
        choice.far === 0
          ? null
          : {
              x1: (cx / plot.width) * 100,
              y1: (cy / plot.height) * 100,
              x2: (to.x / plot.width) * 100,
              y2: (to.y / plot.height) * 100,
            },
    });
  }
  return { labels, quarters, spots };
}

/**
 * The armies at a glance: how much each is used (across, from 0 in equal
 * steps, so distance reads as usage) against how often it
 * three-stars (up), over faint lines every ten points and a dashed cross at
 * the Legend average 3-star rate and the average army's use, its four
 * quarters named. Each army is drawn as its troops, named beside them where
 * the name fits; pointing at or tapping one names it with its numbers.
 * Phones don't show it: each row of the table under it carries the same two
 * numbers as bars, which a 390px scatter could only repeat.
 */
export function ArmyMap({
  rows,
  usageEnd,
  threeStar,
}: {
  /** Named armies only, in the table's order. */
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
    // A plot not laid out yet measures 0 by 0; it keeps the sizes below.
    const observer = new ResizeObserver(([entry]) => {
      const { width, height } = entry!.contentRect;
      if (width > 0 && height > 0) setSize({ width, height });
    });
    observer.observe(plot);
    return () => observer.disconnect();
  }, []);
  const used = rows.filter((row) => row.usageRate > 0);
  if (used.length < 2 || usageEnd <= 0) return null;
  const {
    low,
    high,
    step: bandStep,
  } = rateBand(
    used.map((row) => starRate(row, 3)),
    threeStar,
  );
  // A little room at both ends so no icon sits on the edge.
  const x = (usage: number) => 3 + Math.min(1, Math.max(0, usage / usageEnd)) * 94;
  const y = (rate: number) => ((high - rate) / (high - low)) * 100;
  const averageUse = used.reduce((sum, row) => sum + row.usageRate, 0) / used.length;
  const percent = (value: number) => `${(value * 100).toFixed(1)}%`;
  const tick = (value: number) => `${Number((value * 100).toPrecision(2))}%`;
  // 0, the round steps between, kept clear of the end's own label, and the end.
  const useStep = STEPS.find((step) => usageEnd / step <= 6) ?? 0.5;
  const steps: number[] = [];
  for (let value = useStep; value < usageEnd - useStep / 2; value += useStep)
    steps.push(value);
  // At most five labels up the side.
  const rateStep = (high - low) / bandStep > 4 ? bandStep * 2 : bandStep;
  const rates: number[] = [];
  for (let rate = Math.ceil(low / rateStep) * rateStep; rate <= high; rate += rateStep)
    rates.push(rate);
  const points = used.map((row) => ({
    key: row.key,
    name: row.label,
    usage: row.usageRate,
    x: x(row.usageRate),
    y: y(starRate(row, 3) * 100),
  }));
  const guides = { x: x(averageUse), y: y(threeStar * 100) };
  // Until it is measured, the plot is about 1,060 by 288 pixels.
  const wide = layout(points, guides, {
    width: size?.width ?? 1060,
    height: size?.height ?? 288,
    icon: 26,
    char: 6.7,
  });
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
          <span key={rate} className="army-map-rate" style={{ top: place(y(rate)) }}>
            {rate > low && rate < high ? <span className="army-map-grid" /> : null}
            <span className="army-map-tick" data-axis="rate">
              {rate}%
            </span>
          </span>
        ))}
        <span
          className="army-map-cross"
          data-axis="rate"
          style={{ top: place(guides.y) }}
        />
        <span
          className="army-map-cross"
          data-axis="use"
          style={{ left: place(guides.x) }}
        />
        {[0, ...steps, usageEnd].map((value) => (
          <span
            key={value}
            className="army-map-tick"
            data-axis="use"
            style={{ left: place(x(value)) }}
          >
            {tick(value)}
          </span>
        ))}
        <span className="army-map-axis" data-axis="use">
          Usage
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
          >
            {words}
          </span>
        ))}
        {/* Hairlines from icons to names set further out. */}
        <svg
          className="army-map-leader"
          data-mode="wide"
          viewBox="0 0 100 100"
          preserveAspectRatio="none"
        >
          {[...wide.labels].map(([key, placed]) =>
            placed.leader === null ? null : (
              <line key={key} {...placed.leader} vectorEffect="non-scaling-stroke" />
            ),
          )}
        </svg>
        {used.map((row, index) => {
          const point = points[index]!;
          const placed = wide.labels.get(row.key);
          const label =
            placed === undefined ? null : (
              <span
                className="army-map-label"
                data-mode="wide"
                data-side={placed.side}
                style={{
                  transform: `translate(${placed.shift.x}px, ${placed.shift.y}px)`,
                }}
              >
                {placed.text}
              </span>
            );
          const spot = wide.spots.get(row.key)!;
          const moved = spot.away ? spot : null;
          return (
            <Fragment key={row.key}>
              {moved === null ? null : (
                <svg
                  className="army-map-leader"
                  data-mode="wide"
                  viewBox="0 0 100 100"
                  preserveAspectRatio="none"
                >
                  <line
                    x1={point.x.toFixed(3)}
                    y1={point.y.toFixed(3)}
                    x2={moved.x.toFixed(3)}
                    y2={moved.y.toFixed(3)}
                    vectorEffect="non-scaling-stroke"
                  />
                </svg>
              )}
              {/* The army's exact spot, when its icon was nudged off it. */}
              {moved === null ? null : (
                <span
                  className="army-map-dot"
                  data-wide
                  style={{ left: place(point.x), top: place(point.y) }}
                />
              )}
              <span
                className="army-map-point"
                data-named={named === row.key || undefined}
                // A name near either edge opens inward, so it stays on screen.
                data-edge={point.x > 70 ? "end" : point.x < 30 ? "start" : undefined}
                style={
                  {
                    "--wx": place(spot.x),
                    "--wy": place(spot.y),
                  } as CSSProperties
                }
                onClick={() => setNamed(named === row.key ? null : row.key)}
                // A mouse moving off lets go; a finger's tap stays picked.
                onPointerLeave={(event) => {
                  if (event.pointerType === "mouse") setNamed(null);
                }}
              >
                <UnitArt unitKey={row.key} />
                {label}
                <span className="army-map-name">
                  {row.label} · {percent(row.usageRate)} · {percent(starRate(row, 3))}
                </span>
              </span>
            </Fragment>
          );
        })}
      </div>
    </figure>
  );
}
