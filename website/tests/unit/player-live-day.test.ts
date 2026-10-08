import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  StaticRouterProvider,
} from "react-router";

import type { PlayerPage, RankedDaySummary } from "../../app/lib/contracts";
import PlayerRoute from "../../app/routes/player";

const TAG = "#2PP";
const WAITING = [
  "missing_end_battle_log_baseline",
  "automatic_defense_basis_unavailable",
  "missing_end_baseline",
];

const TODAY: RankedDaySummary = {
  dayNumber: 4,
  label: "Ranked day",
  period: "2026-10-08T05:00:00Z – 2026-10-09T05:00:00Z",
  state: "Live",
  startTrophies: 6000,
  offense: { attacks: 0, threeStars: 0, trophyGain: 0 },
  defense: { defenses: 0, threeStarsAgainst: 0, trophyLoss: 0 },
  trophyChange: null,
  battlesComplete: true,
  offenseEvents: [],
  defenseEvents: [],
  completeness: { state: "partial", reason: WAITING.join("; ") },
  uncertainty: WAITING,
};

// Renders the page as the server would, with the page's clock at `now`.
function page(
  codes: string[],
  now = "2026-10-08T12:00:00Z",
  endedDays: RankedDaySummary[] = [],
) {
  const today = {
    ...TODAY,
    completeness: { state: "partial" as const, reason: codes.join("; ") },
    uncertainty: codes,
  };
  const player = {
    kind: "player-page",
    tag: TAG,
    trackingState: "tracking",
    profile: {
      tag: TAG,
      name: "Nova",
      clan: "Example",
      trophies: 6000,
      freshness: { state: "fresh", observedAt: now, ageSeconds: 0 },
      confidence: "high",
      coverage: "partial",
      eligibility: "legend-i",
    },
    season: null,
    currentDay: today,
    recentDays: [today, ...endedDays],
    seasonDays: [],
    dataQuality: [
      { code: "partial", label: "Incomplete ranked-day data", detail: codes.join("; ") },
    ],
    provenance: {
      source: "api_player_daily_logs",
      observedAt: now,
      freshness: "fresh",
      confidence: "high",
      coverage: "partial",
      version: "v1",
    },
  } satisfies PlayerPage;
  const data = {
    requestedTag: TAG,
    player,
    error: null,
    refreshStatus: null,
    refreshError: null,
    noJsIdempotencyKey: "test-idempotency-key",
    lookup: { tag: TAG, state: "tracking" },
    lookupError: null,
    seasons: [],
    selectedSeason: null,
    historical: null,
    historicalError: null,
  };
  const handler = createStaticHandler([
    { path: "/players/:tag", Component: PlayerRoute, loader: () => data },
  ]);
  return handler
    .query(new Request("https://clashlens.example/players/%232PP"))
    .then((context) => {
      if (context instanceof Response) throw new Error("unexpected route response");
      const router = createStaticRouter(handler.dataRoutes, context);
      return renderToString(createElement(StaticRouterProvider, { router, context }))
        .split("<script>")[0]
        .replaceAll("<!-- -->", "");
    });
}

const note = (html: string) =>
  html.split('id="season-days-title"')[1].split('class="legend-days"')[0];
const todayEntry = (html: string) =>
  html.split('id="legend-day-2026-10-08"')[1].split("</details>")[0];

