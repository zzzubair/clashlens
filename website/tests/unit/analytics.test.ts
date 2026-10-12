import { describe, expect, it } from "vitest";

import {
  loadedPageEvents,
  pageChangeEvents,
  routeOf,
  sanitizeEvent,
  searchInput,
} from "../../app/lib/analytics";
import { loadAnalyticsSettings } from "../../app/server/config.server";

const TOKEN = `phc_${"A1b2C3d4E5".repeat(4)}`;
const HOST = "https://eu.i.posthog.com";

describe("visitor counting settings", () => {
  it.each([
    {},
    { CLASHLENS_POSTHOG_TOKEN: TOKEN },
    { CLASHLENS_POSTHOG_HOST: HOST },
    { CLASHLENS_POSTHOG_TOKEN: TOKEN, CLASHLENS_POSTHOG_HOST: "http://eu.i.posthog.com" },
    { CLASHLENS_POSTHOG_TOKEN: TOKEN, CLASHLENS_POSTHOG_HOST: `${HOST}/capture` },
    { CLASHLENS_POSTHOG_TOKEN: "phx_personal_key", CLASHLENS_POSTHOG_HOST: HOST },
  ])("stays off unless a project token and https host are both set: %o", (env) => {
    expect(loadAnalyticsSettings(env)).toBeNull();
  });

  it("reads the token and host from the environment", () => {
    expect(
      loadAnalyticsSettings({
        CLASHLENS_POSTHOG_TOKEN: ` ${TOKEN} `,
        CLASHLENS_POSTHOG_HOST: `${HOST}/`,
      }),
    ).toEqual({ token: TOKEN, host: HOST });
  });
});

describe("what leaves the browser", () => {
  it("replaces player tags, names and IDs in page addresses", () => {
    expect(routeOf("/players/%232PP")).toEqual({
      route: "player",
      path: "/players/:tag",
    });
    expect(routeOf("/users/someone").path).toBe("/users/:username");
    expect(routeOf("/account/groups/7c1a").path).toBe("/account/groups/:group");
    expect(routeOf("/account/groups/add/2PP").path).toBe("/account/groups/add/:tag");
    expect(routeOf("/crews/join/SECRETCODE").path).toBe("/crews/join/:code");
    expect(routeOf("/crews/new").route).toBe("crew_new");
    expect(routeOf("/")).toEqual({ route: "home", path: "/" });
    expect(routeOf("/typed/by/someone")).toEqual({ route: "other", path: "/other" });
  });

  it("sends only listed events, stripped of addresses, titles, people and location", () => {
    const sent = sanitizeEvent({
      uuid: "u1",
      event: "$pageview",
      properties: {
        distinct_id: "browser-1",
        $host: "clashlens.app",
        $current_url: "https://clashlens.app/players/%232PP?season=1&q=Bob#top",
        $pathname: "/players/%232PP",
        $session_entry_url: "https://clashlens.app/users/someone?x=1",
        $referrer: "https://www.google.com/search?q=bob",
        $initial_referrer: "https://www.google.com/search?q=bob",
        $referring_domain: "www.google.com",
        title: "Bob · Clash Lens",
        utm_source: "newsletter",
        $raw_user_agent: "Mozilla/5.0",
        $browser: "Chrome",
        route: "player",
      },
      $set: { $initial_current_url: "https://clashlens.app/players/%232PP" },
      $set_once: { $initial_pathname: "/players/%232PP" },
    });
    expect(sent).toEqual({
      uuid: "u1",
      event: "$pageview",
      properties: {
        distinct_id: "browser-1",
        $host: "clashlens.app",
        $current_url: "https://clashlens.app/players/:tag",
        $pathname: "/players/:tag",
        $session_entry_url: "https://clashlens.app/users/:username",
        $referrer: "https://www.google.com",
        $referring_domain: "www.google.com",
        $browser: "Chrome",
        route: "player",
        $geoip_disable: true,
      },
      timestamp: undefined,
    });
    for (const event of [
      "$opt_in",
      "$autocapture",
      "$pageleave",
      "$web_vitals",
      "$identify",
    ]) {
      expect(sanitizeEvent({ uuid: "u", event, properties: {} })).toBeNull();
    }
  });

  it("keeps a same-site referrer as a page label, never its IDs", () => {
    const sent = sanitizeEvent({
      uuid: "u2",
      event: "search_submitted",
      properties: {
        $host: "clashlens.app",
        $referrer: "https://clashlens.app/users/someone",
      },
    });
    expect(sent?.properties.$referrer).toBe("https://clashlens.app/users/:username");
  });

  it("describes what was searched for, never the words", () => {
    expect(searchInput("#2pp")).toBe("tag");
    expect(searchInput("@someone")).toBe("username");
    expect(searchInput("Bob the Builder")).toBe("name");
  });
});

describe("events read from the page address", () => {
  const params = (value: string) => new URLSearchParams(value);

  it("names the Rankings controls that changed", () => {
    expect(
      pageChangeEvents(
        "rankings",
        params("view=live&page=1"),
        params("view=daily&page=3"),
      ),
    ).toEqual([
      {
        event: "rankings_changed",
        properties: {
          changed: "view+page",
          view: "daily",
          page: "later",
          season: "current",
        },
      },
    ]);
    expect(
      pageChangeEvents("rankings", params("view=live"), params("view=live&q=bob")),
    ).toEqual([]);
  });

  it("names the Armies controls that changed, with fixed labels only", () => {
    expect(
      pageChangeEvents(
        "armies",
        params("category=troops"),
        params("category=spells&population=trophies&trophy_min=5000&sort=Bob%20Smith"),
      ),
    ).toEqual([
      {
        event: "armies_changed",
        properties: {
          changed: "category+population+sort+trophies",
          category: "spells",
          population: "trophies",
          season: "current",
          sort: "other",
        },
      },
    ]);
  });

  it("counts a player refresh and a Season change", () => {
    expect(
      pageChangeEvents("player", params(""), params("refresh=abc&season=2026-09")),
    ).toEqual([
      { event: "player_detail_changed", properties: { action: "refresh" } },
      {
        event: "player_detail_changed",
        properties: { action: "season", season: "historical" },
      },
    ]);
  });

  it("counts group comparison settings", () => {
    expect(
      pageChangeEvents("group_comparison", params("days=7"), params("days=14")),
    ).toEqual([{ event: "group_used", properties: { action: "change-days" } }]);
  });
});

describe("events read from what a page loaded", () => {
  it("buckets search results without the query", () => {
    expect(
      loadedPageEvents("routes/home", {
        query: "bob",
        search: { results: [{}, {}], users: [{}] },
      }),
    ).toEqual([
      {
        event: "search_results_shown",
        properties: { source: "home", results: "many", kind: "both" },
      },
    ]);
    expect(loadedPageEvents("routes/home", { query: "", search: null })).toEqual([]);
    expect(
      loadedPageEvents("routes/tracked-leaderboard", {
        query: "zzz",
        search: { results: [] },
      }),
    ).toEqual([
      {
        event: "search_results_shown",
        properties: { source: "rankings", results: "zero" },
      },
    ]);
  });

  it("reports how a player lookup ended", () => {
    const state = (data: object) =>
      loadedPageEvents("routes/player", data)[0].properties.state;
    expect(state({ player: {}, lookup: null })).toBe("available");
    expect(state({ player: null, lookup: { state: "checking" } })).toBe("checking");
    expect(state({ player: null, lookup: null, requestedTag: null })).toBe("invalid");
    expect(state({ player: null, lookup: null, requestedTag: "#2PP" })).toBe("failed");
  });
});
