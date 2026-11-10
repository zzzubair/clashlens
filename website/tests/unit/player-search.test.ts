import { describe, expect, it } from "vitest";

import { suggestionsView } from "../../app/components/PlayerSearch";
import type { PlayerSearchLoaderData } from "../../app/lib/player-search-data";

const answer = (query: string, failed = false): PlayerSearchLoaderData => ({
  query,
  search: failed ? null : ({ query, results: [], users: [], exactTag: null } as never),
  error: failed ? { error: { code: "unavailable", message: "Down." } } : null,
});

describe("search suggestions", () => {
  it("stays closed until the typed text has been asked for", () => {
    expect(suggestionsView("", "", { state: "idle" }).state).toBe("closed");
    // Typing waits out its pause: the last request was for other text.
    expect(suggestionsView("Ali", "Al", { state: "idle", data: answer("Al") })).toEqual({
      state: "closed",
      data: undefined,
    });
  });

  it("searches, then shows the answer for exactly the typed text", () => {
    expect(suggestionsView(" ali ", "ali", { state: "loading" })).toEqual({
      state: "searching",
      data: undefined,
    });
    const found = answer("ALI");
    expect(suggestionsView("ali", "ali", { state: "idle", data: found })).toEqual({
      state: "shown",
      data: found,
    });
    // An earlier answer for other text never shows.
    expect(suggestionsView("ali", "ali", { state: "idle", data: answer("al") })).toEqual({
      state: "closed",
      data: undefined,
    });
  });

  it("shows a failed search as unavailable, and closes once dismissed", () => {
    const failed = answer("ali", true);
    expect(suggestionsView("ali", "ali", { state: "idle", data: failed }).state).toBe(
      "shown",
    );
    // Dismissing clears the requested text.
    expect(suggestionsView("ali", "", { state: "idle", data: failed }).state).toBe(
      "closed",
    );
  });
});
