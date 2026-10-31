import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

import { HeatMapCard } from "../../app/components/SeasonCards";
import { heatCells, isShieldDay, type SeasonData } from "../../app/lib/dashboard-cards";

const text = (html: string) => html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");

// Day 1 began at the 05:00 UTC Reset on 1 October.
const START = Date.UTC(2026, 9, 1, 5);
const season = (days: SeasonData["days"], today = 9): SeasonData => ({
  today,
  startMs: START,
  days,
});
const day = (number: number, net: number, changes = {}) => ({
  day: number,
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
      day(3, 0, { attacks: 0, defenses: 0, offense: 0, defense: 0, endTrophies: 5114 }),
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
      /--heat:var\(--cl-heat-4\)[^>]*><span[^>]*>D<span>ay <\/span>8<\/span><button[^>]*aria-label="Day 8: \+86"/,
    );
    expect(html).toMatch(
      /--heat:var\(--cl-heat-0\)[^>]*><button[^>]*aria-label="Day 6: \+2"/,
    );
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
        day(4, 0, { attacks: 0, defenses: 0 }),
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

  it("ranks a day with no battles but an automatic loss, not as a shield day", () => {
    const quiet = { attacks: 0, defenses: 0, offense: 0, defense: 0 };
    const lost = day(3, -304, quiet);
    expect([lost, day(2, 0, quiet)].map((d) => isShieldDay(d))).toEqual([false, true]);
    expect(isShieldDay({ ...day(4, 0, quiet), net: null })).toBe(false);
    const cells = heatCells(season([day(1, 40), day(2, 0, quiet), lost]), "net");
    expect(cells.slice(0, 3).map((cell) => [cell.kind, cell.shade])).toEqual([
      ["played", 1],
      ["shield", null],
      ["played", 0],
    ]);
  });
});
