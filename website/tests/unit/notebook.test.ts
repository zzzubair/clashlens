import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  StaticRouterProvider,
} from "react-router";
import { describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getWebsiteConfig: vi.fn(),
  requireLogin: vi.fn(),
  saveNotebook: vi.fn(),
  getNotebook: vi.fn(),
}));

vi.mock("../../app/server/config.server", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/server/config.server")>();
  return { ...actual, getWebsiteConfig: mocks.getWebsiteConfig };
});

vi.mock("../../app/server/auth-guard.server", () => ({
  requireLogin: mocks.requireLogin,
}));

vi.mock("../../app/services/notebook.server", () => ({
  saveNotebook: mocks.saveNotebook,
  getNotebook: mocks.getNotebook,
}));

import {
  AccountRow,
  canSet,
  DaysSheet,
  dayFixWords,
  dayOptions,
  LEGEND_ONLY,
  Library,
  RunStrip,
  saveError,
  savedToast,
} from "../../app/components/NotebookParts";
import { SeasonSwitcher } from "../../app/components/NotebookSeasons";
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
import { loader as baseLoader } from "../../app/routes/notebook.base";
import { action as saveAction } from "../../app/routes/notebook.save";
import { PythonApiError } from "../../app/services/python.server";

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
      id: "1791176400",
      start: "2026-10-05T05:00:00Z",
      end: "2026-11-02T05:00:00Z",
      today: 16,
      tomorrow: 17,
      current: true,
      editable: true,
      editable_until: "2026-11-30T05:00:00Z",
    },
    seasons: [
      { id: "1791176400", start: "2026-10-05T05:00:00Z", end: "2026-11-02T05:00:00Z" },
    ],
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
      id: "1791262800",
      start: "2026-10-06T05:00:00Z",
      end: "2026-11-03T05:00:00Z",
      today: 28,
      tomorrow: 28,
      current: true,
      editable: true,
      editableUntil: "2026-12-01T05:00:00Z",
    },
    seasons: [
      { id: "1791262800", start: "2026-10-06T05:00:00Z", end: "2026-11-03T05:00:00Z" },
    ],
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

describe("base page", () => {
  it("opens only an exact base ID", async () => {
    vi.stubEnv("CLASHLENS_NOTEBOOK_ENABLED", "true");
    try {
      const request = new Request("http://localhost/notebook/bases/b-7k3qx9");
      const refused = await baseLoader({
        request,
        params: { code: "b-7k3qx9" },
      } as never).then(
        () => null,
        (error: unknown) => error,
      );
      expect((refused as { init: { status: number } }).init.status).toBe(404);
    } finally {
      vi.unstubAllEnvs();
    }
  });
});

describe("a refused save", () => {
  it("says a name or note has invisible characters, not that it is too long", async () => {
    const origin = "https://clashlens.example";
    vi.stubEnv("CLASHLENS_NOTEBOOK_ENABLED", "true");
    mocks.getWebsiteConfig.mockReturnValue({ publicOrigin: new URL(origin) });
    mocks.saveNotebook.mockRejectedValue(
      new PythonApiError(422, { error: "invalid_characters" }),
    );
    try {
      const change = { action: "edit", base: "B-7K3QX9", note: "Ring\u200b" };
      const request = new Request(`${origin}/notebook/save`, {
        method: "POST",
        headers: { "content-type": "application/x-www-form-urlencoded", Origin: origin },
        body: new URLSearchParams({ change: JSON.stringify(change) }).toString(),
      });
      const answer = (await saveAction({ request } as never)) as unknown as {
        data: { error: string };
        init: { status: number };
      };
      expect([answer.init.status, saveError(answer.data.error)]).toEqual([
        422,
        "Names and notes can't contain invisible characters.",
      ]);
      expect(saveError("invalid_note")).toBe("Notes can be up to 1,000 characters.");
    } finally {
      vi.unstubAllEnvs();
    }
  });
});

