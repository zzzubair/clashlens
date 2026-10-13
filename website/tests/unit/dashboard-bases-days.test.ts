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

import { OpponentsCard } from "../../app/components/OpponentsCard";
import { loader } from "../../app/routes/player-bases";
import { loadWebsiteConfig } from "../../app/server/config.server";
import { getPlayerBases } from "../../app/services/dashboard.server";

const ORIGIN = "https://clashlens.example";
const TAG = "#2PP";

/** What GET /v1/players/{tag}/bases?day=3 returned for the saved battles in
 * python/tests/test_dashboard_bases_postgres.py, the typical base only. */
const DAY_3 = {
  tag: TAG,
  season_day_number: 3,
  ranked_day_start: "2026-08-03T05:00:00+00:00",
  opponents: [
    {
      tag: "#8PP",
      name: "Typical",
      reset_trophies: 5400,
      hit: {
        stars: 3,
        destruction_percentage: 100,
        trophy_change: 40,
        battle_timestamp: "2026-08-03T12:30:00Z",
      },
      defenses: [
        { stars: 2, trophy_change: -16, yours: false },
        { stars: 3, trophy_change: -40, yours: true },
      ],
      observed_at: "2026-08-03T14:00:00+00:00",
      legends_held: { held: 58, defenses: 208, lost: 6400 },
    },
  ],
};

async function render(element: ReturnType<typeof createElement>) {
  const handler = createStaticHandler([{ path: "/", Component: () => element }]);
  const context = await handler.query(new Request(`${ORIGIN}/`));
  if (context instanceof Response) throw new Error("unexpected response");
  return renderToString(
    createElement(StaticRouterProvider, {
      router: createStaticRouter(handler.dataRoutes, context),
      context,
      hydrate: false,
    }),
  );
}

beforeEach(() => {
  mocks.requestJson.mockReset();
});

describe("Bases you attacked on earlier Legend days", () => {
  it("reads an earlier day's bases with that day's comparisons", async () => {
    mocks.requestJson.mockResolvedValueOnce(DAY_3);
    const read = await getPlayerBases(TAG, 3);
    expect(mocks.requestJson).toHaveBeenCalledWith(
      "/v1/players/%232PP/bases?day=3",
      "GET",
      undefined,
      undefined,
    );
    expect(read.dayNumber).toBe(3);
    expect(read.rows?.[0]).toMatchObject({
      name: "Typical",
      resetTrophies: 5400,
      defenses: [{ trophyChange: -16 }, { trophyChange: -40, yours: true }],
      legends: { held: 58, defenses: 208, lost: 6400 },
    });

    // A day without the player's battles saved, and a reply for another day.
    mocks.requestJson.mockResolvedValueOnce({ ...DAY_3, opponents: null });
    expect(await getPlayerBases(TAG, 3)).toEqual({ dayNumber: 3, rows: null });
    mocks.requestJson.mockResolvedValueOnce(DAY_3);
    await expect(getPlayerBases(TAG, 4)).rejects.toMatchObject({ status: 502 });
  });

  it("shows the day button on today, with no later day to step to", async () => {
    const card = (dayNumber: number | null) =>
      render(
        createElement(OpponentsCard, {
          tag: TAG,
          dayNumber,
          rows: [],
          timeZone: "UTC",
        }),
      );
    const today = await card(9);
    expect(today).toMatch(/Day <!-- -->9<span[^>]*> · today<\/span>/);
    expect(today).toMatch(/aria-label="Earlier Legend day"(?![^>]*disabled)/);
    expect(today).toMatch(/aria-label="Later Legend day" disabled/);
    // Day 1 has no earlier day; a data service without day numbers, no button.
    expect(await card(1)).toMatch(/aria-label="Earlier Legend day" disabled/);
    const unnumbered = await card(null);
    expect(unnumbered).not.toContain("Legend day");
    expect(unnumbered).toContain("· today · 0 of 8");
  });
});

describe("earlier day read", () => {
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

  it("loads the chosen day for a signed-in user while the dashboard is on", async () => {
    mocks.requestJson.mockResolvedValueOnce(DAY_3);
    const result = await status("/resources/players/%232PP/bases?day=3");
    expect(result.status).toBe(200);
    expect(result.data).toMatchObject({
      bases: { dayNumber: 3, rows: [{ tag: "#8PP" }] },
    });

    mocks.requestJson.mockRejectedValueOnce(new Error("down"));
    expect((await status("/resources/players/%232PP/bases?day=3")).data).toEqual({
      bases: null,
    });

    for (const query of ["day=0", "day=29", "day=2.5", ""])
      expect((await status(`/resources/players/%232PP/bases?${query}`)).status).toBe(400);

    mocks.readLoginIdentity.mockResolvedValueOnce(null);
    expect((await status("/resources/players/%232PP/bases?day=3")).status).toBe(401);

    vi.stubEnv("CLASHLENS_DASHBOARD_ENABLED", "");
    expect((await status("/resources/players/%232PP/bases?day=3")).status).toBe(404);
  });
});
