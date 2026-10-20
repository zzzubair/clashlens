import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  StaticRouterProvider,
} from "react-router";
import { describe, expect, it } from "vitest";

import {
  AccountRow,
  canSet,
  DaysSheet,
  dayOptions,
  LEGEND_ONLY,
  RunStrip,
} from "../../app/components/NotebookParts";
import {
  baseInitials,
  baseOnDay,
  basesNear,
  libraryView,
  matchName,
  parseChange,
  parseNotebook,
  sameUntil,
  stripBlocks,
  type Notebook,
  type NotebookBase,
  type NotebookPlayer,
} from "../../app/lib/notebook";

const RECORD = {
  days: 2,
  defenses: 10,
  held: 7,
  stars: [1, 3, 3, 3],
  trophies_lost: 180,
};

function apiNotebook() {
  return {
    kind: "notebook",
    season: {
      id: "2026-10",
      start: "2026-10-05T05:00:00Z",
      end: "2026-11-02T05:00:00Z",
      today: 16,
      tomorrow: 17,
    },
    as_of_day: 15,
    players: [
      {
        tag: "#L9L0GJ9LR",
        name: "curtypie",
        league: "Legend League",
        runs: [{ from: 9, to: 16, base: "B-7K3QX9", record: RECORD }],
        current: "B-7K3QX9",
        next: null,
        picks: [{ day: 9, base: "B-7K3QX9" }],
        legend_days: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17],
      },
    ],
    bases: [
      {
        id: "B-7K3QX9",
        name: "Ring",
        status: "active",
        added_day: 9,
        last_ran_day: 16,
        recent: "2026-10-20T06:00:00Z",
        record: RECORD,
      },
    ],
  };
}

function base(
  id: string,
  name: string,
  status: NotebookBase["status"],
  held: number,
  defenses: number,
  recent: string,
): NotebookBase {
  return {
    id,
    name,
    status,
    addedDay: 1,
    lastRanDay: null,
    recent,
    record: { days: 1, defenses, held, stars: [0, 0, 0, 0], trophiesLost: 0 },
  };
}

describe("notebook contract", () => {
  it("reads the private API's notebook", () => {
    const notebook = parseNotebook(apiNotebook());
    expect(notebook?.season.today).toBe(16);
    expect(notebook?.players[0]?.runs[0]?.record.trophiesLost).toBe(180);
    expect(notebook?.bases[0]).toMatchObject({ id: "B-7K3QX9", addedDay: 9 });
  });

  it("refuses a notebook with a malformed base ID or day", () => {
    const badId = apiNotebook();
    badId.bases[0]!.id = "B-7K3QXI";
    expect(parseNotebook(badId)).toBeNull();
    const badDay = apiNotebook();
    badDay.season.today = 29;
    expect(parseNotebook(badDay)).toBeNull();
  });

  it("checks a posted change before it reaches the private API", () => {
    expect(
      parseChange({ action: "pick", base: "B-7K3QX9", tag: "#L9L0GJ9LR", day: 9 }),
    ).toEqual({ action: "pick", base: "B-7K3QX9", tag: "#L9L0GJ9LR", day: 9 });
    expect(parseChange({ action: "drop" })).toBeNull();
    expect(parseChange({ action: "pick", tag: "L9L0GJ9LR" })).toBeNull();
    expect(
      parseChange({ action: "edit", base: "B-7K3QX9", note: "x".repeat(2001) }),
    ).toBeNull();
  });
});

describe("Library", () => {
  const bases = [
    base("B-000001", "Ring", "active", 7, 10, "2026-10-19T06:00:00Z"),
    base("B-000002", "Box ring", "to_try", 0, 0, "2026-10-20T06:00:00Z"),
    base("B-000003", "Spiral", "retired", 9, 10, "2026-10-10T06:00:00Z"),
  ];

  it("searches by name anywhere, case-blind, and counts each filter", () => {
    const view = libraryView(bases, "RING", "all", "name");
    expect(view.rows.map((item) => item.name)).toEqual(["Box ring", "Ring"]);
    expect(view.counts).toEqual({ all: 2, active: 1, to_try: 1, retired: 0 });
    expect(matchName("Box ring", "RING")).toEqual(["Box ", "ring", ""]);
  });

  it("sorts by best held with untried bases last, and by most recent", () => {
    expect(libraryView(bases, "", "all", "held").rows.map((item) => item.name)).toEqual([
      "Spiral",
      "Ring",
      "Box ring",
    ]);
    expect(libraryView(bases, "", "all", "recent").rows[0]?.name).toBe("Box ring");
    expect(libraryView(bases, "", "retired", "recent").rows).toHaveLength(1);
  });

  it("draws a tile's letters from the first two words", () => {
    expect(baseInitials("anti 2 star ring")).toBe("A2");
    expect(baseInitials("Spiral")).toBe("S");
  });
});

