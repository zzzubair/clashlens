import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import type { ReactElement, ReactNode } from "react";
import { HeatMapCard, SeasonChart } from "../../app/components/SeasonCards";
import { heatCells, type SeasonData } from "../../app/lib/dashboard-cards";

const text = (html: string) => html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");

// Day 1 began at the 05:00 UTC Reset on 1 October.
const START = Date.UTC(2026, 9, 1, 5);
const season = (days: SeasonData["days"], today = 9): SeasonData => ({
  dayCount: 28,
  endMs: null,
  windows: { "3": null, "7": null, season: null },
  averages: { offense: 301.6666666666667, defense: -256 },
  today,
  startMs: START,
  days,
});
const day = (number: number, net: number, changes = {}) => ({
  day: number,
  kind: "played" as const,
  attackSlots: 8,
  maximumAttackGain: 320,
  attacks: 8,
  defenses: 8,
  offense: 300,
  defense: net - 300,
  net,
  triples: 6,
  held: 4,
  endTrophies: 5000 + net,
  rank: null,
  hits: [],
  ...changes,
});

describe("Season heat map", () => {
  it("ranks each day against your own Season; a shield day is grey", () => {
    // The Season the dashboard's loader works out in dashboard-cards.test.ts:
    // read on Day 9, Day 3 a shield day and Day 7 never saved.
    const saved = ([n, offense, defense, end, rank]: number[]) =>
      day(n!, offense! + defense!, { offense, defense, endTrophies: end, rank });
    const data = season([
      saved([1, 300, -256, 5044, 900]),
      saved([2, 310, -240, 5114, 700]),
      day(3, 0, {
        kind: "shield",
        attacks: 0,
        defenses: 0,
        offense: 0,
        defense: 0,
        endTrophies: 5114,
      }),
      saved([4, 280, -272, 5122, 800]),
      saved([5, 320, -256, 5186, 500]),
      saved([6, 290, -288, 5188, 520]),
      saved([8, 310, -224, 5324, 318]),
    ]);
    const html = renderToStaticMarkup(createElement(HeatMapCard, { data }));
    expect(text(html)).toContain(
      "Up days 6 Best +86 · Day 8 Down days 0 Worst +2 · Day 6 No change 0",
    );
    expect(html).toContain('aria-label="Day 3: no battles"');
    expect(html).toContain('aria-label="Day 7: no data"');
    expect(html).toContain('aria-label="Day 9: today, not finished yet"');
    expect(html).toContain('aria-label="Day 10: not played yet"');
    expect(html).toMatch(
      /data-step="4"[^>]*><span[^>]*>D<span>ay <\/span>8<\/span><button[^>]*aria-label="Day 8: \+86"/,
    );
    expect(html).toMatch(/data-step="0"[^>]*><button[^>]*aria-label="Day 6: \+2"/);
  });

  it("dates each day from the Season's start, never the device's clock", () => {
    // At 05:05 UTC on Day 9, 9 October, a device clock 10 minutes slow still
    // reads 8 October, the Legend day before.
    vi.useFakeTimers({ now: START + 8 * 86_400_000 - 5 * 60_000 });
    try {
      const html = renderToStaticMarkup(
        createElement(HeatMapCard, { data: season([day(8, 86)]) }),
      );
      expect(text(html)).toContain("Day 9 · 9 Oct · in progress");
    } finally {
      vi.useRealTimers();
    }
  });

  it("gives equal days one colour, a lone day its sign, and leaves shields out", () => {
    const cells = heatCells(
      season([
        day(1, 10),
        day(2, 10),
        day(3, 30),
        day(4, 0, { kind: "shield", attacks: 0, defenses: 0 }),
        day(5, -20),
      ]),
      "net",
    );
    expect(cells.slice(0, 5).map((cell) => [cell.kind, cell.shade])).toEqual([
      ["played", 0.5],
      ["played", 0.5],
      ["played", 1],
      ["shield", null],
      ["played", 0],
    ]);
    expect(heatCells(season([day(1, 40)]), "net")[0]!.shade).toBe(1);
  });

  it("draws every lost day red, on its step, whatever the step", () => {
    // Every day lost: the least bad is the Season's best, its richest step.
    const data = season([day(1, -10), day(2, -120), day(3, -60)], 4);
    const html = renderToStaticMarkup(createElement(HeatMapCard, { data }));
    const cellOf = (n: number) =>
      html.split("<li").find((cell) => cell.includes(`aria-label="Day ${n}: `))!;
    expect(cellOf(1)).toContain('data-step="4"');
    expect(cellOf(1)).toContain("is-loss");
    expect(cellOf(2)).toContain('data-step="0"');
    expect(cellOf(2)).toContain("is-loss");
    // Its key shows only the red half, worst to best, with no gains or 0.
    expect(text(html)).toContain("Net trophies −120 −10");
    expect(text(html)).toContain("every day a loss");
    expect(html.match(/class="season-heat-ramp"/g)).toHaveLength(1);
  });

  it("ranks a day with no battles but an automatic loss, not as a shield day", () => {
    const quiet = { attacks: 0, defenses: 0, offense: 0, defense: 0 };
    const lost = day(3, -304, quiet);
    const cells = heatCells(
      season([day(1, 40), day(2, 0, { ...quiet, kind: "shield" }), lost]),
      "net",
    );
    expect(cells.slice(0, 3).map((cell) => [cell.kind, cell.shade])).toEqual([
      ["played", 1],
      ["shield", null],
      ["played", 0],
    ]);
  });
});

