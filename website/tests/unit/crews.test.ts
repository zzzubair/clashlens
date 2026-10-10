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
import CrewRoute, { loader as crewLoader } from "../../app/routes/crews.$crewId";
import { loader as listLoader } from "../../app/routes/crews";
import { action as createAction, loader as newLoader } from "../../app/routes/crews.new";
import { loadWebsiteConfig } from "../../app/server/config.server";
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
  kind: "crew",
  crew_id: CREW_ID,
  name: "Red Dawn",
  size: 50,
  used: 9,
  my_role: "owner",
  members: [
    {
      username: "zara",
      display_name: "Zara",
      role: "owner",
      you: true,
      players: [{ tag: TAGS[1], name: "Player 2", trophies: 5390, status: "tracking" }],
    },
  ],
  invites: [],
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
  vi.stubEnv("CLASHLENS_DASHBOARD_ENABLED", "true");
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
  async function renderNavigation() {
    return render("/", {
      path: "/",
      Component: App as () => unknown,
      loader: rootLoader as never,
    });
  }

  it("hides every crew page, the create form and the menu link while it is off", async () => {
    vi.stubEnv("CLASHLENS_DASHBOARD_ENABLED", "");
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
    expect(mocks.requestJson).not.toHaveBeenCalled();
    expect(await renderNavigation()).not.toContain('href="/crews"');
  });

  it("shows the menu link while it is on", async () => {
    expect(await renderNavigation()).toContain('href="/crews"');
  });
});

describe("crew board numbers", () => {
  it("shows averages a Legend day with one decimal and today's totals whole", () => {
    expect(formatAverage({ total: 1139, days: 5 }, "attackers", "season")).toBe("+227.8");
    expect(formatAverage({ total: 1504, days: 6 }, "worst_defenders", "week")).toBe(
      "-250.7",
    );
    expect(formatAverage({ total: 120, days: 1 }, "attackers", "today")).toBe("+120");
    expect(formatAverage({ total: 80, days: 1 }, "best_defenders", "today")).toBe("-80");
    expect(formatAverage({ total: 0, days: 3 }, "best_defenders", "season")).toBe("0.0");
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
    expect(html).toContain("9 of 50 places");
    expect(html).toContain("41 open");
    // Seven attackers: five on the card, then See all.
    const attackers = html.slice(html.indexOf('id="board-attackers"'));
    const card = attackers.slice(0, attackers.indexOf("</section>"));
    expect(card.match(/class="crew-row"/g)).toHaveLength(5);
    expect(card).toContain("+227.8");
    expect(card).toContain("See all 7");
    expect(card).toContain("2 accounts have no data here.");
    expect(card).toContain(`href="/crews/${CREW_ID}/boards/attackers#not-on-board"`);
    expect(html).toContain(`href="/players/${encodeURIComponent(TAGS[0]!)}"`);
    expect(html).toMatch(/class="crew-row-you"[^]*?Player 2[^]*?You/);
    expect(html).toContain("No Reset reading yet");
  });

  it("asks for the chosen period and shows today's totals whole", async () => {
    answerCrew("today");
    const html = await render(`/crews/${CREW_ID}?period=today`, crewRoute);
    expect(mocks.requestJson).toHaveBeenCalledWith(
      `/v1/account/crews/${CREW_ID}/boards?period=today`,
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

  it("reads a crew the account is not in as a missing page", async () => {
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
    expect((response as Response).headers.get("Location")).toBe(`/crews/${CREW_ID}`);
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
    expect((response as Response).headers.get("Location")).toBe(`/crews/${CREW_ID}`);
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
    expect((response as Response).headers.get("Location")).toBe(`/crews/${CREW_ID}`);
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
