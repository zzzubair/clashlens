import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { PlayerTrends } from "../../app/components/PlayerTrends";
import type { PlayerPage, RankedDaySummary } from "../../app/lib/contracts";

const DAY_MS = 86_400_000;
// The Season that starts on 5 October 2026.
const RESET = Date.parse("2026-10-05T05:00:00Z");
const LATER = RESET + 20 * DAY_MS;

function day(start: number, change: number | null = 10): RankedDaySummary {
  return {
    dayNumber: start < RESET ? 28 : 1,
    label: "Ranked day",
    period: `${new Date(start).toISOString()} – ${new Date(start + DAY_MS).toISOString()}`,
    state: "Complete",
    startTrophies: start < RESET ? 6200 : 5000,
    offense: { attacks: 8, threeStars: 8, trophyGain: 320 },
    defense: { defenses: 8, threeStarsAgainst: 0, trophyLoss: 310 },
    trophyChange: change,
    completeness: { state: "complete", reason: "Complete evidence." },
    uncertainty: [],
    offenseEvents: [],
    defenseEvents: [],
  };
}

function render(days: RankedDaySummary[], now: number) {
  const player = { season: null, recentDays: days } as unknown as PlayerPage;
  return renderToStaticMarkup(createElement(PlayerTrends, { player, now }));
}

function cards(days: RankedDaySummary[], now = LATER) {
  const html = render(days, now);
  const card = (title: string) =>
    html.split(`<h3>${title}</h3>`)[1].split("</article>")[0];
  return {
    html,
    card,
    get seven() {
      return card("Last 7 days");
    },
    get fourteen() {
      return card("Last 14 days");
    },
  };
}

describe("player trophy trends", () => {
  it("adds the finished days of the last 7 and 14 days, with no explanation paragraph", () => {
    const days = Array.from({ length: 14 }, (_, i) => day(LATER - (i + 1) * DAY_MS));
    days[0].trophyChange = -20;
    const result = cards(days);
    expect(result.seven).toContain("<dd>+40</dd>");
    expect(result.seven).toContain("<dd>7 of 7</dd>");
    expect(result.fourteen).toContain("<dd>+110</dd>");
    expect(result.fourteen).toContain("<dd>14 of 14</dd>");
    expect(result.html).not.toContain("<p");
  });

  it("uses only this Season's finished days early on and says how many so far", () => {
    const now = RESET + 3 * DAY_MS + 1000;
    const days = [
      day(RESET - DAY_MS, 500),
      day(RESET - 2 * DAY_MS, 500),
      day(RESET, 10),
      day(RESET + DAY_MS, 20),
      day(RESET + 2 * DAY_MS, 30),
    ];
    const result = cards(days, now);
    const seven = result.card("Last 7 days (3 so far)");
    expect(seven).toContain("<dd>+60</dd>");
    expect(seven).toContain("<dd>3 of 3</dd>");
    expect(result.card("Last 14 days (3 so far)")).toContain("<dd>+60</dd>");
  });

  it("shows nothing before this Season has a finished day", () => {
    expect(render([day(RESET - DAY_MS, 30)], RESET)).toBe("");
    expect(render([day(RESET - DAY_MS, 30)], RESET + DAY_MS - 1)).toBe("");
  });

  it("counts calendar days, excluding today, future days and older history", () => {
    const result = cards([
      day(LATER, 500),
      day(LATER + DAY_MS, 500),
      day(LATER - 7 * DAY_MS, 15),
      day(LATER - 8 * DAY_MS, 20),
      day(LATER - 14 * DAY_MS, 30),
      day(LATER - 15 * DAY_MS, 500),
    ]);
    expect(result.seven).toContain("<dd>+15</dd>");
    expect(result.seven).toContain("<dd>1 of 7</dd>");
    expect(result.fourteen).toContain("<dd>+65</dd>");
    expect(result.fourteen).toContain("<dd>3 of 14</dd>");
  });

  it("moves the window at 05:00 UTC", () => {
    const days = [day(LATER - DAY_MS, 30), day(LATER - 8 * DAY_MS, 80)];
    expect(cards(days, LATER - 1).seven).toContain("<dd>+80</dd>");
    expect(cards(days, LATER).seven).toContain("<dd>+30</dd>");
  });

  it("leaves incomplete, uncertain and unknown totals out, and reports coverage", () => {
    const result = cards([
      { ...day(LATER - 4 * DAY_MS), completeness: { state: "partial", reason: "Gap" } },
      {
        ...day(LATER - 3 * DAY_MS),
        completeness: { state: "uncertain", reason: "Dispute" },
      },
      day(LATER - 2 * DAY_MS, null),
      day(LATER - DAY_MS, -15),
    ]);
    expect(result.seven).toContain("<dd>-15</dd>");
    expect(result.seven).toContain("<dd>1 of 7</dd>");
    expect(result.fourteen).toContain("<dd>1 of 14</dd>");
  });

  it("shows unavailable for no counted days, and distinguishes a proven zero", () => {
    for (const days of [[], [day(LATER - DAY_MS, null)]]) {
      const result = cards(days);
      expect(result.seven).toContain("<dd>Unavailable</dd>");
      expect(result.seven).toContain("<dd>0 of 7</dd>");
      expect(result.fourteen).toContain("<dd>Unavailable</dd>");
      expect(result.fourteen).toContain("<dd>0 of 14</dd>");
    }
    const result = cards([day(LATER - DAY_MS, 0)]);
    expect(result.seven).toContain("<dd>0</dd>");
    expect(result.seven).toContain("<dd>1 of 7</dd>");
  });

  it("does not count duplicate or malformed days", () => {
    const start = LATER - 3 * DAY_MS;
    const saved = day(start);
    const result = cards([
      saved,
      saved,
      { ...day(start + DAY_MS), period: "unknown" },
      day(start + 1000),
      { ...day(start + 2 * DAY_MS), period: new Date(start + 2 * DAY_MS).toISOString() },
    ]);
    expect(result.seven).toContain("<dd>+10</dd>");
    expect(result.seven).toContain("<dd>1 of 7</dd>");
  });
});
