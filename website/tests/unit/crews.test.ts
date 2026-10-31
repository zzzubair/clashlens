import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  StaticRouterProvider,
} from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getWebsiteConfig: vi.fn(),
  requireLogin: vi.fn(),
  readLoginIdentity: vi.fn(),
  requestJson: vi.fn(),
  checkPlayerTag: vi.fn(),
}));

vi.mock("../../app/server/config.server", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/server/config.server")>();
  return { ...actual, getWebsiteConfig: mocks.getWebsiteConfig };
});

vi.mock("../../app/server/auth-guard.server", () => ({
  requireLogin: mocks.requireLogin,
}));

vi.mock("../../app/server/actions.server", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/server/actions.server")>();
  return { ...actual, readLoginIdentity: mocks.readLoginIdentity };
});

vi.mock("../../app/services/python.server", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../app/services/python.server")>();
  return { ...actual, requestJson: mocks.requestJson };
});

vi.mock("../../app/services/group-players.server", () => ({
  checkPlayerTag: mocks.checkPlayerTag,
}));

import App, { loader as rootLoader } from "../../app/root";
import { formatAverage, mapCrewBoards } from "../../app/lib/crew-contracts";
import CrewBoardRoute, {
  handle as boardHandle,
  loader as boardLoader,
} from "../../app/routes/crews.$crewId.boards.$board";
import CrewRoute, {
  loader as crewLoader,
  shouldRevalidate,
} from "../../app/routes/crews.$crewId";
import { loader as listLoader } from "../../app/routes/crews";
import { action as createAction, loader as newLoader } from "../../app/routes/crews.new";
import { isCrewsEnabled, loadWebsiteConfig } from "../../app/server/config.server";
import { PythonApiError } from "../../app/services/python.server";

const ORIGIN = "https://clashlens.example";
const IDENTITY = { provider: "google", providerSubject: "11223344556677889900" } as const;
const IDEMPOTENCY_KEY = "3be934b5-68fa-4741-8c7b-e03592e4ad70";
const CREW_ID = "6f1c2b9e-4a7d-4c1e-9b2a-0d5e8f7a6b3c";
const TAGS = ["#2PP", "#2QQ", "#2RR", "#2YY", "#2LL", "#2GG", "#2JJ", "#2CC", "#2UU"];

type Args = { request: Request; params: Record<string, string>; context: unknown };
const args = (path: string, params: Record<string, string> = {}): Args => ({
  request: new Request(`${ORIGIN}${path}`),
  params,
  context: {},
});

function createRequest(fields: Record<string, string>): Args {
  return {
    request: new Request(`${ORIGIN}/crews/new`, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded", Origin: ORIGIN },
      body: new URLSearchParams({
        idempotencyKey: IDEMPOTENCY_KEY,
        ...fields,
      }).toString(),
    }),
    params: {},
    context: { get: () => undefined },
  };
}

function unwrap<T>(result: unknown): { data: T; status: number } {
  const wrapped = result as { data: T; init: { status?: number } | null };
  return { data: wrapped.data, status: wrapped.init?.status ?? 200 };
}

const thrown = (run: () => Promise<unknown>) =>
  run().then(
    () => null,
    (error: unknown) => error,
  );

const player = (tag: string, name: string, you = false) => ({ tag, name, you });

/** A boards answer as the private API sends it: seven accounts with data, two without. */
function boardsPayload(period = "season") {
  const averages = TAGS.slice(0, 7).map((tag, index) => ({
    ...player(tag, `Player ${index + 1}`, index === 1),
    total: period === "today" ? 120 - index : 1139 - index * 40,
    days: period === "today" ? 1 : 5,
    battles: 8,
  }));
  const missing = TAGS.slice(7).map((tag, index) => ({
    tag,
    name: `Quiet ${index + 1}`,
    reason: index === 0 ? "not_in_legend" : "no_battles_this_season",
  }));
  const board = (rows: unknown[]) => ({ rows, missing });
  return {
    kind: "crew-boards",
    crew_id: CREW_ID,
    period,
    season_id: "2026-10",
    day_number: 6,
    window_days: [],
    today_start: "2026-10-10T05:00:00+00:00",
    boards: {
      live: board(
        averages.map(({ tag, name, you }, index) => ({
          tag,
          name,
          you,
          trophies: 5400 - index * 10,
          observed_at: "2026-10-10T12:00:00+00:00",
        })),
      ),
      top: board([]),
      attackers: board(averages),
      best_defenders: board(averages.slice(0, 3)),
      worst_defenders: board(averages.slice(0, 3)),
      streaks: board([
        { ...player(TAGS[0]!, "Player 1"), best: 9, going: true, attacks: 30 },
      ]),
    },
  };
}

