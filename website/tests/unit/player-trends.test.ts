import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { PlayerTrends } from "../../app/components/PlayerTrends";
import type { RankedDaySummary } from "../../app/lib/contracts";

const DAY_MS = 86_400_000;
const RESET = Date.parse("2026-10-05T05:00:00Z");

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

function cards(days: RankedDaySummary[], now = RESET + 3 * DAY_MS) {
  const html = renderToStaticMarkup(createElement(PlayerTrends, { days, now }));
  return {
    html,
    seven: html.split("<h3>Last 7 days</h3>")[1].split("</article>")[0],
    fourteen: html.split("<h3>Last 14 days</h3>")[1].split("</article>")[0],
  };
}

describe("player trophy trends", () => {
  it("adds daily changes across the 5 October Season reset without a fake drop", () => {
    const now = RESET + 3 * DAY_MS;
    const days = Array.from({ length: 14 }, (_, i) => day(now - (i + 1) * DAY_MS));
    // A real daily loss stays negative even across a Season boundary.
    days[0].trophyChange = -20;
    const result = cards(days, now);
    expect(result.seven).toContain("<dd>+40</dd>");
    expect(result.seven).toContain("<dd>7 of 7</dd>");
    expect(result.fourteen).toContain("<dd>+110</dd>");
    expect(result.fourteen).toContain("<dd>14 of 14</dd>");
    expect(result.html).toContain("the Season reset to 5,000 never counts as a drop");
  });

  it("counts calendar days, excluding today, future days and older history", () => {
    const now = RESET + 3 * DAY_MS;
    const result = cards([
      day(now, 500),
      day(now + DAY_MS, 500),
      day(now - 7 * DAY_MS, 15),
      day(now - 8 * DAY_MS, 20),
      day(now - 14 * DAY_MS, 30),
      day(now - 15 * DAY_MS, 500),
    ]);
    expect(result.seven).toContain("<dd>+15</dd>");
    expect(result.seven).toContain("<dd>1 of 7</dd>");
    expect(result.fourteen).toContain("<dd>+65</dd>");
    expect(result.fourteen).toContain("<dd>3 of 14</dd>");
  });

  it("moves the window at 05:00 UTC, including on the Season reset", () => {
    const days = [day(RESET - DAY_MS, 30), day(RESET - 8 * DAY_MS, 80)];
    expect(cards(days, RESET - 1).seven).toContain("<dd>+80</dd>");
    expect(cards(days, RESET).seven).toContain("<dd>+30</dd>");
  });

  it("leaves incomplete, uncertain and unknown totals out, and reports coverage", () => {
    const result = cards([
      { ...day(RESET), completeness: { state: "partial", reason: "Missing battles" } },
      { ...day(RESET + DAY_MS), completeness: { state: "uncertain", reason: "Dispute" } },
      day(RESET + 2 * DAY_MS, null),
      day(RESET - DAY_MS, -15),
    ]);
    expect(result.seven).toContain("<dd>-15</dd>");
    expect(result.seven).toContain("<dd>1 of 7</dd>");
    expect(result.fourteen).toContain("<dd>1 of 14</dd>");
  });

  it("shows unavailable for no counted days, and distinguishes a proven zero", () => {
    for (const days of [[], [day(RESET, null)]]) {
      const result = cards(days);
      expect(result.seven).toContain("<dd>Unavailable</dd>");
      expect(result.seven).toContain("<dd>0 of 7</dd>");
      expect(result.fourteen).toContain("<dd>Unavailable</dd>");
      expect(result.fourteen).toContain("<dd>0 of 14</dd>");
    }
    const result = cards([day(RESET, 0)]);
    expect(result.seven).toContain("<dd>0</dd>");
    expect(result.seven).toContain("<dd>1 of 7</dd>");
  });

  it("does not count duplicate or malformed days", () => {
    const saved = day(RESET);
    const result = cards([
      saved,
      saved,
      { ...day(RESET + DAY_MS), period: "unknown" },
      day(RESET + 1000),
      { ...day(RESET + 2 * DAY_MS), period: "2026-10-07T05:00:00Z" },
    ]);
    expect(result.seven).toContain("<dd>+10</dd>");
    expect(result.seven).toContain("<dd>1 of 7</dd>");
  });
});
