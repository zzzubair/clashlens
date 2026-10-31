import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { TrophyLine } from "../../app/components/TrophyLine";
import type { PlayerPage, RankedDaySummary } from "../../app/lib/contracts";
import { worstCasePlayer } from "../fixtures/player-worst-case";

const DAY = 86_400_000;
const FIRST = Date.parse("2026-10-01T05:00:00Z");
// Day 4 of the line, an hour after its Reset.
const NOW = FIRST + 3 * DAY + 3_600_000;

// Day `index` of the line, starting at `start` trophies; the next saved day's
// start is this day's EOD.
function day(index: number, start: number): RankedDaySummary {
  const begins = FIRST + index * DAY;
  return {
    dayNumber: index + 1,
    label: "Legend day",
    period: `${new Date(begins).toISOString()} – ${new Date(begins + DAY).toISOString()}`,
    state: begins + DAY > NOW ? "Live" : "Complete",
    startTrophies: start,
    offense: { attacks: 0, threeStars: null, trophyGain: 0 },
    defense: { defenses: 0, threeStarsAgainst: null, trophyLoss: 0 },
    trophyChange: null,
    offenseEvents: [],
    defenseEvents: [],
    completeness: { state: "complete", reason: "" },
    battlesComplete: true,
    uncertainty: [],
  };
}

function line(days: RankedDaySummary[]) {
  const newestFirst = [...days].reverse();
  const player: PlayerPage = {
    ...worstCasePlayer("#2PP", NOW),
    currentDay: newestFirst.find((saved) => saved.state === "Live") ?? null,
    recentDays: newestFirst,
    seasonDays: newestFirst,
  };
  const html = renderToStaticMarkup(
    createElement(TrophyLine, {
      history: newestFirst.map((saved) => ({ day: saved })),
      player,
      now: NOW,
      current: 5_100,
    }),
  );
  const path = (name: string) =>
    html.match(new RegExp(`class="${name}" d="([^"]*)"`))?.[1] ?? null;
  return {
    html,
    solid: path("trophy-line-path"),
    dashed: path("trophy-line-path trophy-line-gap"),
  };
}

describe("Season trophy line", () => {
  it("keeps a day with no saved EOD in its place, bridged by a dashed line", () => {
    // Day 3 was never saved, so Day 2 has no EOD: Day 1's end, then a dashed
    // stretch across Days 2 and 3 to now, the end of Day 4.
    const { solid, dashed } = line([day(0, 5_000), day(1, 5_040), day(3, 5_080)]);
    expect(solid).toMatch(/^M0\.0 [\d.]+ L80\.0 [\d.]+ M240\.0 [\d.]+ L320\.0 [\d.]+$/);
    expect(dashed).toMatch(/^M80\.0 [\d.]+ L240\.0 [\d.]+$/);
  });

  it("draws every day solid when each has an EOD", () => {
    const { solid, dashed } = line([
      day(0, 5_000),
      day(1, 5_040),
      day(2, 5_020),
      day(3, 5_080),
    ]);
    expect(solid?.match(/M/g)).toHaveLength(4);
    expect(dashed).toBe("");
  });

  it("labels a round trophy scale down one side and lists each Reset for screen readers", () => {
    const { html } = line([day(0, 5_000), day(1, 5_040), day(2, 5_020), day(3, 5_080)]);
    const axis = html.match(/class="trophy-line-axis"[^>]*>(.*?)<\/div>/)?.[1] ?? "";
    expect([...axis.matchAll(/>([\d,]+)</g)].map(([, tick]) => tick)).toEqual([
      "5,000",
      "5,025",
      "5,050",
      "5,075",
      "5,100",
    ]);
    const resets =
      html.match(/aria-label="Trophies at each Reset">(.*?)<\/ol>/)?.[1] ?? "";
    expect([...resets.matchAll(/<li>(.*?)<\/li>/g)].map(([, item]) => item)).toEqual([
      "1 Oct start: 5,000",
      "1 Oct EOD: 5,040, +40",
      "2 Oct EOD: 5,020, −20",
      "3 Oct EOD: 5,080, +60",
      "Now: 5,100, +20",
    ]);
  });
});