describe("run strip", () => {
  const player: NotebookPlayer = {
    tag: "#L9L0GJ9LR",
    name: "curtypie",
    league: null,
    runs: [
      {
        from: 3,
        to: 8,
        base: "B-000001",
        record: { days: 0, defenses: 0, held: 0, stars: [0, 0, 0, 0], trophiesLost: 0 },
      },
      {
        from: 9,
        to: 16,
        base: "B-000002",
        record: { days: 0, defenses: 0, held: 0, stars: [0, 0, 0, 0], trophiesLost: 0 },
      },
    ],
    current: "B-000002",
    next: null,
    picks: [
      { day: 3, base: "B-000001" },
      { day: 9, base: "B-000002" },
    ],
    legendDays: Array.from({ length: 17 }, (_, index) => index + 1),
  };

  it("covers all 28 days: no base, past run, today's run, days to come", () => {
    expect(stripBlocks(player, 16)).toEqual([
      { from: 1, to: 2, base: null, kind: "none" },
      { from: 3, to: 8, base: "B-000001", kind: "past" },
      { from: 9, to: 16, base: "B-000002", kind: "now" },
      { from: 17, to: 28, base: null, kind: "next" },
    ]);
  });

  it("says how long a fixed day's base, or no base, lasts", () => {
    expect(sameUntil(player, 5, 16)).toBe(8);
    expect(sameUntil(player, 9, 16)).toBe(16);
    expect(sameUntil(player, 1, 16)).toBe(2);
    const cleared = { ...player, picks: [...player.picks, { day: 12, base: null }] };
    expect(sameUntil(cleared, 12, 16)).toBe(16);
    // Out of Legend League on Days 12 and 13: Day 9's base stops at Day 11
    // and is gone when they are back.
    const gap = {
      ...player,
      legendDays: player.legendDays.filter((day) => day < 12 || day > 13),
    };
    expect(sameUntil(gap, 9, 16)).toBe(11);
    expect(baseOnDay(gap, 14)).toBeNull();
  });

  it("offers the base said for a day first when fixing it", () => {
    expect(basesNear(player, 5)).toEqual(["B-000001", "B-000002"]);
    expect(basesNear(player, 12)).toEqual(["B-000002", "B-000001"]);
  });
});

async function render(element: ReturnType<typeof createElement>) {
  const handler = createStaticHandler([{ path: "/", Component: () => element }]);
  const context = await handler.query(new Request("http://localhost/"));
  if (context instanceof Response) throw new Error("unexpected response");
  return renderToString(
    createElement(StaticRouterProvider, {
      router: createStaticRouter(handler.dataRoutes, context),
      context,
      hydrate: false,
    }),
  );
}

