import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  loadPlayerSuggestions,
  SearchSuggestions,
  suggestionsView,
} from "../../app/components/PlayerSearch";
import type { PlayerSearchLoaderData } from "../../app/lib/player-search-data";
import { possiblePlayerTag, searchedPlayerTag } from "../../app/lib/player-tag";
import { loader as searchLoader } from "../../app/routes/search";

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
    expect(suggestionsView("ali", "", { state: "idle", data: failed }).state).toBe(
      "closed",
    );
  });

  it("offers an inline Try again button on error and opens bare tags without a leading #", () => {
    const failedHtml = renderToString(
      createElement(
        MemoryRouter,
        null,
        createElement(SearchSuggestions, {
          id: "test-search-failed",
          data: answer("ali", true),
          loading: false,
          onRetry: () => {},
        }),
      ),
    );
    expect(failedHtml).toContain("Search unavailable.");
    expect(failedHtml).toContain(">Try again</button>");

    const bareTagHtml = renderToString(
      createElement(
        MemoryRouter,
        null,
        createElement(SearchSuggestions, {
          id: "test-search-tag",
          data: answer("2py0lq"),
          loading: false,
        }),
      ),
    );
    expect(bareTagHtml.replaceAll("<!-- -->", "")).toContain("Open #2PY0LQ");
    expect(bareTagHtml).toContain('href="/players/%232PY0LQ"');
  });
});

const suggestionsHtml = (props: Record<string, unknown>) =>
  renderToString(
    createElement(
      MemoryRouter,
      null,
      createElement(SearchSuggestions, { id: "test-search", ...props } as never),
    ),
  ).replaceAll("<!-- -->", "");

describe("bare player tags", () => {
  it("opens only a #tag, and offers text without the # as a possible tag", () => {
    expect(searchedPlayerTag(" #2py0lq ")).toBe("#2PY0LQ");
    expect(searchedPlayerTag("#curry")).toBe("#CURRY");
    expect(searchedPlayerTag("2py0lq")).toBeNull();
    expect(searchedPlayerTag("Curry2")).toBeNull();
    expect(searchedPlayerTag("#2PY0LA")).toBeNull();
    expect(possiblePlayerTag(" 2py0lq ")).toBe("#2PY0LQ");
    expect(possiblePlayerTag("Curry2")).toBe("#CURRY2");
    expect(possiblePlayerTag("YQQ")).toBe("#YQQ");
    expect(possiblePlayerTag("Ali 2")).toBeNull();
  });

  it("opens the player page when the search page gets a #tag", async () => {
    const response = await searchLoader({
      request: new Request("https://clashlens.example/search?q=%232py0lq"),
    } as never).catch((thrown: unknown) => thrown);
    expect(response).toBeInstanceOf(Response);
    expect((response as Response).headers.get("Location")).toBe("/players/%232PY0LQ");
  });

  it("still offers the typed tag when the name search fails", () => {
    const html = suggestionsHtml({
      data: answer("curry2", true),
      loading: false,
      onRetry: () => {},
    });
    expect(html).toContain("Search unavailable.");
    expect(html).toContain("Open #CURRY2");
    expect(html).toContain('href="/players/%23CURRY2"');
  });

  it("offers a short bare tag as a suggestion", () => {
    expect(suggestionsHtml({ data: answer("lq2"), loading: false })).toContain(
      "Open #LQ2",
    );
  });

  it("offers a letter-only tag as a choice beside name results", () => {
    const html = suggestionsHtml({ data: answer("yqq"), loading: false });
    expect(html).toContain("Open #YQQ");
    expect(html).toContain('href="/players/%23YQQ"');
    expect(suggestionsHtml({ data: answer("ali"), loading: false })).not.toContain(
      "Open #",
    );
  });
});

describe("suggestion requests", () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  const stalledReply = () =>
    vi.fn((_url: string, init: RequestInit) =>
      Promise.resolve({
        json: () =>
          new Promise((_resolve, reject) =>
            init.signal?.addEventListener("abort", () => reject(new Error("aborted"))),
          ),
      }),
    );

  it("answers Search unavailable when the reply stalls past the deadline", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", stalledReply());
    const pending = loadPlayerSuggestions("ali", new AbortController().signal, 1_000);
    await vi.advanceTimersByTimeAsync(1_000);
    expect(await pending).toEqual({
      query: "ali",
      search: null,
      error: { error: { code: "unavailable", message: "Search did not answer." } },
    });
  });

  it("answers nothing once cancelled, and stops the download", async () => {
    const fetch = stalledReply();
    vi.stubGlobal("fetch", fetch);
    const controller = new AbortController();
    const pending = loadPlayerSuggestions("ali", controller.signal);
    await Promise.resolve();
    controller.abort();
    expect(await pending).toBeNull();
    expect(fetch.mock.calls[0][1].signal?.aborted).toBe(true);
  });

  it("disables Try again while the retry is running", () => {
    const retrying = suggestionsHtml({
      data: answer("ali", true),
      loading: true,
      onRetry: () => {},
    });
    expect(retrying).toMatch(/<button[^>]*disabled=""[^>]*>Trying again…<\/button>/);
  });
});