const crewPayload = {
  kind: "crew-view",
  crew_id: CREW_ID,
  name: "Red Dawn",
  size: 50,
  used: 9,
  my_role: "owner",
  clashers: 3,
  players: [{ tag: TAGS[1], name: "Player 2", trophies: 5390, status: "tracking" }],
};

function answerCrew(period = "season") {
  mocks.requestJson.mockImplementation(async (target: string) =>
    target.includes("/boards") ? boardsPayload(period) : crewPayload,
  );
}

async function render(
  path: string,
  route: { path: string; Component: () => unknown; loader: (a: Args) => unknown },
) {
  const handler = createStaticHandler([
    {
      path: route.path,
      Component: route.Component as () => null,
      loader: route.loader as never,
    },
  ]);
  const context = await handler.query(new Request(`${ORIGIN}${path}`));
  if (context instanceof Response) throw new Error("unexpected response");
  return renderToString(
    createElement(StaticRouterProvider, {
      router: createStaticRouter(handler.dataRoutes, context),
      context,
      hydrate: false,
    }),
  ).replaceAll("<!-- -->", "");
}

beforeEach(() => {
  vi.stubEnv("CLASHLENS_CREWS_ENABLED", "true");
  mocks.getWebsiteConfig.mockReturnValue(
    loadWebsiteConfig({
      NODE_ENV: "test",
      CLASHLENS_LOGIN_ENABLED: "true",
      CLASHLENS_PUBLIC_ORIGIN: ORIGIN,
      CLASHLENS_GOOGLE_CLIENT_ID: "test-client.apps.googleusercontent.com",
      CLASHLENS_GOOGLE_CLIENT_SECRET: "test-client-secret",
      CLASHLENS_DISCORD_CLIENT_ID: "1234567890123456789",
      CLASHLENS_DISCORD_CLIENT_SECRET: "discord-test-secret",
      CLASHLENS_LOGIN_SECRET_B64: "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8",
    }),
  );
  mocks.requireLogin.mockResolvedValue(IDENTITY);
  mocks.readLoginIdentity.mockResolvedValue(null);
  mocks.requestJson.mockReset();
  mocks.requestJson.mockRejectedValue(new PythonApiError(503, { error: "unavailable" }));
  mocks.checkPlayerTag.mockReset();
});

afterEach(() => {
  vi.unstubAllEnvs();
});

describe("crews switch", () => {
  it("has its own setting, separate from the dashboard", () => {
    expect(isCrewsEnabled({ CLASHLENS_DASHBOARD_ENABLED: "true" })).toBe(false);
    expect(isCrewsEnabled({ CLASHLENS_CREWS_ENABLED: "true" })).toBe(true);
  });

  async function renderNavigation() {
    return render("/", {
      path: "/",
      Component: App as () => unknown,
      loader: rootLoader as never,
    });
  }

  it("hides every crew page, the create form and the menu link while it is off", async () => {
    vi.stubEnv("CLASHLENS_CREWS_ENABLED", "");
    const crew = { crewId: CREW_ID };
    for (const run of [
      () => listLoader(args("/crews") as never),
      () => newLoader(args("/crews/new") as never),
      () => createAction(createRequest({ name: "Red Dawn", size: "50" }) as never),
      () => crewLoader(args(`/crews/${CREW_ID}`, crew) as never),
      () =>
        boardLoader(
          args(`/crews/${CREW_ID}/boards/live`, { ...crew, board: "live" }) as never,
        ),
    ]) {
      expect(unwrap(await thrown(run)).status).toBe(404);
    }
    expect(mocks.requireLogin).not.toHaveBeenCalled();
    expect(mocks.readLoginIdentity).not.toHaveBeenCalled();
    expect(mocks.requestJson).not.toHaveBeenCalled();
    expect(await renderNavigation()).not.toContain('href="/crews"');
  });

  it("shows the menu link while it is on", async () => {
    expect(await renderNavigation()).toContain('href="/crews"');
  });
});

