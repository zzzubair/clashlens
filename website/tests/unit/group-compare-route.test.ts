import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  StaticRouterProvider,
} from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  requireLogin: vi.fn(),
  requestJson: vi.fn(),
}));

vi.mock("../../app/server/auth-guard.server", () => ({
  requireLogin: mocks.requireLogin,
}));

vi.mock("../../app/services/python.server", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../app/services/python.server")>();
  return { ...actual, requestJson: mocks.requestJson };
});

import GroupCompareRoute, { loader } from "../../app/routes/account.groups.$groupId";
import { PythonApiError } from "../../app/services/python.server";
import { mapGroupComparison, type GroupComparison } from "../../app/lib/group-comparison";
import { worstComparison } from "../fixtures/worst-case-accounts";
import { GainLines } from "../../app/components/GainLines";

const ORIGIN = "https://clashlens.example";
const GROUP_ID = "6c1e3f8a-2a44-4b7d-9c0e-1f2a3b4c5d6e";
const IDENTITY = { provider: "google", providerSubject: "11223344556677889900" } as const;
const DAYS = [
  "2026-08-03T05:00:00+00:00",
  "2026-08-04T05:00:00+00:00",
  "2026-08-05T05:00:00+00:00",
];

function player(overrides: Record<string, unknown> = {}) {
  return {
    tag: "#2PP",
    name: "Prof",
    you: false,
    in_group: true,
    status: "tracking",
    trophies: 5300,
    observed_at: "2026-08-06T12:00:00+00:00",
    age_seconds: 60,
    freshness: "fresh",
    today: { net: 64, gained: 80, lost: 16, attacks: 2, defenses: 1 },
    day_results: [
      { start: DAYS[0], state: "retired", net: null },
      { start: DAYS[1], state: "partial", net: 10 },
      { start: DAYS[2], state: "correcting", net: 40 },
    ],
    counted_days: 1,
    counted_attacks: 2,
    net: 40,
    net_per_day: 40.0,
    vs_group: null,
    attack: {
      count: 2,
      stars: 5,
      destruction: 180,
      three_stars: 1,
      trophies: 56,
      days: 1,
    },
    defense: {
      count: 1,
      stars: 1,
      destruction: 45,
      trophies: 16,
      star_counts: { "0": 0, "1": 1, "2": 0, "3": 0 },
      days: 1,
    },
    ...overrides,
  };
}

function payload(players = [player()]) {
  return {
    kind: "group-comparison",
    group_id: GROUP_ID,
    name: "Rivals",
    days: 3,
    day_starts: DAYS,
    today_start: "2026-08-06T05:00:00+00:00",
    generated_at: "2026-08-06T12:00:00+00:00",
    season: "1783918800",
    players,
  };
}

function load(path: string) {
  return loader({
    request: new Request(`${ORIGIN}${path}`),
    params: { groupId: path.split("/")[3]?.split("?")[0] },
  } as never) as unknown as Promise<{
    data: Record<string, unknown>;
    init: { status?: number };
  }>;
}

