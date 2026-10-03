import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  createStaticHandler,
  createStaticRouter,
  StaticRouterProvider,
} from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  createPythonClient: vi.fn(),
}));

vi.mock("../../app/services/python.server", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../app/services/python.server")>();
  return { ...actual, createPythonClient: mocks.createPythonClient };
});

import { PythonApiError } from "../../app/services/python.server";
import Home, { loader as homeLoader } from "../../app/routes/home";

const search = {
  kind: "player-search",
  query: "Nova",
  exactTag: null,
  results: [],
  users: [],
  knownOnly: true,
};

async function renderHome(data: unknown, search: string) {
  const handler = createStaticHandler([
    { path: "/", Component: Home, loader: () => data },
  ]);
  const context = await handler.query(new Request(`https://clashlens.example/${search}`));
  if (context instanceof Response) throw new Error("unexpected response");
  return renderToString(
    createElement(StaticRouterProvider, {
      router: createStaticRouter(handler.dataRoutes, context),
      context,
      hydrate: false,
    }),
  ).replaceAll("<!-- -->", "");
}

it("keeps the exact player tag link when another player's name matches the tag", async () => {
  const html = await renderHome(
    {
      leaderboard: null,
      query: "#2PP",
      error: null,
      search: {
        exactTag: "#2PP",
        users: [],
        results: [{ tag: "#2PY", name: "#2PP", clan: "Test clan", trophies: 5000 }],
      },
    },
    "?q=%232PP",
  );
  expect(html).toContain('href="/players/%232PP"');
  expect(html).toContain("Open player profile");
  expect(html).not.toContain('href="/players/%232PY"');
});

it("formats the tracked total and explains a logout the server could not record", async () => {
  const data = {
    leaderboard: { entries: [], totalTracked: 13263 },
    query: "",
    error: null,
    search: null,
  };
  expect(await renderHome(data, "")).not.toContain("could not record it");
  const html = await renderHome(data, "?logout=unrecorded");
  expect(html).toContain("Top 0 of 13,263 tracked players");
  expect(html).toContain(
    "You are logged out on this browser, but Clash Lens could not record it.",
  );
  expect(html).not.toContain("log out again");
});

describe("home search loading", () => {
  beforeEach(() => {
    mocks.createPythonClient.mockReset();
  });

  it("starts rankings and search before either has finished", async () => {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    let started = 0;
    const delayed = <T>(value: T) => {
      started += 1;
      return gate.then(() => value);
    };
    mocks.createPythonClient.mockReturnValue({
      getTrackedLeaderboard: vi.fn(() => delayed({ totalTracked: 25 })),
      searchPlayers: vi.fn(() => delayed(search)),
    });

    const loading = homeLoader({
      request: new Request("https://clashlens.example/?q=Nova"),
    } as never);
    try {
      await vi.waitFor(() => expect(started).toBe(2));
    } finally {
      release();
    }
    const result = await loading;
    expect(result.leaderboard).toMatchObject({ totalTracked: 25 });
    expect(result.search).toEqual(search);
  });

  it("shows search results when rankings fail", async () => {
    mocks.createPythonClient.mockReturnValue({
      getTrackedLeaderboard: vi
        .fn()
        .mockRejectedValue(new PythonApiError(503, { error: "unavailable" })),
      searchPlayers: vi.fn().mockResolvedValue(search),
    });
    const result = await homeLoader({
      request: new Request("https://clashlens.example/?q=Nova"),
    } as never);
    expect(result.leaderboard).toBeNull();
    expect(result.search).toEqual(search);
    expect(result.error?.error.code).toBe("unavailable");
  });

  it("keeps the rankings error first when both requests fail", async () => {
    mocks.createPythonClient.mockReturnValue({
      getTrackedLeaderboard: vi
        .fn()
        .mockRejectedValue(new PythonApiError(503, { error: "unavailable" })),
      searchPlayers: vi
        .fn()
        .mockRejectedValue(new PythonApiError(429, { error: "rate_limited" })),
    });
    const result = await homeLoader({
      request: new Request("https://clashlens.example/?q=Nova"),
    } as never);
    expect(result.search).toBeNull();
    expect(result.error?.error.code).toBe("unavailable");
  });
});

it("submitting a normalized exact tag opens its automatic lookup page", async () => {
  await expect(
    homeLoader({
      request: new Request("https://clashlens.example/?q=%20%23lqqp%20"),
    } as never),
  ).rejects.toMatchObject({ status: 302 });
  try {
    await homeLoader({
      request: new Request("https://clashlens.example/?q=%23lqqp"),
    } as never);
  } catch (response) {
    expect((response as Response).headers.get("Location")).toBe("/players/%23LQQP");
  }
});
