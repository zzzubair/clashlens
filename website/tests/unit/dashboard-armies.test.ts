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
  readLoginIdentity: vi.fn(),
  requestJson: vi.fn(),
}));

vi.mock("../../app/server/config.server", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/server/config.server")>();
  return { ...actual, getWebsiteConfig: mocks.getWebsiteConfig };
});

vi.mock("../../app/server/actions.server", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../app/server/actions.server")>();
  return { ...actual, readLoginIdentity: mocks.readLoginIdentity };
});

vi.mock("../../app/services/python.server", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../app/services/python.server")>();
  return { ...actual, requestJson: mocks.requestJson };
});

import { ArmiesOnDefenseCard, ArmiesOnOffenseCard } from "../../app/components/ArmyCards";
import type { PlayerArmies } from "../../app/lib/dashboard-armies";
import { loader } from "../../app/routes/player-armies";
import { loadWebsiteConfig } from "../../app/server/config.server";
import { getPlayerArmies } from "../../app/services/dashboard.server";

const ORIGIN = "https://clashlens.example";
const TAG = "#2PP";
const DAY = "2026-08-04T05:00:00+00:00";
const NEXT_DAY = "2026-08-05T05:00:00+00:00";

const outcome = (battles: number, three_stars: number, trophies: number) => ({
  battles,
  three_stars,
  trophies,
});
const hits = (battles: number, three_stars: number) => ({ battles, three_stars });

/** What GET /v1/players/{tag}/armies returned for the saved battles in
 * python/tests/test_dashboard_armies_postgres.py. */
const DEFENSE = {
  tag: TAG,
  lens: "defense",
  days: 3,
  covered_days: [DAY, NEXT_DAY],
  rows: [
    { type_id: 3, name: "Thrower", ...outcome(2, 1, 69), legends: outcome(4, 2, 119) },
    { type_id: 4, name: "Hydra", ...outcome(1, 0, 26), legends: outcome(1, 0, 26) },
  ],
  total: outcome(3, 1, 95),
  legends_total: outcome(5, 2, 145),
};
const OFFENSE = {
  tag: TAG,
  lens: "offense",
  days: 7,
  covered_days: [DAY, NEXT_DAY],
  rows: [{ type_id: 3, name: "Thrower", ...outcome(1, 1, 40), legends: hits(4, 2) }],
  total: outcome(1, 1, 40),
};