type Element = ReactElement<Record<string, unknown> & { children?: ReactNode }>;
function walk(node: ReactNode, found: Element[] = []): Element[] {
  if (Array.isArray(node)) node.forEach((child) => walk(child, found));
  else if (node && typeof node === "object" && "props" in node) {
    const element = node as Element;
    found.push(element);
    if (typeof element.type === "function")
      walk((element.type as (props: unknown) => ReactNode)(element.props), found);
    walk(element.props.children, found);
  }
  return found;
}

describe("Season chart on touch screens", () => {
  const points = [
    { day: 1, value: 5000 },
    { day: 2, value: 5100 },
    { day: 3, value: 5150 },
  ];
  const chart = (tip: number | null, onTip: (day: number | null) => void) =>
    walk(
      SeasonChart({
        dayCount: 28,
        points,
        mode: "trophies",
        tip,
        onTip,
        days: points.map((point) => ({
          ...day(point.day, 0),
          endTrophies: point.value,
          rank: 10,
        })) as unknown as SeasonData["days"],
      }),
    );
  const dayButton = (elements: Element[], number: number) =>
    elements.find((element) => element.props["aria-label"] === `Day ${number}: 5,150`)!;

  it("keeps a tapped day's readout open when the tap also points at it", () => {
    const onTip = vi.fn();
    (dayButton(chart(null, onTip), 3).props.onMouseEnter as () => void)();
    expect(onTip).toHaveBeenLastCalledWith(3);
    (dayButton(chart(3, onTip), 3).props.onClick as () => void)();
    expect(onTip).toHaveBeenLastCalledWith(3);
    (dayButton(chart(3, onTip), 3).props.onKeyDown as (event: { key: string }) => void)({
      key: "Escape",
    });
    expect(onTip).toHaveBeenLastCalledWith(null);
  });

  it("gives each day a full-height column and thumb-sized day steps", () => {
    const elements = chart(null, vi.fn());
    const columns = elements.filter((element) => element.props.className === "graph-hit");
    expect(columns).toHaveLength(3);
    // At 320px wide the 760-unit chart draws each column over 44px tall.
    for (const column of columns)
      expect(((column.props.height as number) * 320) / 760).toBeGreaterThanOrEqual(44);
    const steps = elements.filter((element) =>
      ["Earlier day", "Later day"].includes(element.props["aria-label"] as string),
    );
    expect(steps).toHaveLength(2);
    for (const button of steps) {
      const style = button.props.style as { minWidth: string; minHeight: string };
      expect(parseFloat(style.minWidth) * 16).toBeGreaterThanOrEqual(44);
      expect(parseFloat(style.minHeight) * 16).toBeGreaterThanOrEqual(44);
    }
  });

  it("keeps the tapped day when focus moves to Earlier, then steps back from it", () => {
    const onTip = vi.fn();
    const elements = chart(3, onTip);
    const earlier = { label: "Earlier day" };
    const outside = { label: "page" };
    const group = { contains: (node: unknown) => node === earlier };
    const currentTarget = {
      closest: (selector: string) => (selector === ".day-group" ? group : null),
    };
    const point = dayButton(elements, 3);
    (point.props.onBlur as (event: object) => void)({
      currentTarget,
      relatedTarget: earlier,
    });
    (point.props.onMouseLeave as (event: object) => void)({
      currentTarget,
      relatedTarget: earlier,
    });
    expect(onTip).not.toHaveBeenCalled();
    const button = elements.find(
      (element) => element.props["aria-label"] === "Earlier day",
    )!;
    (button.props.onClick as () => void)();
    expect(onTip).toHaveBeenLastCalledWith(2);
    (point.props.onBlur as (event: object) => void)({
      currentTarget,
      relatedTarget: outside,
    });
    expect(onTip).toHaveBeenLastCalledWith(null);
  });

  it("steps the readout through every day with Earlier and Later", () => {
    const step = (tip: number | null, label: string) => {
      const onTip = vi.fn();
      const button = chart(tip, onTip).find(
        (element) => element.props["aria-label"] === label,
      )!;
      if (button.props.disabled) return "disabled";
      (button.props.onClick as () => void)();
      return onTip.mock.calls.at(-1)![0] as number | null;
    };
    expect(step(null, "Earlier day")).toBe(3);
    expect(step(3, "Earlier day")).toBe(2);
    expect(step(2, "Earlier day")).toBe(1);
    expect(step(1, "Earlier day")).toBe("disabled");
    expect(step(1, "Later day")).toBe(2);
    expect(step(2, "Later day")).toBe(3);
    expect(step(3, "Later day")).toBe("disabled");
  });
});