describe("crew board numbers", () => {
  it("shows averages a Legend day and today's totals in whole trophies", () => {
    expect(formatAverage({ total: 1139, days: 5 }, "attackers", "season")).toBe("+228");
    expect(formatAverage({ total: 1504, days: 6 }, "worst_defenders", "week")).toBe(
      "−251",
    );
    expect(formatAverage({ total: 120, days: 1 }, "attackers", "today")).toBe("+120");
    expect(formatAverage({ total: 80, days: 1 }, "best_defenders", "today")).toBe("−80");
    expect(formatAverage({ total: 0, days: 3 }, "best_defenders", "season")).toBe("0");
  });

  it("refuses a boards answer that is missing a board or an average without days", () => {
    expect(mapCrewBoards(boardsPayload())).not.toBeNull();
    const missingBoard = boardsPayload();
    delete (missingBoard.boards as Partial<typeof missingBoard.boards>).streaks;
    expect(mapCrewBoards(missingBoard)).toBeNull();
    const noDays = boardsPayload();
    (noDays.boards.attackers.rows[0] as { days: number }).days = 0;
    expect(mapCrewBoards(noDays)).toBeNull();
  });
});

describe("crew page", () => {
  const crewRoute = {
    path: "/crews/:crewId",
    Component: CrewRoute as () => unknown,
    loader: crewLoader as never,
  };

  it("shows each board's top five, who is missing, and links rows to player pages", async () => {
    answerCrew();
    const html = await render(`/crews/${CREW_ID}`, crewRoute);
    expect(html).toContain('<h1 id="crew-title">Red Dawn</h1>');
    // Counts in words a Clasher uses, and the room as a meter with its count.
    expect(html).toContain("9 accounts · 3 Clashers");
    expect(html).toContain('<span aria-hidden="true">9/50</span>');
    expect(html).not.toMatch(/\d places|open</);
    // Seven attackers: five on the card, and the card's title opens all seven.
    const attackers = html.slice(html.indexOf('id="board-attackers"'));
    const card = attackers.slice(0, attackers.indexOf("</section>"));
    expect(card.match(/class="crew-row"/g)).toHaveLength(5);
    expect(card).toContain("+228");
    expect(card).toMatch(
      new RegExp(
        `<a class="crew-board-open" href="/crews/${CREW_ID}/boards/attackers"[^>]*>Top attackers<span class="sr-only">, all 7</span></a>`,
      ),
    );
    expect(card).toContain("2 accounts have no data here.");
    expect(card).toContain(`href="/crews/${CREW_ID}/boards/attackers#not-on-board"`);
    expect(html).toContain(`href="/players/${encodeURIComponent(TAGS[0]!)}"`);
    expect(html).toMatch(/class="crew-row-you"[^]*?Player 2[^]*?You/);
    expect(html).toContain("No Reset reading yet");
  });

  it("ranks every board alike, names the Reset board, sums up the crew's day and shows all", async () => {
    // Today Player 1 won 20 and Player 3 lost 10. Player 2 has not played:
    // it is 10 below the Reset board only by the automatic defense loss its
    // Reset reading was taken before, so its day so far is level.
    const payload = boardsPayload();
    payload.boards.top = {
      rows: [
        { ...player(TAGS[1]!, "Player 2", true), trophies: 5400 },
        { ...player(TAGS[2]!, "Player 3"), trophies: 5390 },
        { ...player(TAGS[0]!, "Player 1"), trophies: 5380 },
      ],
      missing: [],
    };
    payload.boards.live.rows.forEach((row, index) => {
      (row as { today_change?: number | null }).today_change =
        [20, 0, -10][index] ?? null;
    });
    mocks.requestJson.mockImplementation(async (target: string) =>
      target.includes("/boards") ? payload : crewPayload,
    );
    const html = (await render(`/crews/${CREW_ID}`, crewRoute)).replaceAll(
      "<!-- -->",
      "",
    );
    const card = (board: string) => {
      const from = html.slice(html.indexOf(`id="board-${board}"`));
      return from.slice(0, from.indexOf("</section>"));
    };
    // One row style on every board, each board's top three on podium plates
    // and every other rank plain, and on both trophy boards how far each
    // account below first trails, so side-by-side rows line up.
    const podium = [
      'data-podium-rank="1"',
      'data-podium-rank="2"',
      'data-podium-rank="3"',
    ];
    expect(card("live").match(/data-podium-rank="\d"/g)).toEqual(podium);
    const boards = [...html.matchAll(/id="board-([\w-]+)"/g)].map((match) => match[1]!);
    expect(boards).toEqual(expect.arrayContaining(["live", "top", "attackers"]));
    for (const board of boards) {
      const ranks = card(board).match(/class="crew-rank/g) ?? [];
      expect(card(board).match(/data-podium-rank="\d"/g) ?? []).toEqual(
        // Worst defenders ranks the worst first, so its plates win no medal.
        board === "worst_defenders" ? [] : podium.slice(0, Math.min(3, ranks.length)),
      );
    }
    expect(card("live")).toContain(
      '<span class="crew-behind">10 behind<span class="sr-only"> first</span></span>',
    );
    expect(card("top")).toContain(
      '<span class="crew-behind">20 behind<span class="sr-only"> first</span></span>',
    );
    // Live rows also say how each account has done since the Reset, signed
    // and coloured; the Reset board itself has nothing to compare.
    expect(card("live")).toContain('<small class="score-positive">+20 today</small>');
    expect(
      card("live").match(/<small class="score-negative">−10 today<\/small>/g),
    ).toHaveLength(1);
    expect(card("live")).toContain("<small>0 today</small>");
    expect(card("top")).not.toContain(" today</small>");
    // A defense's loss takes the minus sign, not a hyphen.
    expect(card("best_defenders")).toContain('<span class="crew-value">−228<small>');
    expect(card("best_defenders")).not.toMatch(/>-\d/);
    expect(card("top")).toMatch(/>At last Reset<span class="sr-only">/);
    expect(card("top")).toContain("Trophies when this Legend day began");
    // A board longer than its card opens in full from Show all under it; a
    // short one needs none.
    expect(card("attackers")).toMatch(
      /<a class="show-all" href="\/crews\/[^"]+\/boards\/[^"]+"[^>]*>Show all 7<span class="sr-only"> on /,
    );
    expect(card("top")).not.toContain("Show all");
    // The crew's day under its name: the three accounts whose day is known
    // counted up, level and down, and their trophies today added together.
    const day = html.slice(
      html.indexOf('<section class="crew-day"'),
      html.indexOf('<div class="crew-tabs-dock">'),
    );
    expect(day).toContain("Since the last Reset");
    expect(day).toContain('Crew total <b class="crew-day-up">+10</b>');
    expect(day).toMatch(
      /<span class="crew-day-up">1 up<\/span><span class="crew-day-bar" aria-hidden="true">.*<\/span><span>1 level · <span class="crew-day-down">1 down<\/span><\/span>/,
    );
  });

  it("shows a signed-out visitor the boards and accounts, but nothing to manage", async () => {
    mocks.requestJson.mockImplementation(async (target: string) =>
      target.includes("/boards")
        ? boardsPayload()
        : { ...crewPayload, my_role: null, clashers: null },
    );
    const html = await render(`/crews/${CREW_ID}`, crewRoute);
    expect(mocks.requireLogin).not.toHaveBeenCalled();
    expect(mocks.requestJson).toHaveBeenCalledWith(
      `/v1/crews/${CREW_ID}`,
      "GET",
      undefined,
      "crew-view",
      undefined,
      undefined,
    );
    expect(html).toContain('<h1 id="crew-title">Red Dawn</h1>');
    expect(html).toContain("By invite only");
    // The Live leaderboard names every account; there is no second list.
    expect(html).toMatch(/aria-labelledby="board-live"[^]*?Player 2/);
    for (const hidden of ["Invite", "Members", "Edit", "Clasher", "Owner"]) {
      expect(html).not.toContain(hidden);
    }
  });

  it("asks for the chosen period and shows today's totals whole", async () => {
    mocks.readLoginIdentity.mockResolvedValue(IDENTITY);
    answerCrew("today");
    const html = await render(`/crews/${CREW_ID}?period=today`, crewRoute);
    expect(mocks.requestJson).toHaveBeenCalledWith(
      `/v1/crews/${CREW_ID}/boards?period=today`,
      "GET",
      undefined,
      "crew-boards",
      undefined,
      IDENTITY,
    );
    expect(html).toContain("+120");
    expect(html).toMatch(/aria-current="page"[^>]*>Today<\/a>/);
  });

  it("says which battles a board is missing when only the other kind were seen", async () => {
    const payload = boardsPayload("today");
    payload.boards.attackers.rows = [];
    payload.boards.streaks.rows = [];
    mocks.requestJson.mockImplementation(async (target: string) =>
      target.includes("/boards") ? payload : crewPayload,
    );
    const html = await render(`/crews/${CREW_ID}?period=today`, crewRoute);
    expect(html.match(/No attacks yet today/g)).toHaveLength(2);
    expect(html).not.toContain("No battles");
  });

  it("shows a phone the board picked in the address, with its tab, keeping it across periods", async () => {
    answerCrew("week");
    const html = await render(
      `/crews/${CREW_ID}?board=worst-defenders&period=week`,
      crewRoute,
    );
    // Every board is on the page for wider screens; only the picked one is current.
    expect(html.match(/data-current=""/g)).toHaveLength(2);
    expect(html).toMatch(/aria-labelledby="board-worst_defenders" data-current=""/);
    expect(html).toMatch(/class="crew-period-bar" data-current=""/);
    expect(html).not.toContain('id="board-best_defenders"');
    // Worst first is no award: its top three keep the plate's shape, plain,
    // while every other board's take the podium colours.
    const worst = html.slice(html.indexOf('id="board-worst_defenders"'));
    const worstCard = worst.slice(0, worst.indexOf("</section>"));
    expect(worstCard.match(/class="crew-rank rank-mark"/g)).toHaveLength(3);
    expect(worstCard).not.toContain("data-podium-rank");
    expect(html).toContain('data-podium-rank="1"');
    expect(html).toMatch(/aria-current="page"[^>]*>Defense<\/a>/);
    expect(html).toContain(
      `href="/crews/${CREW_ID}?board=worst-defenders&amp;period=today"`,
    );
    expect(html).toContain(
      `href="/crews/${CREW_ID}?board=best-defenders&amp;period=week"`,
    );
  });

  it("reads the boards again for a new period but not for another board", () => {
    const check = (from: string, to: string) =>
      shouldRevalidate({
        currentUrl: new URL(`${ORIGIN}/crews/${CREW_ID}${from}`),
        nextUrl: new URL(`${ORIGIN}/crews/${CREW_ID}${to}`),
        defaultShouldRevalidate: true,
      } as never);
    expect(check("", "?board=streaks")).toBe(false);
    expect(check("?board=streaks&period=week", "?period=week")).toBe(false);
    expect(check("?board=streaks", "?board=streaks&period=week")).toBe(true);
  });

  it("reads a missing crew as a missing page", async () => {
    mocks.requestJson.mockRejectedValue(
      new PythonApiError(404, { error: "crew_not_found" }),
    );
    const error = await thrown(() =>
      crewLoader(args(`/crews/${CREW_ID}`, { crewId: CREW_ID }) as never),
    );
    expect(unwrap(error).status).toBe(404);
  });

  it("lists everyone on the full board and why the rest are not on it", async () => {
    answerCrew();
    const html = await render(`/crews/${CREW_ID}/boards/attackers`, {
      path: "/crews/:crewId/boards/:board",
      Component: CrewBoardRoute as () => unknown,
      loader: boardLoader as never,
    });
    expect(html.match(/class="crew-row"/g)).toHaveLength(7);
    expect(html).toContain("Not on this board (2)");
    expect(html).toContain("Not in Legend League");
    expect(html).toContain("No Legend battles this Season");
  });

  it("leads Back from a board to its crew, on the same period", async () => {
    answerCrew("week");
    const handler = createStaticHandler([
      {
        id: "root",
        path: "/",
        loader: rootLoader as never,
        Component: App,
        children: [
          {
            path: "crews/:crewId/boards/:board",
            Component: CrewBoardRoute,
            loader: boardLoader as never,
            handle: boardHandle,
          },
        ],
      },
    ]);
    const context = await handler.query(
      new Request(`${ORIGIN}/crews/${CREW_ID}/boards/streaks?period=week`),
    );
    if (context instanceof Response) throw new Error("unexpected response");
    const html = renderToString(
      createElement(StaticRouterProvider, {
        router: createStaticRouter(handler.dataRoutes, context),
        context,
        hydrate: false,
      }),
    ).replaceAll("<!-- -->", "");
    expect(html).toMatch(
      new RegExp(`class="back-link" href="/crews/${CREW_ID}\\?period=week"[^]*?Red Dawn`),
    );
  });
});

describe("create a crew", () => {
  it("makes the crew with the picked accounts and opens it", async () => {
    mocks.requestJson.mockResolvedValue({ crew_id: CREW_ID, name: "Red Dawn", size: 20 });
    const response = await thrown(() =>
      createAction(
        createRequest({
          name: " Red Dawn ",
          size: "20",
          [`join:${TAGS[0]}`]: "on",
        }) as never,
      ),
    );
    expect((response as Response).headers.get("Location")).toBe(
      `/crews/${CREW_ID}?invite=1`,
    );
    const [target, method, body, , key] = mocks.requestJson.mock.calls[0]!;
    expect([target, method, key]).toEqual(["/v1/account/crews", "POST", IDEMPOTENCY_KEY]);
    expect(JSON.parse((body as Buffer).toString())).toEqual({
      name: "Red Dawn",
      size: 20,
      tags: [TAGS[0]],
    });
  });

  it("asks for an account before sending anything", async () => {
    const result = unwrap<{ fieldErrors: { accounts?: string } }>(
      await createAction(createRequest({ name: "Red Dawn", size: "20" }) as never),
    );
    expect(result.status).toBe(400);
    expect(result.data.fieldErrors.accounts).toBe("Pick at least one account.");
    expect(mocks.requestJson).not.toHaveBeenCalled();
  });

  it("checks an account Clash Lens has not checked yet, then makes the crew", async () => {
    mocks.requestJson
      .mockRejectedValueOnce(
        new PythonApiError(409, {
          error: "player_not_checked",
          state: "unknown",
          tag: TAGS[0],
        }),
      )
      .mockResolvedValueOnce({ crew_id: CREW_ID, name: "Red Dawn", size: 20 });
    mocks.checkPlayerTag.mockResolvedValue({ tag: TAGS[0], state: "tracking" });
    const response = await thrown(() =>
      createAction(
        createRequest({
          name: "Red Dawn",
          size: "20",
          [`join:${TAGS[0]}`]: "on",
        }) as never,
      ),
    );
    expect((response as Response).headers.get("Location")).toBe(
      `/crews/${CREW_ID}?invite=1`,
    );
    expect(mocks.checkPlayerTag).toHaveBeenCalledWith(undefined, TAGS[0]);
    // The retry is a new request, since the first one's refusal is stored.
    expect(mocks.requestJson.mock.calls[1]![4]).not.toBe(IDEMPOTENCY_KEY);
  });

  it("says when an account is not in Legend League", async () => {
    mocks.requestJson.mockRejectedValue(
      new PythonApiError(422, { error: "player_not_in_legend", tag: TAGS[0] }),
    );
    const result = unwrap<{ idempotencyKey: string; fieldErrors: { accounts?: string } }>(
      await createAction(
        createRequest({
          name: "Red Dawn",
          size: "20",
          [`join:${TAGS[0]}`]: "on",
        }) as never,
      ),
    );
    expect(result.data.fieldErrors.accounts).toBe(
      `${TAGS[0]} is not in Legend League, so it can't join.`,
    );
    expect(result.data.idempotencyKey).not.toBe(IDEMPOTENCY_KEY);
  });

  it("keeps the form's key after a lost answer, so Create again replays the same requests", async () => {
    const fields = { name: "Red Dawn", size: "20", [`join:${TAGS[0]}`]: "on" };
    mocks.checkPlayerTag.mockResolvedValue({ tag: TAGS[0], state: "tracking" });
    mocks.requestJson
      .mockRejectedValueOnce(
        new PythonApiError(409, { error: "player_not_checked", tag: TAGS[0] }),
      )
      .mockRejectedValueOnce(new PythonApiError(503, { error: "timeout" }))
      .mockRejectedValueOnce(
        new PythonApiError(409, { error: "player_not_checked", tag: TAGS[0] }),
      )
      .mockResolvedValueOnce({ crew_id: CREW_ID, name: "Red Dawn", size: 20 });
    const lost = unwrap<{ idempotencyKey: string }>(
      await createAction(createRequest(fields) as never),
    );
    expect(lost.status).toBe(503);
    expect(lost.data.idempotencyKey).toBe(IDEMPOTENCY_KEY);
    const response = await thrown(() => createAction(createRequest(fields) as never));
    expect((response as Response).headers.get("Location")).toBe(
      `/crews/${CREW_ID}?invite=1`,
    );
    const keys = mocks.requestJson.mock.calls.map((call) => call[4]);
    expect(keys[0]).toBe(IDEMPOTENCY_KEY);
    expect(keys[1]).not.toBe(IDEMPOTENCY_KEY);
    expect(keys.slice(2)).toEqual(keys.slice(0, 2));
  });

  it("keeps the form's key while an account is still being checked", async () => {
    mocks.requestJson.mockRejectedValue(
      new PythonApiError(409, { error: "player_not_checked", tag: TAGS[0] }),
    );
    mocks.checkPlayerTag.mockResolvedValue({ tag: TAGS[0], state: "checking" });
    const result = unwrap<{ idempotencyKey: string; fieldErrors: { accounts?: string } }>(
      await createAction(
        createRequest({
          name: "Red Dawn",
          size: "20",
          [`join:${TAGS[0]}`]: "on",
        }) as never,
      ),
    );
    expect(result.data.fieldErrors.accounts).toContain(`Still checking ${TAGS[0]}`);
    expect(result.data.idempotencyKey).toBe(IDEMPOTENCY_KEY);
  });

  it("keeps the form and its key when checking an account fails", async () => {
    mocks.requestJson.mockRejectedValue(
      new PythonApiError(409, { error: "player_not_checked", tag: TAGS[0] }),
    );
    mocks.checkPlayerTag.mockRejectedValue(
      new PythonApiError(429, { error: "rate_limited" }),
    );
    const result = unwrap<{
      idempotencyKey: string;
      values: { tags: string[] };
      generalError: { error: { code: string } };
    }>(
      await createAction(
        createRequest({
          name: "Red Dawn",
          size: "20",
          [`join:${TAGS[0]}`]: "on",
        }) as never,
      ),
    );
    expect(result.status).toBe(429);
    expect(result.data.values.tags).toEqual([TAGS[0]]);
    expect(result.data.generalError.error.code).toBe("rate_limited");
    expect(result.data.idempotencyKey).toBe(IDEMPOTENCY_KEY);
  });
});