/** The card's text, or its markup when `markup` is set. */
async function render(element: ReturnType<typeof createElement>, markup = false) {
  const handler = createStaticHandler([{ path: "/", Component: () => element }]);
  const context = await handler.query(new Request(`${ORIGIN}/`));
  if (context instanceof Response) throw new Error("unexpected response");
  const html = renderToString(
    createElement(StaticRouterProvider, {
      router: createStaticRouter(handler.dataRoutes, context),
      context,
      hydrate: false,
    }),
  );
  if (markup) return html;
  return html
    .replace(/<[^>]+>/g, " ")
    .replace(/&#x27;/g, "'")
    .replace(/\s+/g, " ");
}

async function read(
  payload: object,
  lens: "offense" | "defense",
  days: number | "season",
) {
  mocks.requestJson.mockResolvedValueOnce(payload);
  return getPlayerArmies(TAG, lens, days);
}

beforeEach(() => {
  mocks.requestJson.mockReset();
});

describe("Armies on defense", () => {
  it("shows each army's share, held and loss beside the Legends average", async () => {
    const armies = await read(DEFENSE, "defense", 3);
    expect(mocks.requestJson).toHaveBeenCalledWith(
      `/v1/players/${encodeURIComponent(TAG)}/armies?lens=defense&days=3`,
      "GET",
      undefined,
      undefined,
    );
    const plain = await render(
      createElement(ArmiesOnDefenseCard, { data: { tag: TAG, armies: armies } }),
    );
    // One of the 3 days has no army records, so the window line shows. The
    // headings are one word each; the header says once what they compare with.
    expect(plain).toContain(
      "vs Legend avg 3 days 7 days Custom Last 3 days · 2 with army data · 3 defenses Army · usage Usage Not tripled Per defense Thrower",
    );
    expect(plain.match(/vs Legend avg/g)).toHaveLength(1);
    // Thrower: 2 of 3 defenses, 1 of 2 held, 69 lost; every tracked player
    // held 2 of 4 against it and lost 119. Each number is followed by its one
    // difference from the Legends with its unit; screen readers hear it as a
    // sentence.
    expect(plain).toContain(
      "Thrower 67% of defenses 50% 0 pts Not tripled 50%, same as the Legends average −35 −5 Trophies per defense −35, 5 more lost than the Legends average",
    );
    expect(plain).toContain("Hydra 33% of defenses 100% 0 pts Not tripled 100%,");
    expect(plain).toContain(
      "All 3 defenses 67% +7 pts Not tripled 67%, 7 points above the Legends average −32 −3",
    );
    // The one colouring rule, said once.
    expect(plain).toContain(
      "Green or red when 5+ pts or 3+ trophies from the Legend average.",
    );
    expect(plain.indexOf("Thrower")).toBeLessThan(plain.indexOf("Hydra"));
  });

  it("shows – for the Legends average until a day of it is counted", async () => {
    const armies = await read(
      {
        ...DEFENSE,
        rows: DEFENSE.rows.map((row) => ({ ...row, legends: null })),
        legends_total: null,
      },
      "defense",
      3,
    );
    const plain = await render(
      createElement(ArmiesOnDefenseCard, { data: { tag: TAG, armies } }),
    );
    expect(plain).toContain(
      "Thrower 67% of defenses 50% – Not tripled 50%, no Legends average yet −35 –",
    );
    expect(plain).toContain(
      "All 3 defenses 67% – Not tripled 67%, no Legends average yet",
    );
  });

  it("lists at most 5 armies, and says when there is nothing to show", async () => {
    const rows = Array.from({ length: 7 }, (_, index) => ({
      type_id: index + 3,
      name: `Army ${index + 1}`,
      ...outcome(1, 0, 30 - index),
      legends: outcome(10, 5, 300),
    }));
    const many = await read(
      { ...DEFENSE, days: 7, rows, total: outcome(7, 0, 189) },
      "defense",
      7,
    );
    const plain = await render(
      createElement(ArmiesOnDefenseCard, { data: { tag: TAG, armies: many } }),
    );
    expect(plain).toContain("Army 5");
    expect(plain).not.toContain("Army 6");

    const none = await read(
      {
        ...DEFENSE,
        covered_days: [],
        rows: [],
        total: outcome(0, 0, 0),
      },
      "defense",
      3,
    );
    expect(
      await render(
        createElement(ArmiesOnDefenseCard, { data: { tag: TAG, armies: none } }),
      ),
    ).toContain("No finished Legend days with army data yet.");
  });

  it("colours only a difference well clear of the Legends, beside no average bar", async () => {
    // Every tracked player held 50% and lost 30 a defense against each army.
    const legends = outcome(10, 5, 300);
    const rows = [
      [10, 4, 260],
      [10, 5, 320],
      [10, 6, 340],
      [10, 5, 330],
      [20, 9, 600],
    ].map(([battles, tripled, lost], index) => ({
      type_id: index + 3,
      name: `Army ${index + 1}`,
      ...outcome(battles!, tripled!, lost!),
      legends,
    }));
    const armies = await read(
      { ...DEFENSE, days: 7, rows, total: outcome(50, 25, 1520), legends_total: legends },
      "defense",
      7,
    );
    const html = await render(
      createElement(ArmiesOnDefenseCard, { data: { tag: TAG, armies } }),
      true,
    );
    // 5 points or 3 trophies away is coloured, as the card's rule says; 2
    // trophies away is not. Only the difference is coloured; fewer trophies
    // lost is the better side.
    expect(html.match(/class="army-diff[^"]*" aria-hidden="true">[^<]+/g)).toEqual(
      [
        ["is-better", "+10 pts", "is-better", "+4"],
        ["", "0 pts", "", "−2"],
        ["is-worse", "−10 pts", "is-worse", "−4"],
        ["", "0 pts", "is-worse", "−3"],
        ["is-better", "+5 pts", "", "0"],
        ["", "0 pts", "", "0"],
      ].flatMap(([held, heldText, lost, lostText]) => [
        `class="army-diff${held && ` ${held}`}" aria-hidden="true">${heldText}`,
        `class="army-diff${lost && ` ${lost}`}" aria-hidden="true">${lostText}`,
      ]),
    );
    expect(html).not.toMatch(/army-own is-/);
    expect(html.match(/army-bar/g)).toHaveLength(5);
    expect(html).not.toMatch(/army-bar is-(better|worse)/);
    // A phone shows the top 3 and All defenses until Show all.
    expect(
      html
        .split("army-defense-row is-extra")
        .slice(1)
        .map((row) => /Army \d/.exec(row)?.[0]),
    ).toEqual(["Army 4", "Army 5"]);
    expect(html).toMatch(/aria-expanded="false"[^>]*>Show all 5</);
  });

  it("refuses a malformed read", async () => {
    mocks.requestJson.mockResolvedValueOnce({ ...DEFENSE, days: 7 });
    await expect(getPlayerArmies(TAG, "defense", 3)).rejects.toMatchObject({
      status: 502,
    });
    mocks.requestJson.mockResolvedValueOnce({ ...DEFENSE, total: outcome(1, 2, 0) });
    await expect(getPlayerArmies(TAG, "defense", 3)).rejects.toMatchObject({
      status: 502,
    });
    // An attack row without every tracked player's attacks with that army,
    // and a defense row without their defenses against it.
    const bare = { type_id: 3, name: "Thrower", ...outcome(2, 1, 69) };
    mocks.requestJson.mockResolvedValueOnce({ ...OFFENSE, rows: [bare] });
    await expect(getPlayerArmies(TAG, "offense", 7)).rejects.toMatchObject({
      status: 502,
    });
    mocks.requestJson.mockResolvedValueOnce({ ...DEFENSE, rows: [bare] });
    await expect(getPlayerArmies(TAG, "defense", 3)).rejects.toMatchObject({
      status: 502,
    });
    await expect(getPlayerArmies(TAG, "offense", 5)).rejects.toMatchObject({
      status: 422,
    });
  });
});

describe("Armies on offense", () => {
  it("shows a one-army player's hit rate big beside everyone's", async () => {
    const armies = await read(OFFENSE, "offense", 7);
    const plain = await render(
      createElement(ArmiesOnOffenseCard, { data: { tag: TAG, armies: armies } }),
    );
    expect(plain).toContain("Last 7 days · 2 with army data · 1 attack");
    expect(plain).toContain("1 attack Thrower Your hit rate 100% Legends 50%");
    expect(plain).not.toContain("Tripled");
  });

  it("lists 3 armies and puts the rest under Other armies", async () => {
    const names = ["Thrower", "Super Bowler", "Hydra", "Dragon Rider"];
    const armies: PlayerArmies = await read(
      {
        ...OFFENSE,
        covered_days: Array.from({ length: 7 }, (_, day) =>
          new Date(Date.UTC(2026, 7, 4 + day, 5)).toISOString(),
        ),
        rows: names.map((name, index) => ({
          type_id: index + 3,
          name,
          ...outcome([30, 14, 8, 4][index], [23, 10, 5, 3][index], 0),
          legends: hits(1000, [503, 467, 475, 544][index]),
        })),
        // 2 unreadable attacks, never tripled.
        total: outcome(58, 41, 0),
      },
      "offense",
      7,
    );
    const plain = await render(
      createElement(ArmiesOnOffenseCard, { data: { tag: TAG, armies: armies } }),
    );
    expect(plain).toContain("Last 7 days · 58 attacks");
    // Each army's hit rate beside everyone's with it. Dragon Rider and the 2
    // unreadable attacks: 3 of 6 tripled, a mix with no Legends rate.
    expect(plain).toContain(
      "Army type Your hit rate Legends Thrower 77% 50% Super Bowler 71% 47% Hydra 63% 48% Other armies 50% mixed",
    );
    expect(plain).not.toContain("Dragon Rider");
    expect(plain).not.toContain("of attacks");
    expect(plain).not.toContain("All attacks");
  });

  it("shows this Season's finished days when asked for the Season", async () => {
    // On Day 25: the Season's 24 finished days, 2 of them with army records.
    const armies = await read(
      { ...OFFENSE, days: 24, season: true },
      "offense",
      "season",
    );
    expect(mocks.requestJson).toHaveBeenCalledWith(
      `/v1/players/${encodeURIComponent(TAG)}/armies?lens=offense&days=season`,
      "GET",
      undefined,
      undefined,
    );
    const plain = await render(
      createElement(ArmiesOnOffenseCard, { data: { tag: TAG, armies } }),
    );
    expect(plain).toContain("Armies on offense 3 days 7 days Season");
    expect(plain).toContain("Whole Season · 24 days · 2 with army data · 1 attack");

    // A Season read must say it is one, and cannot cover a whole 28 days
    // while the Season is still running.
    for (const payload of [
      { ...OFFENSE, days: 24 },
      { ...OFFENSE, days: 28, season: true },
    ]) {
      mocks.requestJson.mockResolvedValueOnce(payload);
      await expect(getPlayerArmies(TAG, "offense", "season")).rejects.toMatchObject({
        status: 502,
      });
    }
    await expect(getPlayerArmies(TAG, "defense", "season")).rejects.toMatchObject({
      status: 422,
    });
  });
});

describe("army card switches", () => {
  const args = (path: string) =>
    ({
      request: new Request(`${ORIGIN}${path}`),
      params: { tag: decodeURIComponent(path.split("/")[3]) },
      context: {},
    }) as unknown as Parameters<typeof loader>[0];
  const status = async (path: string) => {
    try {
      const result = (await loader(args(path))) as {
        data: unknown;
        init: { status?: number };
      };
      return { status: result.init?.status ?? 200, data: result.data };
    } catch (thrown) {
      return { status: (thrown as { init: { status: number } }).init.status, data: null };
    }
  };

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
    mocks.readLoginIdentity.mockResolvedValue({
      provider: "google",
      providerSubject: "11223344556677889900",
    });
  });

  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("loads the chosen days for a signed-in user while the dashboard is on", async () => {
    mocks.requestJson.mockResolvedValueOnce({ ...DEFENSE, days: 14 });
    const result = await status("/resources/players/%232PP/armies?lens=defense&days=14");
    expect(result.status).toBe(200);
    expect(result.data).toMatchObject({ armies: { days: 14, total: { battles: 3 } } });

    mocks.requestJson.mockRejectedValueOnce(new Error("down"));
    expect(
      (await status("/resources/players/%232PP/armies?lens=defense&days=3")).data,
    ).toEqual({ armies: null });

    mocks.requestJson.mockResolvedValueOnce({ ...OFFENSE, days: 24, season: true });
    expect(
      await status("/resources/players/%232PP/armies?lens=offense&days=season"),
    ).toMatchObject({ status: 200, data: { armies: { days: 24, season: true } } });

    // Attacks offer 3 days, 7 days or the Season only; defenses 1 to 28 days.
    for (const query of [
      "lens=offense&days=5",
      "lens=defense&days=29",
      "lens=defense&days=season",
      "lens=army&days=3",
    ])
      expect((await status(`/resources/players/%232PP/armies?${query}`)).status).toBe(
        400,
      );

    mocks.readLoginIdentity.mockResolvedValueOnce(null);
    expect(
      (await status("/resources/players/%232PP/armies?lens=defense&days=3")).status,
    ).toBe(401);

    vi.stubEnv("CLASHLENS_DASHBOARD_ENABLED", "");
    expect(
      (await status("/resources/players/%232PP/armies?lens=defense&days=3")).status,
    ).toBe(404);
  });
});