describe("group comparison", () => {
  beforeEach(() => {
    mocks.requireLogin.mockReset().mockResolvedValue(IDENTITY);
    mocks.requestJson.mockReset();
  });

  it("keeps missing results empty instead of zero", () => {
    const comparison = mapGroupComparison(payload());
    expect(comparison?.players[0].days.map((day) => day.net)).toEqual([null, 10, 40]);
    expect(comparison?.players[0].days[0].state).toBe("retired");
    expect(comparison?.players[0].vsGroup).toBeNull();
  });

  it("keeps a player waiting for their Season reset without trophies", () => {
    const waiting = player({ trophies: null, season_reset_pending: true });
    const players = mapGroupComparison(
      payload([waiting, player({ tag: "#8PY" })]),
    )?.players;
    expect(players?.map((p) => [p.trophies, p.seasonResetPending])).toEqual([
      [null, true],
      [5300, false],
    ]);
  });

  it("rejects a response whose days do not match the requested window", () => {
    expect(mapGroupComparison({ ...payload(), days: 7 })).toBeNull();
    expect(mapGroupComparison(payload([player({ day_results: [] })]))).toBeNull();
    expect(mapGroupComparison(payload([player({ status: "inactive" })]))).toBeNull();
  });

  it("reads the signed-in account's group for a supported window only", async () => {
    mocks.requestJson.mockResolvedValue(payload());
    const result = await load(`/account/watchlists/${GROUP_ID}?days=3`);
    expect(result.data.comparison).not.toBeNull();
    expect(mocks.requestJson).toHaveBeenCalledWith(
      `/v1/account/groups/${GROUP_ID}/comparison?days=3`,
      "GET",
      undefined,
      undefined,
      undefined,
      IDENTITY,
    );
    mocks.requestJson.mockResolvedValue({
      ...payload(),
      days: 7,
      day_starts: [...DAYS, ...DAYS, DAYS[0]],
    });
    const fallback = await load(
      `/account/watchlists/${GROUP_ID}?days=999&sort=__proto__`,
    );
    // With no usable sort it opens on the days compared, best gain first.
    expect(fallback.data.sort).toBe("net");
    expect(mocks.requestJson).toHaveBeenLastCalledWith(
      `/v1/account/groups/${GROUP_ID}/comparison?days=7`,
      "GET",
      undefined,
      undefined,
      undefined,
      IDENTITY,
    );
  });

  it("shows another account's group as not found", async () => {
    mocks.requestJson.mockRejectedValue(
      new PythonApiError(404, { error: "group_not_found" }),
    );
    const result = await load(`/account/watchlists/${GROUP_ID}`);
    expect(result.init.status).toBe(404);
    expect(result.data).toMatchObject({ notFound: true, comparison: null });
  });

  it("explains a group too large to compare", async () => {
    mocks.requestJson.mockRejectedValue(
      new PythonApiError(422, {
        error: "group_too_large",
        detail: "34 players; compare at most 20",
      }),
    );
    const result = await load(`/account/watchlists/${GROUP_ID}`);
    expect(result.data.tooLarge).toBe("34 players; compare at most 20");
  });

  it("returns to the comparison after first-time account setup", async () => {
    mocks.requestJson.mockRejectedValue(
      new PythonApiError(403, { error: "account_not_found" }),
    );
    const thrown = await load(`/account/watchlists/${GROUP_ID}?days=3`).catch(
      (error: unknown) => error,
    );
    expect((thrown as Response).headers.get("Location")).toBe(
      `/account/setup?returnPath=${encodeURIComponent(`/account/watchlists/${GROUP_ID}`)}`,
    );
  });

  it("shows the table without explanation paragraphs", async () => {
    const handler = createStaticHandler([
      {
        path: "/account/watchlists/:groupId",
        Component: GroupCompareRoute,
        loader: () => ({
          comparison: mapGroupComparison(
            payload([
              player({
                today: { net: null, gained: 80, lost: 16, attacks: 2, defenses: 1 },
              }),
            ]),
          ),
          days: 3,
          sort: "trophies",
          notFound: false,
          tooLarge: null,
          error: null,
        }),
      },
    ]);
    const context = await handler.query(
      new Request(`${ORIGIN}/account/watchlists/${GROUP_ID}`),
    );
    if (context instanceof Response) throw new Error("unexpected response");
    const html = renderToString(
      createElement(StaticRouterProvider, {
        router: createStaticRouter(handler.dataRoutes, context),
        context,
      }),
    );
    // Each day bar says what it means to screen readers; the bars take keyboard
    // focus (pointing or the arrow keys then show one day's date and result), and
    // their column names them as Rankings does; the shared scale is spelt out
    // for screen readers.
    expect(html).toContain("5 Aug may still change +40");
    expect(html).toContain("4 Aug incomplete +10");
    expect(html).toMatch(
      /<span class="trend" role="img" aria-label="[^"]*" tabindex="0"/,
    );
    // The bars are on the player's own scale, up and down from zero in the
    // middle: the best day, +40, fills the top half, and the +10 a quarter
    // of it. No range is spelt out over them; the gain has one short heading.
    expect(html).toContain('style="height:max(4px, 50%)"');
    expect(html).toContain('style="height:max(4px, 12.5%)"');
    expect(html).not.toMatch(/trend-title|trend-range|a day<\/small>/);
    expect(html).toContain('sort=net" data-discover="true">Gain</a></th>');
    // The chart over the table climbs from 0 by the same counted days as the
    // gain: the incomplete +10 adds nothing, so the line ends on +40.
    expect(html).toContain(
      '<span class="gain-lines-title">Trophies gained, 3–5 Aug</span>',
    );
    expect(html).toMatch(
      /<span class="gain-lines-name">Prof<\/span> <!-- -->\+40<\/span>/,
    );
    expect(html).toMatch(/<path d="M0 [\d.]+ L33\.3+ ([\d.]+) L66\.6+7? \1 L100 4"/);
    // Every day's bar is drawn the same way; the dates over them say which is last.
    expect(html.match(/<span class="trend-day [^"]*"[^>]*>/g)).toHaveLength(3);
    expect(html).not.toContain("data-latest");
    expect(html).not.toContain("data-cut");
    // The two numbers in Attack and Defense are named once, in their
    // headings; each cell keeps the words for screen readers.
    expect(html).toContain('<small class="compare-head-sub">Triples · avg ★</small>');
    expect(html).toContain('<small class="compare-head-sub">Held · avg ★</small>');
    expect(html).toContain(
      '<span aria-hidden="true">★</span><span class="sr-only"> stars an attack</span>',
    );
    expect(html).not.toMatch(/avg stars|compare-rate-word/);
    // Wide screens group the columns into where each player is now and the
    // days compared, "Last 3 days", their dates on hover and behind the ⓘ.
    expect(html).toMatch(
      /<tr class="compare-groups"><td><\/td><th scope="colgroup" colSpan="2"><span>Now<\/span><\/th><th scope="colgroup" colSpan="[34]"><span><span class="last-days">Last <!-- -->3<!-- --> days<details class="last-days-info"><summary title="3–5 Aug">/,
    );
    expect(html).toContain(
      "3–5 Aug, one bar a day. Each player&#x27;s bars have their own scale: the tallest is that player&#x27;s biggest day.",
    );
    const text = html
      .replaceAll("<!-- -->", "")
      .replaceAll("&#x27;", "'")
      .replace(/<[^>]*>/g, " ")
      .replace(/\s+/g, " ");
    expect(text).not.toContain("adds up each player's trophy change");
    expect(text).not.toContain("Some battles are missing");
    expect(text).not.toContain("Attack and defense also use every battle recorded");
    expect(text).toContain("Not yet proven");
    // The smaller figures wait in each row's details instead of filling the table.
    expect(text).toContain("+80 won · −16 lost");
    expect(text).toContain("2 attacks, 1 defense");
  });

  it("offers to reload the comparison when it could not be read", async () => {
    const handler = createStaticHandler([
      {
        path: "/account/watchlists/:groupId",
        Component: GroupCompareRoute,
        loader: () => ({
          comparison: null,
          days: 3,
          sort: "net",
          notFound: false,
          tooLarge: null,
          error: { error: { code: "unavailable", message: "Try again later." } },
        }),
      },
    ]);
    const context = await handler.query(
      new Request(`${ORIGIN}/account/watchlists/${GROUP_ID}?days=3&sort=net`),
    );
    if (context instanceof Response) throw new Error("unexpected response");
    const html = renderToString(
      createElement(StaticRouterProvider, {
        router: createStaticRouter(handler.dataRoutes, context),
        context,
      }),
    ).replaceAll("&amp;", "&");
    expect(html).toContain("Comparison unavailable");
    expect(html).toContain(
      `<a href="/account/watchlists/${GROUP_ID}?days=3&sort=net">Try again</a>`,
    );
  });

  it("does not call Python for a malformed group ID", async () => {
    const result = await load("/account/watchlists/not-a-group");
    expect(result.init.status).toBe(404);
    expect(mocks.requestJson).not.toHaveBeenCalled();
  });

  it("keeps a full group of worst-case players readable", async () => {
    const html = await renderComparison(worstComparison());
    expect(html.match(/<th scope="row"/g)).toHaveLength(20);
    // Totals over 14 days pass 1,000 trophies, and each count reads in the singular.
    for (const text of ["−1,234", "+3,808 won · −3,360 lost", "1 defense gave up 1 star"])
      expect(html).toContain(text);
    expect(html).not.toContain("No players in this watchlist yet");
  });

  it("shows your own players alongside a group without members", async () => {
    const comparison = worstComparison(3);
    comparison.players = comparison.players.filter((player) => !player.inGroup);
    const html = await renderComparison(comparison);
    expect(html.match(/<th scope="row"/g)).toHaveLength(1);
    expect(html).toContain("not in this watchlist");
    expect(html).not.toContain("No players in this watchlist yet");
  });

  it("sorts by and crowns the biggest total gain, the number shown", async () => {
    // Ace gained +280 over 7 counted days, 40 a day; Bea +150 over 3, 50 a
    // day. Ace's line ends highest, so Ace's +280 wears the crown.
    const comparison = mapGroupComparison(
      payload([
        player({ tag: "#2PP", name: "Ace", net: 280, net_per_day: 40, counted_days: 7 }),
        player({ tag: "#2QQ", name: "Bea", net: 150, net_per_day: 50, counted_days: 3 }),
      ]),
    );
    if (comparison === null) throw new Error("the payload should map");
    const html = await renderComparison(comparison, "net");
    // Sorted by Gain, the order follows the same total: Ace first.
    const names = [...html.matchAll(/class="compare-name"[^>]*>([^<]+)</g)].map(
      (match) => match[1],
    );
    expect(names).toEqual(["Ace", "Bea"]);
    expect(html).toMatch(/class="compare-number compare-up compare-lead"[^>]*>\+280</);
    expect(html).not.toMatch(/compare-lead"[^>]*>\+150</);
    expect(html.match(/compare-number[^"]* compare-up compare-lead"/g)).toHaveLength(1);
  });

  it("sorts a measure by the number it shows and marks who leads it", async () => {
    const attack = (threeStars: number, stars: number) => ({
      count: 4,
      stars,
      destruction: 360,
      three_stars: threeStars,
      trophies: 120,
      days: 1,
    });
    // Ace triples more often; Bea gets more stars an attack and gains more a
    // day; Cy, the Clasher's own player outside the list, has no attacks.
    const comparison = mapGroupComparison(
      payload([
        player({
          tag: "#2QQ",
          name: "Bea",
          attack: attack(2, 10),
          net: 60,
          net_per_day: 20,
        }),
        player({ tag: "#2PP", name: "Ace", attack: attack(3, 9), net_per_day: 10 }),
        player({
          tag: "#2RR",
          name: "Cy",
          you: true,
          in_group: false,
          attack: { ...attack(0, 0), count: 0 },
        }),
      ]),
    );
    if (comparison === null) throw new Error("the payload should map");
    const html = await renderComparison(comparison, "attack");
    const names = [...html.matchAll(/class="compare-name"[^>]*>([^<]+)</g)].map(
      (match) => match[1],
    );
    // Attack sorts by the share of triples it shows, not the stars beside it.
    expect(names).toEqual(["Ace", "Bea", "Cy"]);
    // Each measure's leader is marked once; both held every defense, so no
    // one leads defense.
    expect(html).toContain(
      'class="compare-number compare-lead" title="The best in this watchlist">75%<span class="sr-only"> of attacks tripled</span><span class="sr-only">, the best in this watchlist</span>',
    );
    expect(html).toMatch(/class="compare-number compare-up compare-lead"[^>]*>\+60</);
    expect(html.match(/compare-number[^"]* compare-lead"/g)).toHaveLength(2);
    // The crown is keyed once, in the line under the title.
    expect(html).toContain(
      '<span class="compare-lead-key" aria-hidden="true"> · <span class="compare-crown"></span>best in this watchlist</span>',
    );
    // The title says who the list holds and the Clasher's own beside it.
    expect(html).toContain(
      '2 watched + 1 yours<span class="compare-span-days"> · 3 finished days</span>',
    );
    // A phone's two headings sort; the measure on screen is the sorted one.
    expect(html).toContain(
      `<a class="compare-key-sort" aria-current="true" href="/account/watchlists/${GROUP_ID}?days=3&amp;sort=attack" data-discover="true"><span>Triples</span><small class="compare-key-range">and avg ★</small><span class="sr-only">, sorted</span></a>`,
    );
    expect(html).toContain(
      `<a class="compare-key-sort" href="/account/watchlists/${GROUP_ID}?days=3&amp;sort=trophies" data-discover="true"><span>Trophies</span></a>`,
    );
  });

  it("says a comparison without any players has nobody to compare", async () => {
    const comparison = worstComparison(3);
    comparison.players = [];
    const html = await renderComparison(comparison);
    expect(html).toContain("No players in this watchlist yet");
    expect(html).toContain(`href="/account/watchlists#group-${comparison.groupId}"`);
    expect(html).not.toContain("<table");
  });
});

describe("trophies gained chart labels", () => {
  // A crowd at the top, as in a real watchlist: two of yours, others whose
  // lines end close together, and one that dips below zero first.
  const ends: [string, boolean, number, number][] = [
    ["Ember", true, 200, 278],
    ["Ember Mini", true, 100, 89],
    ["Echo jr", false, 241, 200],
    ["Juno FR", false, 200, 241],
    ["Vera JP", false, 219, 200],
    ["Arjun IN", false, 142, 200],
    ["Kai", false, 130, 200],
    ["Lou", false, -150, 270],
  ];
  const html = renderToString(
    createElement(GainLines, {
      lines: ends.map(([name, you, first, second]) => ({
        tag: name,
        name,
        you,
        days: [
          { start: "2026-10-27T05:00:00Z", state: "complete" as const, net: first },
          { start: "2026-10-28T05:00:00Z", state: "complete" as const, net: second },
        ],
      })),
      dayStarts: ["2026-10-27T05:00:00Z", "2026-10-28T05:00:00Z"],
      lit: null,
      light: { hover: () => undefined, pin: () => undefined },
    }),
  ).replaceAll("<!-- -->", "");
  const gain = Object.fromEntries(ends.map(([name, , a, b]) => [name, a + b]));
  const labels = [
    ...html.matchAll(
      /<span class="gain-lines-end"([^>]*)><span class="gain-lines-name">([^<]+)</g,
    ),
  ].map(([, attributes, name]) => ({
    name: name!,
    shade: attributes!.match(/data-shade="(\d)"/)?.[1],
    at: Number(attributes!.match(/--wide-at:(-?[\d.]+)%/)![1]),
  }));

  it("names every line at its end on a wide screen, stacked in the lines' order", () => {
    expect(labels.map((label) => label.name).sort()).toEqual(
      ends.map(([name]) => name).sort(),
    );
    // Top to bottom, biggest gain first, a label's height apart at least,
    // so none overlaps; the one that dipped below zero is named too.
    const stack = [...labels].sort((a, b) => a.at - b.at);
    stack.slice(1).forEach((label, index) => {
      expect(gain[label.name]).toBeLessThanOrEqual(gain[stack[index]!.name]!);
      expect(label.at - stack[index]!.at).toBeGreaterThanOrEqual(10.5 - 1e-9);
    });
    // A label moved off its line's end has a leader back to it.
    const leaders = [
      ...html.matchAll(/data-mode="wide" x1="0" x2="100" y1="[^"]+" y2="([^"]+)"/g),
    ];
    expect(leaders.length).toBeGreaterThan(0);
    for (const [, y2] of leaders)
      expect(labels.some((label) => label.at === Number(y2))).toBe(true);
  });

  it("tells your two players apart by shade, in the chart and its list", () => {
    expect(labels.find((label) => label.name === "Ember")!.shade).toBe("1");
    expect(labels.find((label) => label.name === "Ember Mini")!.shade).toBe("2");
    expect(labels.find((label) => label.name === "Lou")!.shade).toBeUndefined();
    expect(html).toContain("data-two-yours");
  });

  it("lists the names under a phone's chart when they can't all fit beside it", () => {
    // Eight names are too many for a phone's short chart, not a wide one's.
    expect(html).toMatch(/<figure class="gain-lines" data-list="narrow"/);
    const listed = [
      ...html.matchAll(/<li( data-shade="\d")?><span class="gain-lines-name">([^<]+)</g),
    ].map(([, shade, name]) => `${name}${shade ? " (yours)" : ""}`);
    expect(listed).toEqual([
      "Ember (yours)",
      "Echo jr",
      "Juno FR",
      "Vera JP",
      "Arjun IN",
      "Kai",
      "Ember Mini (yours)",
      "Lou",
    ]);
  });

  it("lights nothing on the chart for a player who has no line", () => {
    const lit = (tag: string) =>
      renderToString(
        createElement(GainLines, {
          lines: [ends[0], ends[1]].map(([name, you, first]) => ({
            tag: name,
            name,
            you,
            days: [
              { start: "2026-10-28T05:00:00Z", state: "complete" as const, net: first },
            ],
          })),
          dayStarts: ["2026-10-28T05:00:00Z"],
          lit: tag,
          light: { hover: () => undefined, pin: () => undefined },
        }),
      );
    // Lit from the table, a player without a line dims none of the others.
    expect(lit("#NOLINE")).toMatch(/^<figure class="gain-lines"(?! data-lit)/);
    expect(lit("Ember")).toMatch(/^<figure class="gain-lines" data-lit=""/);
  });

  it("draws no chart before anyone has a counted day", () => {
    const html = renderToString(
      createElement(GainLines, {
        lines: [
          {
            tag: "#2PP",
            name: "Prof",
            you: false,
            days: [{ start: "2026-10-28T05:00:00Z", state: "partial" as const, net: 30 }],
          },
        ],
        dayStarts: ["2026-10-28T05:00:00Z"],
        lit: null,
        light: { hover: () => undefined, pin: () => undefined },
      }),
    );
    expect(html).toBe("");
  });

  it("marks the days under the lines and a few trophy steps beside them", () => {
    expect(html.match(/<span class="gain-lines-day"[^>]*>([^<]+)</g)?.length).toBe(2);
    expect(html).toContain(">27 Oct</span>");
    expect(html).toContain(">28 Oct</span>");
    const ticks = [...html.matchAll(/<span class="gain-lines-tick"[^>]*>([^<]+)</g)].map(
      ([, text]) => text,
    );
    // The lines run from −150 to +478: steps of 200, 0 first.
    expect(ticks).toEqual(["0", "+200", "+400"]);
  });
});

async function renderComparison(comparison: GroupComparison, sort = "trophies") {
  const handler = createStaticHandler([
    {
      path: "/account/watchlists/:groupId",
      Component: GroupCompareRoute,
      loader: () => ({
        comparison,
        days: comparison.days,
        sort,
        notFound: false,
        tooLarge: null,
        error: null,
      }),
    },
  ]);
  const context = await handler.query(
    new Request(`${ORIGIN}/account/watchlists/${GROUP_ID}`),
  );
  if (context instanceof Response) throw new Error("unexpected response");
  return renderToString(
    createElement(StaticRouterProvider, {
      router: createStaticRouter(handler.dataRoutes, context),
      context,
    }),
  ).replaceAll("<!-- -->", "");
}
