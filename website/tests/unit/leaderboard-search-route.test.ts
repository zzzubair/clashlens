import type { LoaderFunctionArgs } from "react-router";
import { beforeEach, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  searchLeaderboard: vi.fn(),
  getTrackedLeaderboard: vi.fn(),
}));
vi.mock("../../app/services/python.server", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("../../app/services/python.server")>();
  return { ...actual, createPythonClient: () => mocks };
});
import { loader } from "../../app/routes/tracked-leaderboard";
import { PythonApiError } from "../../app/services/python.server";

const load = async (query: string) =>
  (await loader({
    request: new Request(
      `https://clashlens.example/leaderboards/tracked?view=live&page=1${query}`,
    ),
  } as LoaderFunctionArgs)) as Extract<
    Awaited<ReturnType<typeof loader>>,
    { focusTag: unknown }
  >;

beforeEach(() => {
  vi.resetAllMocks();
  mocks.getTrackedLeaderboard.mockResolvedValue({ page: 1, entries: [] });
});

it("opens the correct page directly for an exact tag", async () => {
  mocks.searchLeaderboard.mockResolvedValue({
    exactTag: "#2PP",
    hasMore: false,
    results: [{ tag: "#2PP", rank: 103, name: "Nova", trophies: 6000 }],
  });
  const response = await load("&q=%232pp").catch((cause: unknown) => cause);
  expect(response).toBeInstanceOf(Response);
  expect((response as Response).headers.get("Location")).toBe(
    "/leaderboards/tracked?view=live&page=2&player=%232PP",
  );
  expect(mocks.getTrackedLeaderboard).not.toHaveBeenCalled();
});

it("returns name matches with their ranks for selection", async () => {
  const search = {
    exactTag: null,
    hasMore: false,
    results: [{ tag: "#2PP", rank: 103, name: "Nova", trophies: 6000 }],
  };
  mocks.searchLeaderboard.mockResolvedValue(search);
  expect((await load("&q=Nova")).search).toEqual(search);
});

it("asks the backend to locate the player again when opening their row", async () => {
  mocks.getTrackedLeaderboard.mockResolvedValue({ page: 3 });
  const result = await load("&player=%232PP");
  expect(mocks.getTrackedLeaderboard).toHaveBeenCalledWith(
    100,
    "live",
    0,
    undefined,
    "#2PP",
  );
  expect(result.leaderboard?.page).toBe(3);
  expect(result.focusTag).toBe("#2PP");
});

it("explains a vanished player and returns to the board", async () => {
  mocks.getTrackedLeaderboard.mockRejectedValueOnce(
    new PythonApiError(404, { error: "leaderboard_not_found" }),
  );
  const result = await load("&player=%232PP");
  expect(result.error?.error.code).toBe("missing");
  expect(result.focusTag).toBeNull();
  expect(result.leaderboard?.page).toBe(1);
});

it("keeps a search failure visible", async () => {
  mocks.searchLeaderboard.mockRejectedValue(
    new PythonApiError(503, { error: "unavailable" }),
  );
  expect((await load("&q=Nova")).error?.error.code).toBe("unavailable");
});

it.each(["&player=bad", `&q=${"x".repeat(81)}`])(
  "rejects malformed search input",
  async (query) => {
    await expect(load(query)).rejects.toMatchObject({ status: 422 });
    expect(mocks.searchLeaderboard).not.toHaveBeenCalled();
    expect(mocks.getTrackedLeaderboard).not.toHaveBeenCalled();
  },
);
