import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  StaticRouterProvider,
} from "react-router";
import { describe, expect, it } from "vitest";
import type { PlayerPage, RankedDaySummary } from "../../app/lib/contracts";
import PlayerRoute, { loader as playerLoader } from "../../app/routes/player";
import {
  CALENDAR,
  UNKNOWN_END,
  VERIFIED,
  logFacts,
  PLAYER,
  SAVED_DAY,
  SUMMARY,
  SEASON,
  SEASONS,
  TAG,
} from "../fixtures/player-route-data";

async function renderRoute(
  data: Awaited<ReturnType<typeof playerLoader>>,
  search = "?season=missing",
) {
  const handler = createStaticHandler([
    { path: "/players/:tag", Component: PlayerRoute, loader: () => data },
  ]);
  const context = await handler.query(
    new Request(`https://clashlens.example/players/%232PP${search}`),
  );
  if (context instanceof Response) throw new Error("unexpected route response");
  const router = createStaticRouter(handler.dataRoutes, context);
  return renderToString(createElement(StaticRouterProvider, { router, context }));
}

describe("player day honesty", () => {
  const ENDED_DAY: RankedDaySummary = {
    ...SAVED_DAY,
    dayNumber: 3,
    start: "2026-10-02T05:00:00Z",
    end: "2026-10-03T05:00:00Z",
    seasonId: null,
    presentation: {
      ...VERIFIED,
      status: "uncertain",
      battle_net: 26,
      reason_codes: ["missing_end_baseline"],
    },
    displayEnd: UNKNOWN_END,
    state: "Partial",
    offense: { attacks: 8, threeStars: 6, trophyGain: 310 },
    defense: { defenses: 8, threeStarsAgainst: 2, trophyLoss: 284 },
    trophyChange: null,
    offenseEvents: [],
    defenseEvents: [],
    completeness: { state: "partial", reason: "missing_end_baseline" },
    uncertainty: ["missing_end_baseline"],
  };
  const TODAY: RankedDaySummary = {
    ...ENDED_DAY,
    dayNumber: 4,
    start: "2026-10-03T05:00:00Z",
    end: "2026-10-04T05:00:00Z",
    seasonId: null,
    presentation: {
      ...VERIFIED,
      status: "in_progress",
      is_current: true,
      battle_net: 0,
      reason_codes: ["missing_end_battle_log_baseline", "missing_start_baseline"],
    },
    displayEnd: UNKNOWN_END,
    state: "Live",
    offense: { attacks: 0, threeStars: 0, trophyGain: 0 },
    defense: { defenses: 0, threeStarsAgainst: 0, trophyLoss: 0 },
    uncertainty: ["missing_end_battle_log_baseline", "missing_start_baseline"],
  };

  function page(days: RankedDaySummary[], extra: Partial<PlayerPage> = {}) {
    return renderRoute(
      {
        requestedTag: TAG,
        player: {
          ...PLAYER,
          calendar: {
            ...CALENDAR,
            as_of: "2026-10-03T12:00:00Z",
            season_end: "2026-10-05T05:00:00Z",
          },
          recentDays: days,
          seasonDays: days,
          ...logFacts(
            days,
            days.map((day) => day.dayNumber!),
          ),
          ...extra,
        },
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
      },
      "",
    ).then((html) => html.split("<script>")[0].replaceAll("<!-- -->", ""));
  }

  // A day ends at its own closing tag, not its status note's, which the
  // battle switch follows.
  function dayHtml(html: string, key: string) {
    return html
      .split(`id="legend-day-${key}"`)[1]
      .split(/<\/div><\/details>(?!<\/div><fieldset)/)[0];
  }

  const net = (value: string) =>
    new RegExp(`<small>Trophy change</small><strong class="[^"]+">${value}</strong>`);

  it("keeps an unknown daily net unknown while showing recorded battle net", async () => {
    const zero = { ...ENDED_DAY, start: SAVED_DAY.start, trophyChange: 0 };
    const html = await page([ENDED_DAY, zero]);
    const ended = dayHtml(html, "2026-10-02");
    expect(ended).toMatch(net("Unknown"));
    expect(ended).toContain("Uncertain");
    expect(ended).toContain("<span>+26 from battles</span>");
    expect(ended).toContain("Trophies at the end of this day were not recorded.");
    expect(ended).toContain("8 recorded");
    expect(ended).not.toContain("missing_end_baseline");
    expect(dayHtml(html, "2026-09-07")).toMatch(net("0"));
  });

  it("does not invent net zero for a current day with no recorded battles", async () => {
    const html = await page([TODAY], { currentDay: TODAY });
    const today = dayHtml(html, "2026-10-03");
    expect(today).toContain("In progress");
    expect(today).toMatch(net("Unknown"));
    expect(today).toContain("<span>0 from battles</span>");
    expect(today).toContain("Ending evidence arrives after Reset.");
    expect(today).toContain("Trophies at the start of this day were not recorded.");
    expect(today).toContain(">8 attacks not recorded yet</li>");
    expect(today).toContain(">8 defenses not recorded yet</li>");
    expect(today).not.toContain("not recorded<");
    expect(html).not.toContain('id="legend-day-2026-10-03" open=""');

    const missingDefense = {
      ...TODAY,
      defense: { ...TODAY.defense, trophyLoss: null },
      presentation: { ...TODAY.presentation!, battle_net: null },
    };
    const later = await page([missingDefense], { currentDay: missingDefense });
    expect(dayHtml(later, "2026-10-03")).not.toContain("from battles");
  });

  it("does not label an ended saved Live row as today's result", async () => {
    const ended = {
      ...TODAY,
      trophyChange: 12,
      uncertainty: [],
      presentation: { ...VERIFIED, status: "uncertain" as const },
      displayEnd: UNKNOWN_END,
    };
    for (const currentDay of [null, ENDED_DAY]) {
      const html = await page([ended, ENDED_DAY], { currentDay });
      const row = dayHtml(html, "2026-10-03");
      expect(row).not.toContain("In progress");
      expect(row).toContain("Uncertain");
      expect(row).toContain("Final evidence for this day has not been processed yet.");
      expect(html).not.toContain('id="legend-day-2026-10-03" open=""');
    }
    const html = await page(
      [{ ...ENDED_DAY, presentation: { ...TODAY.presentation!, battle_net: 26 } }],
      { currentDay: ENDED_DAY, calendar: { ...CALENDAR, as_of: "2026-10-02T12:00:00Z" } },
    );
    expect(dayHtml(html, "2026-10-02")).toContain("In progress");
    expect(dayHtml(html, "2026-10-02")).toContain("Ending evidence arrives after Reset.");
  });

  const battles = (id: string, start: string, changes: number[]) =>
    changes.map((trophyChange, slot) => ({
      ...SAVED_DAY.offenseEvents[0],
      battleId: `${id}-${slot}`,
      battleTimestamp: new Date(Date.parse(start) + (slot + 1) * 3_600_000).toISOString(),
      trophyChange,
    }));

  // Prodigi's Day 24: no start-of-day check, but 8 attacks and 8 defenses,
  // so Python saved the net the battles add up to.
  const DAY_24: RankedDaySummary = {
    ...ENDED_DAY,
    dayNumber: 24,
    start: "2026-09-30T05:00:00Z",
    end: "2026-10-01T05:00:00Z",
    seasonId: null,
    presentation: {
      ...VERIFIED,
      status: "calculated",
      battle_net: -11,
      reason_codes: ["missing_start_battle_log_baseline", "missing_start_baseline"],
    },
    displayEnd: { ...UNKNOWN_END, status: "calculated" },
    offense: { attacks: 8, threeStars: 7, trophyGain: 300 },
    defense: { defenses: 8, threeStarsAgainst: 3, trophyLoss: 311 },
    trophyChange: -11,
    battlesComplete: true,
    offenseEvents: battles(
      "a24",
      "2026-09-30T05:00:00Z",
      [40, 40, 40, 40, 40, 40, 40, 20],
    ),
    defenseEvents: battles(
      "d24",
      "2026-09-30T05:00:00Z",
      [-40, -40, -40, -40, -40, -40, -40, -31],
    ),
    uncertainty: ["missing_start_battle_log_baseline", "missing_start_baseline"],
  };

  it("calls a finished day with all 8 attacks and defenses calculated", async () => {
    const row = dayHtml(await page([DAY_24]), "2026-09-30");
    expect(row).toMatch(net("−11"));
    expect(row).toContain("Calculated");
    expect(row).not.toContain("Uncertain");
    expect(row).not.toContain("so far");
    expect(await page([DAY_24])).not.toContain("A Legend day runs from");
    expect(row).toContain("The battle log was not checked at the start of this day.");

    // Without every battle recorded, a saved number with gaps is uncertain.
    const gaps = dayHtml(
      await page([
        {
          ...DAY_24,
          battlesComplete: false,
          presentation: { ...DAY_24.presentation!, status: "uncertain" },
          displayEnd: UNKNOWN_END,
        },
      ]),
      "2026-09-30",
    );
    expect(gaps).toMatch(net("−11"));
    expect(gaps).toContain("Uncertain");
  });

  it("shows today's net so far only when every battle so far is recorded", async () => {
    // Prodigi's Day 27, in progress: 5,412 + 295 - 139 = 5,568 trophies now.
    const day27: RankedDaySummary = {
      ...TODAY,
      dayNumber: 27,
      offense: { attacks: 8, threeStars: 7, trophyGain: 295 },
      defense: { defenses: 4, threeStarsAgainst: 2, trophyLoss: 139 },
      battlesComplete: true,
      presentation: {
        ...TODAY.presentation!,
        battle_net: 156,
        battle_net_complete: 156,
        reason_codes: [
          "missing_end_battle_log_baseline",
          "automatic_defense_basis_unavailable",
        ],
      },
      offenseEvents: battles(
        "a27",
        "2026-10-03T05:00:00Z",
        [40, 40, 40, 40, 40, 40, 40, 15],
      ),
      defenseEvents: battles("d27", "2026-10-03T05:00:00Z", [-40, -40, -40, -19]),
      uncertainty: [
        "missing_end_battle_log_baseline",
        "automatic_defense_basis_unavailable",
      ],
    };
    const today = dayHtml(await page([day27], { currentDay: day27 }), "2026-10-03");
    expect(today).toContain("In progress");
    expect(today).toMatch(
      /<small>Trophy change<\/small><strong class="[^"]+">\+156<\/strong><span>so far<\/span>/,
    );

    const gap = { ...day27, battlesComplete: false };
    const unknown = dayHtml(await page([gap], { currentDay: gap }), "2026-10-03");
    expect(unknown).toMatch(net("Unknown"));
    expect(unknown).not.toContain("so far");
  });

  it("calls a complete day verified only when its reading matched exactly", async () => {
    const complete = {
      ...ENDED_DAY,
      state: "Complete" as const,
      presentation: { ...VERIFIED, status: "calculated" as const, battle_net: 26 },
      displayEnd: { ...UNKNOWN_END, status: "calculated" as const },
      trophyChange: 26,
      completeness: { state: "complete" as const, reason: "Complete" },
      uncertainty: [],
    };
    const row = dayHtml(await page([complete]), "2026-10-02");
    expect(row).toContain("Calculated");
    expect(row).toMatch(net("\\+26"));
    const exact = dayHtml(
      await page([
        {
          ...complete,
          confidence: "exact",
          presentation: { ...VERIFIED, battle_net: 26 },
          displayEnd: { ...UNKNOWN_END, status: "verified" },
        },
      ]),
      "2026-10-02",
    );
    expect(exact).toContain('<span class="day-mark day-mark-verified" title="Verified">');
    expect(exact).toContain("<strong>Verified.</strong>");
  });

  it("labels saved-season days with the same statuses and plain reasons", async () => {
    const entry = {
      dayNumber: 1,
      start: "2026-05-01T05:00:00Z",
      end: "2026-05-02T05:00:00Z",
      seasonId: null,
      presentation: { ...VERIFIED, status: "calculated" as const, battle_net: 26 },
      displayEnd: UNKNOWN_END,
      startTrophies: 6000,
      endTrophies: 6026,
      eodState: "accepted" as const,
      eodChange: 40,
      eodChangeState: "accepted" as const,
      attackGain: 310,
      defenseLoss: 284,
      netChange: 26,
      attacks: 8,
      defenses: 8,
      state: "Complete",
      coverage: "complete",
      hasAdjustment: false,
      adjustmentTotal: null,
      flags: [],
    };
    const html = await renderRoute(
      {
        requestedTag: TAG,
        player: PLAYER,
        error: null,
        refreshStatus: null,
        refreshError: null,
        noJsIdempotencyKey: "test-idempotency-key",
        lookup: { tag: TAG, state: "tracking" },
        lookupError: null,
        seasons: SEASONS,
        selectedSeason: SEASON,
        historical: {
          ...SUMMARY,
          dailyEntries: [
            entry,
            {
              ...entry,
              dayNumber: 2,
              eodState: "provisional" as const,
              eodChange: null,
              eodChangeState: null,
              state: "Partial",
              coverage: "partial",
              presentation: {
                ...VERIFIED,
                status: "uncertain",
                battle_net: 26,
                reason_codes: ["missing_end_baseline", "attack_star_total_mismatch"],
              },
              flags: ["missing_end_baseline", "attack_star_total_mismatch"],
            },
            {
              ...entry,
              dayNumber: 3,
              netChange: null,
              presentation: { ...VERIFIED, status: "uncertain", battle_net: 26 },
              eodState: null,
              flags: [],
            },
            {
              ...entry,
              dayNumber: 4,
              eodChangeState: "provisional" as const,
              presentation: {
                ...VERIFIED,
                status: "uncertain",
                battle_net: 26,
                reason_codes: ["ranked_version_mismatch"],
              },
              flags: ["ranked_version_mismatch"],
            },
            {
              ...entry,
              dayNumber: 5,
              defenseLoss: 0,
              netChange: 0,
              defenses: 9,
              state: "Partial",
              coverage: "partial",
              presentation: {
                ...VERIFIED,
                status: "uncertain",
                battle_net: 310,
                reason_codes: ["defense_count_exceeds_eight"],
              },
              flags: ["defense_count_exceeds_eight"],
            },
            // Prodigi's Day 24: no start-of-day checks, but all 8 of each.
            {
              ...entry,
              dayNumber: 24,
              attackGain: 300,
              defenseLoss: 311,
              netChange: -11,
              state: "Partial",
              coverage: "partial",
              presentation: {
                ...VERIFIED,
                status: "calculated",
                battle_net: -11,
                reason_codes: [
                  "missing_start_battle_log_baseline",
                  "missing_start_baseline",
                ],
              },
              flags: ["missing_start_battle_log_baseline", "missing_start_baseline"],
            },
            {
              ...entry,
              dayNumber: 25,
              state: "Partial",
              coverage: "partial",
              presentation: {
                ...VERIFIED,
                status: "uncertain",
                battle_net: 26,
                reason_codes: ["missing_start_baseline", "perspective_disagreement"],
              },
              flags: ["missing_start_baseline", "perspective_disagreement"],
            },
            {
              ...entry,
              dayNumber: 26,
              attacks: 7,
              state: "Partial",
              coverage: "partial",
              presentation: {
                ...VERIFIED,
                status: "uncertain",
                battle_net: 26,
                reason_codes: ["missing_start_baseline"],
              },
              flags: ["missing_start_baseline"],
            },
          ],
        },
        historicalError: null,
      },
      `?season=${SEASON}`,
    ).then((value) => value.replaceAll("<!-- -->", ""));
    const rows = html.split('label="Daily trophy totals"')[1].split("</details>");
    const text = rows.map((row) => row.replace(/<[^>]*>/g, " ").replace(/\s+/g, " "));
    expect(rows[0]).toContain('Day 1</small><span class="day-mark" title="Calculated');
    // EOD change sits apart from battle net, and is provisional unless its
    // proof is accepted; an unknown proof state is not accepted.
    expect(text[0]).toContain("End of day 6,026 Trophy change +26");
    expect(text[0]).toContain("net +26 EOD change from previous day +40 Attacks");
    expect(rows[1]).toContain('6,026<span class="day-mark" title="Provisional">');
    expect(text[2]).toContain("6,026 (provisional) Trophy change Unknown");
    expect(text[2]).toContain("net +26 EOD change from previous day +40 Attacks");
    expect(rows[3]).toContain('<dd>+40<span class="day-mark" title="Provisional">');
    expect(rows[1]).toContain("Uncertain");
    expect(rows[1]).toContain("Trophies at the end of this day were not recorded.");
    expect(rows[1]).toContain(
      "Recorded attacks do not match the day&#x27;s attack count.",
    );
    expect(rows[1]).not.toContain("missing_end_baseline");
    expect(rows[2]).toContain("Uncertain");
    expect(rows[3]).toContain("Uncertain");
    expect(rows[3]).not.toContain("Calculated");
    expect(rows[3]).toContain("The evidence for this day conflicts.");
    expect(rows[4]).toContain(
      "Clash of Clans returned 9 defenses for this day, more than the usual 8, so this day is uncertain.",
    );
    expect(text[4]).toContain(
      "Trophy change 0 Reset rank Unknown Attacks +310 Defenses 0",
    );
    expect(text[4]).toContain("Recorded battle net +310");
    expect(rows[5]).toContain("<strong>Calculated.</strong>");
    expect(rows[5]).not.toContain("Uncertain");
    expect(text[5]).toMatch(/Trophy change −11 .*battle net −11 /);
    expect(rows[5]).toContain("The battle log was not checked at the start of this day.");
    expect(rows[6]).toContain("Uncertain");
    expect(rows[6]).toContain(
      "The two players&#x27; battle logs disagree about a result.",
    );
    expect(rows[7]).toContain('<span class="day-mark day-mark-gap" title="Uncertain">');
    expect(text[0]).toContain("Attacks recorded 8 Defenses recorded 8 Adjustment No");
  });

  it("shows the received warnings instead of a late-tracking explanation", async () => {
    const html = await page([ENDED_DAY], {
      dataQuality: [
        {
          code: "stale",
          label: "Stale saved profile",
          detail:
            "The collector has not confirmed this player profile within the current freshness limit.",
        },
        {
          code: "partial",
          label: "Incomplete ranked-day data",
          detail: "missing_end_battle_log_baseline; new_unknown_code",
        },
        {
          code: "uncertain",
          label: "Saved day state",
          detail: "ranked_day_state:Inconsistent; ranked_day_state:Malformed",
        },
      ],
    });
    expect(html).not.toContain("tracking started partway");
    expect(html).toContain(
      "This profile hasn&#x27;t been confirmed recently, so it may be behind.",
    );
    expect(html).toContain(
      "Ending evidence arrives after Reset. Some daily evidence is unavailable.",
    );
    expect(html).not.toContain("new_unknown_code");
    expect(html).toContain(
      "The evidence for this day conflicts. Some saved evidence for this day could not be read.",
    );
    expect(html).not.toContain("ranked_day_state");
  });
});