describe("past Seasons", () => {
  const SEPTEMBER = "1788757200";
  // The Season of 7 Sep – 5 Oct, read on 20 Oct, in the Season after it.
  const past = (editable: boolean): Notebook => {
    const notebook = parseNotebook({
      ...apiNotebook(),
      season: {
        id: SEPTEMBER,
        start: "2026-09-07T05:00:00Z",
        end: "2026-10-05T05:00:00Z",
        today: 28,
        tomorrow: 28,
        current: false,
        editable,
        editable_until: "2026-11-02T05:00:00Z",
      },
      seasons: [
        { id: "1791176400", start: "2026-10-05T05:00:00Z", end: "2026-11-02T05:00:00Z" },
        { id: SEPTEMBER, start: "2026-09-07T05:00:00Z", end: "2026-10-05T05:00:00Z" },
      ],
    });
    if (!notebook) throw new Error("unreadable notebook");
    return notebook;
  };
  const row = (notebook: Notebook) =>
    render(
      createElement(AccountRow, {
        notebook,
        player: notebook.players[0]!,
        onChange: () => {},
        onAdd: () => {},
        onDays: () => {},
      }),
    );

  it("starts no base in a past Season, and fixes days only while editable", async () => {
    const open = await row(past(true));
    expect(open).not.toContain(">Change<");
    expect(open).not.toContain(LEGEND_ONLY);
    expect(open).not.toContain("today");
    expect(open).toContain("No base on Day 28");
    expect(open).toContain(">Days<");
    expect(await row(past(false))).not.toContain(">Days<");
  });

  it("opens a past Season's bases in that Season, with no + New base", async () => {
    const html = await render(createElement(Library, { notebook: past(false) }));
    expect(html).not.toContain("New base");
    expect(html).toContain(`href="/notebook/bases/B-7K3QX9?season=${SEPTEMBER}"`);
    expect(html.replaceAll("<!-- -->", "")).toContain("ran Day 16 · held 7 of 10");
  });

  it("switches between this Season and past ones", async () => {
    const html = (
      await render(createElement(SeasonSwitcher, { notebook: past(true) }))
    ).replaceAll("<!-- -->", "");
    expect(html).toContain(
      '<option value="1791176400">5 Oct – 2 Nov · this Season</option>',
    );
    expect(html).toContain(
      `<option value="${SEPTEMBER}" selected="">7 Sep – 5 Oct</option>`,
    );
    expect(html).not.toContain("of 28");
  });

  it("words a fixed day from its Season's notebook once the page shows the next", async () => {
    const origin = "https://clashlens.example";
    // September as on its Day 28, before the fix: Ring from Day 9 to 28.
    const before = past(true);
    const player: NotebookPlayer = {
      ...before.players[0]!,
      runs: [{ ...before.players[0]!.runs[0]!, to: 28 }],
      current: null,
      legendDays: Array.from({ length: 28 }, (_, index) => index + 1),
    };
    // After it: Hex from Day 10 to 28, as the private API now reads it.
    const fixed = parseNotebook({
      ...apiNotebook(),
      season: { ...apiNotebook().season, id: SEPTEMBER, today: 28, tomorrow: 28 },
      players: [
        {
          ...apiNotebook().players[0],
          runs: [
            { from: 9, to: 9, base: "B-7K3QX9", record: RECORD },
            { from: 10, to: 28, base: "B-000003", record: RECORD },
          ],
          picks: [
            { day: 9, base: "B-7K3QX9" },
            { day: 10, base: "B-000003" },
          ],
          legend_days: Array.from({ length: 28 }, (_, index) => index + 1),
        },
      ],
      bases: [
        ...apiNotebook().bases,
        { ...apiNotebook().bases[0], id: "B-000003", name: "Hex", status: "retired" },
      ],
    });
    const saved = { base: null, changed: [], undo: { season: SEPTEMBER } };
    vi.stubEnv("CLASHLENS_NOTEBOOK_ENABLED", "true");
    mocks.getWebsiteConfig.mockReturnValue({ publicOrigin: new URL(origin) });
    mocks.saveNotebook.mockResolvedValue(saved);
    mocks.getNotebook.mockResolvedValue(fixed);
    try {
      const change = { action: "pick", season: SEPTEMBER, tag: player.tag, day: 10 };
      const post = async (fields: Record<string, string>) =>
        (await saveAction({
          request: new Request(`${origin}/notebook/save`, {
            method: "POST",
            headers: {
              "content-type": "application/x-www-form-urlencoded",
              Origin: origin,
            },
            body: new URLSearchParams({
              change: JSON.stringify({ ...change, base: "B-000003" }),
              ...fields,
            }).toString(),
          }),
        } as never)) as unknown as {
          data: { saved: typeof saved; notebook: Notebook | null };
        };
      // An ordinary save reads no notebook: the page reloads its own.
      mocks.getNotebook.mockClear();
      expect((await post({})).data.notebook).toBeNull();
      expect(mocks.getNotebook).not.toHaveBeenCalled();
      // A page left open past its Season's end asks for that Season's.
      const answer = await post({ reread: "1" });
      expect(mocks.getNotebook.mock.calls[0]?.slice(1)).toEqual([undefined, SEPTEMBER]);
      // The page has reloaded as October's Day 16.
      const toast = savedToast(
        answer.data.saved,
        answer.data.notebook,
        parseNotebook(apiNotebook()),
        SEPTEMBER,
        dayFixWords(player, 10, "B-000003"),
      );
      expect(toast?.text.trim()).toBe("Hex on curtypie, Days 10–28.");
      expect(toast?.actions.map((item) => [item.label, item.change])).toEqual([
        [
          "Only Day 10",
          {
            action: "pick",
            tag: player.tag,
            day: 11,
            base: "B-7K3QX9",
            season: SEPTEMBER,
          },
        ],
        ["Undo", { action: "undo", undo: { season: SEPTEMBER } }],
      ]);
    } finally {
      vi.unstubAllEnvs();
    }
  });

  it("checks a copy names a Season", () => {
    const copy = { action: "copy", season: SEPTEMBER, base: "B-7K3QX9" };
    expect(parseChange(copy)).toEqual(copy);
    expect(parseChange({ ...copy, season: "2026-09" })).toBeNull();
  });
});