describe("today's Legend day wording", () => {
  it("keeps the previous Season's days out of recent trophy trends", async () => {
    const previousSeasonDay: RankedDaySummary = {
      ...TODAY,
      dayNumber: 28,
      period: "2026-10-04T05:00:00Z – 2026-10-05T05:00:00Z",
      state: "Complete",
      trophyChange: 35,
      completeness: { state: "complete", reason: "Complete evidence." },
      uncertainty: [],
    };
    const html = await page(WAITING, "2026-10-08T12:00:00Z", [previousSeasonDay]);
    const trends = html.split('id="player-trends-title"')[1].split("</section>")[0];
    expect(trends).toContain("Last 7 days (3 so far)");
    expect(trends).toContain("Last 14 days (3 so far)");
    expect(trends).not.toContain("+35");
    expect(trends.match(/<dd>0 of 3<\/dd>/g)).toHaveLength(2);
    expect(html).not.toContain('id="legend-day-2026-10-04"');
  });

  it("shows a normal wait for Reset as a day in progress, not a fault", async () => {
    const html = await page(WAITING);
    // The day's In progress badge says it; no extra note above the log.
    expect(note(html)).not.toContain("<strong>");
    expect(html).not.toContain("Incomplete ranked-day data");
    expect(html).not.toContain("could not be calculated");
    const entry = todayEntry(html);
    expect(entry).toContain("In progress");
    expect(entry).toContain("Ending evidence arrives after Reset.");
    expect(entry).toContain(
      "Any automatic defense loss needs complete records for this Legend day and the previous one.",
    );
    // Each sentence appears once on the page.
    expect(html.split("Ending evidence arrives after Reset.")).toHaveLength(2);
    expect(html.split("Any automatic defense loss needs")).toHaveLength(2);
  });

  it("keeps a real problem on today visible as a caution", async () => {
    for (const [code, text] of [
      ["battle_log_row_gap", "Part of a battle log reply could not be read."],
      ["missing_start_baseline", "Trophies at the start of this day were not recorded."],
      ["end_baseline_incomplete", "The end-of-day trophy reading is incomplete."],
      ["new_unknown_code", "Some daily evidence is unavailable."],
    ]) {
      const html = await page([...WAITING, code]);
      expect(note(html)).toContain(
        `<strong>Incomplete ranked-day data:</strong> ${text}`,
      );
      expect(html).not.toContain("Day in progress");
      expect(todayEntry(html)).toContain(text);
      expect(todayEntry(html)).toContain("Ending evidence arrives after Reset.");
    }
  });

  it("stops calling the day in progress once the page's clock passes Reset", async () => {
    const html = await page(WAITING, "2026-10-09T05:00:00Z");
    expect(note(html)).toContain(
      "<strong>Day ended:</strong> This Legend day has ended. Updated results are not on this page yet.",
    );
    expect(html).not.toContain("Day in progress");
    const entry = todayEntry(html);
    expect(entry).not.toContain("In progress");
    expect(entry).not.toContain("Ending evidence arrives after Reset.");
    expect(entry).toContain("Uncertain");
    expect(html).not.toContain('id="legend-day-2026-10-08" open=""');
  });

  it("keeps a real problem visible and its day open after Reset", async () => {
    const html = await page([...WAITING, "battle_log_row_gap"], "2026-10-09T05:00:00Z");
    expect(note(html)).toContain(
      "<strong>Incomplete ranked-day data:</strong> Part of a battle log reply could not be read.",
    );
    expect(html).not.toContain("Day ended");
    expect(html).not.toContain("Day in progress");
    expect(html).toContain('id="legend-day-2026-10-08" open=""');
    const entry = todayEntry(html);
    expect(entry).not.toContain("In progress");
    expect(entry).toContain("Part of a battle log reply could not be read.");
  });

  it("shows each ended day's Reset rank and leaves today's until Reset", async () => {
    const ended = (day: number, resetRank: number | null): RankedDaySummary => ({
      ...TODAY,
      dayNumber: day,
      period: `2026-10-0${day}T05:00:00Z – 2026-10-0${day + 1}T05:00:00Z`,
      state: "Complete",
      resetRank,
    });
    const html = await page(WAITING, undefined, [ended(7, 1234), ended(6, null)]);
    const rank = (key: string) =>
      html
        .split(`id="legend-day-${key}"`)[1]
        .split("<small>Reset rank</small>")[1]
        .split("</strong>")[0];
    expect(rank("2026-10-07")).toMatch(/>1,234$/);
    expect(rank("2026-10-06")).toMatch(/>Unknown$/);
    expect(rank("2026-10-08")).toMatch(/>After Reset$/);
  });
});