describe("Legend days only", () => {
  const EMPTY = {
    days: 0,
    defenses: 0,
    held: 0,
    stars: [0, 0, 0, 0],
    trophiesLost: 0,
  } as const;
  const DAYS = Array.from({ length: 28 }, (_, index) => index + 1);
  // The worst case: a different base on every one of the 28 days.
  const daily: NotebookPlayer = {
    tag: "#L9L0GJ9LR",
    name: "curtypie",
    league: "Legend League",
    runs: DAYS.map((day) => ({
      from: day,
      to: day,
      base: `B-0000${String(day).padStart(2, "0")}`,
      record: { ...EMPTY, stars: [0, 0, 0, 0] },
    })),
    current: "B-000028",
    next: null,
    picks: DAYS.map((day) => ({ day, base: `B-0000${String(day).padStart(2, "0")}` })),
    legendDays: DAYS,
  };
  // Dropped out of Legend League at the Day 8 Reset.
  const dropped: NotebookPlayer = {
    ...daily,
    tag: "#80YJ8Y99R",
    name: "Sloothyy",
    runs: daily.runs.slice(0, 7),
    current: null,
    picks: daily.picks.slice(0, 7),
    legendDays: DAYS.slice(0, 7),
  };
  const notebook: Notebook = {
    season: {
      id: "2026-10",
      start: "2026-10-06T05:00:00Z",
      end: "2026-11-03T05:00:00Z",
      today: 28,
      tomorrow: 28,
    },
    asOfDay: 27,
    players: [daily, dropped],
    bases: DAYS.map((day) => ({
      id: `B-0000${String(day).padStart(2, "0")}`,
      name: `Base number ${day}`,
      status: "active",
      addedDay: day,
      lastRanDay: day,
      recent: "2026-10-06T05:00:00Z",
      record: { ...EMPTY, stars: [0, 0, 0, 0] },
    })),
  };

  it("offers only the account's Legend days", () => {
    const mid = { ...notebook, season: { ...notebook.season, today: 9, tomorrow: 10 } };
    expect(dayOptions(mid, daily).map(([day]) => day)).toEqual([9, 10]);
    expect(dayOptions(mid, dropped)).toEqual([]);
    expect(canSet(mid, dropped)).toBe(false);
  });

  it("draws 28 one-day runs as a picture with nothing to tap", async () => {
    const html = await render(createElement(RunStrip, { notebook, player: daily }));
    expect(html.match(/class="nb-run /g)).toHaveLength(28);
    expect(html).not.toContain("<button");
    expect(html).toContain('role="img"');
    expect(html).toContain("Base number 1 Day 1, Base number 2 Day 2");
  });

  it("keeps Change and day picks from an account off Legend League", async () => {
    const off = await render(
      createElement(AccountRow, {
        notebook,
        player: dropped,
        onChange: () => {},
        onAdd: () => {},
        onDays: () => {},
      }),
    );
    expect(off).toContain(LEGEND_ONLY);
    expect(off).not.toContain(">Change<");
    // Its Legend days 1 to 7 can still be fixed.
    expect(off).toContain(">Days<");
    const on = await render(
      createElement(AccountRow, {
        notebook,
        player: daily,
        onChange: () => {},
        onAdd: () => {},
        onDays: () => {},
      }),
    );
    expect(on).toContain(">Change<");
    expect(on).not.toContain(LEGEND_ONLY);
  });

  it("asks an account back in Legend League with no base to pick one", async () => {
    // Back from Day 15: Day 7's base stopped at Day 8.
    const back = { ...dropped, legendDays: [...DAYS.slice(0, 7), 15, 16, 17] };
    const mid = { ...notebook, season: { ...notebook.season, today: 16, tomorrow: 17 } };
    const row = (player: NotebookPlayer) =>
      render(
        createElement(AccountRow, {
          notebook: mid,
          player,
          onChange: () => {},
          onAdd: () => {},
          onDays: () => {},
        }),
      );
    const html = await row(back);
    expect(html.match(/class="button"/g)).toHaveLength(1);
    expect(html).toContain('class="button">Pick a base<');
    expect(html).not.toContain(">Change<");
    expect(html).not.toContain("Add your base");
    // An account that never ran a base adds its first one.
    const first = await row({ ...back, runs: [], picks: [] });
    expect(first.match(/class="button"/g)).toHaveLength(1);
    expect(first).toContain('class="button">Add your base<');
  });

  it("lists every day, newest first, with days off Legend League shut", async () => {
    const html = await render(
      createElement(DaysSheet, {
        notebook,
        player: dropped,
        onDay: () => {},
        onClose: () => {},
      }),
    );
    const rows =
      html.match(/<button type="button" class="nb-row nb-day-row"[^>]*>/g) ?? [];
    expect(rows).toHaveLength(28);
    expect(rows.filter((row) => row.includes("disabled"))).toHaveLength(21);
    const text = html.replaceAll("<!-- -->", "");
    expect(text.indexOf("Day 28 · today")).toBeLessThan(text.indexOf("Day 7<"));
    expect(html).toContain("Not a Legend day");
    expect(html).toContain("Tue 6 Oct");
  });
});
