import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { SeasonOverviewCard } from "../../app/components/SeasonCards";
import {
  seasonOverview,
  type SeasonData,
  type SeasonDay,
} from "../../app/lib/dashboard-cards";

const text = (html: string) => html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");

const season = (days: SeasonDay[], today: number | null = 9): SeasonData => ({
  today,
  startMs: Date.UTC(2026, 9, 1, 5),
  days,
});
const day = (
  number: number,
  endTrophies: number | null,
  rank: number | null,
  net: number | null,
): SeasonDay => ({
  day: number,
  attacks: 8,
  defenses: 8,
  offense: 300,
  defense: net === null ? null : net - 300,
  net,
  triples: 6,
  held: 4,
  endTrophies,
  rank,
  hits: [],
});

describe("Season overview rows", () => {
  it("lists every finished day newest first, with places moved since the day before", () => {
    // Read on Day 9: Day 7 never saved, Day 5's board left the player out.
    const rows = seasonOverview(
      season([
        day(1, 5044, 900, 44),
        day(2, 5114, 700, 70),
        day(3, 5114, 700, 0),
        day(4, 5122, 812, 8),
        day(5, 5186, null, 64),
        day(6, 5188, 520, 2),
        day(8, 5324, 318, 86),
      ]),
    );
    expect(rows.map(({ day }) => day)).toEqual([8, 7, 6, 5, 4, 3, 2, 1]);
    expect(rows).toEqual([
      // Day 7 has no rank, so Day 8 has nothing to move from.
      { day: 8, endTrophies: 5324, rank: 318, moved: null, net: 86 },
      { day: 7, endTrophies: null, rank: null, moved: null, net: null },
      // Day 5 has no rank either side of it to measure from.
      { day: 6, endTrophies: 5188, rank: 520, moved: null, net: 2 },
      { day: 5, endTrophies: 5186, rank: null, moved: null, net: 64 },
      // A fall is negative, a climb positive, level is 0.
      { day: 4, endTrophies: 5122, rank: 812, moved: -112, net: 8 },
      { day: 3, endTrophies: 5114, rank: 700, moved: 0, net: 0 },
      { day: 2, endTrophies: 5114, rank: 700, moved: 200, net: 70 },
      // Day 1 has no day before it this Season.
      { day: 1, endTrophies: 5044, rank: 900, moved: null, net: 44 },
    ]);
  });

  it("has no rows before the first Reset or when the Season is unknown", () => {
    expect(seasonOverview(season([], 1))).toEqual([]);
    expect(seasonOverview(season([], null))).toEqual([]);
    // Day 28 is the Season's last: 27 finished days, never more than 28.
    expect(seasonOverview(season([], 28))).toHaveLength(27);
    expect(seasonOverview(season([], 40))).toHaveLength(28);
  });
});

describe("Season overview card", () => {
  const full = season(
    Array.from({ length: 11 }, (_, index) =>
      day(index + 1, 5000 + 20 * (index + 1), 1200 - 100 * index, 20),
    ),
    12,
  );

  it("shows today, then the last 7 days, with Show all for the rest", () => {
    const html = renderToStaticMarkup(createElement(SeasonOverviewCard, { data: full }));
    const shown = text(html);
    expect(shown).toContain("Season overview");
    expect(shown).toContain("Day Trophies Rank Net 12 Today · ends at Reset 11 5,220");
    // Day 11 climbed from #300 to #200; the move is read out in words.
    expect(shown).toContain("11 5,220 #200 ▲ 100 , up 100 since the day before +20");
    expect(html).toContain('class="rival-moved positive"');
    expect(shown).toContain("5 5,100 #800");
    expect(shown).not.toContain("4 5,080");
    expect(html).toMatch(/<button[^>]*class="show-all"[^>]*aria-expanded="false"/);
    expect(shown).toContain("Show all 11 days");
  });

  it("shows – for an unknown rank, trophies or net, and no Show all for a short Season", () => {
    const data = season([day(1, 5040, 1000, 40), day(2, null, null, null)], 4);
    const shown = text(renderToStaticMarkup(createElement(SeasonOverviewCard, { data })));
    expect(shown).toContain("4 Today · ends at Reset 3 – – – 2 – – – 1 5,040 #1,000 +40");
    expect(shown).not.toContain("Show all");
    expect(shown).not.toContain("since the day before");
  });

  it("shows today on Day 1, saying results start after the first Reset", () => {
    const render = (today: number | null) =>
      text(
        renderToStaticMarkup(
          createElement(SeasonOverviewCard, { data: season([], today) }),
        ),
      );
    expect(render(1)).toContain("1 Today · ends at Reset Results start after the Season");
    // With no Season known there is no today to show.
    expect(render(null)).not.toContain("Today · ends at Reset");
    expect(render(null)).toContain("Results start after the Season");
  });
});
