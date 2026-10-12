import { describe, expect, it } from "vitest";

import {
  loadedPageEvents,
  pagePerformance,
  routeOf,
  sanitizeEvent,
  searchInput,
  selectionEvents,
  selectionOf,
} from "../../app/lib/analytics";
import { loadAnalyticsSettings } from "../../app/server/config.server";

const TOKEN = `phc_${"A1b2C3d4E5".repeat(4)}`;

describe("visitor counting settings", () => {
  it.each([{}, { CLASHLENS_POSTHOG_TOKEN: "phx_personal_key" }])(
    "stays off unless a project token is set: %o",
    (env) => {
      expect(loadAnalyticsSettings(env)).toBeNull();
    },
  );

  it("reads the token from the environment", () => {
    expect(loadAnalyticsSettings({ CLASHLENS_POSTHOG_TOKEN: ` ${TOKEN} ` })).toEqual({
      token: TOKEN,
    });
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

  it("sends only listed events with their labels, never browser or device details", () => {
    const sent = sanitizeEvent({
      uuid: "u1",
      event: "$pageview",
      properties: {
        token: TOKEN,
        distinct_id: "browser-1",
        $session_id: "session-1",
        $host: "clashlens.app",
        $current_url: "https://clashlens.app/players/%232PP?season=1&q=Bob#top",
        $pathname: "/players/%232PP",
        $session_entry_url: "https://clashlens.app/users/someone?x=1",
        $prev_pageview_pathname: "/users/someone",
        $referrer: "https://www.google.com/search?q=bob",
        $initial_referrer: "https://www.google.com/search?q=bob",
        $referring_domain: "www.google.com",
        title: "Bob · Clash Lens",
        utm_source: "newsletter",
        $raw_user_agent: "Mozilla/5.0",
        $browser: "Chrome",
        $os: "Linux",
        $device_type: "Desktop",
        $screen_height: 1080,
        $viewport_width: 1280,
        $browser_language: "en-GB",
        $timezone: "Europe/London",
        route: "player",
        signed_in: false,
        source: "home",
      },
      $set: { $initial_current_url: "https://clashlens.app/players/%232PP" },
      $set_once: { $initial_pathname: "/players/%232PP" },
    });
    expect(sent).toEqual({
      uuid: "u1",
      event: "$pageview",
      properties: {
        token: TOKEN,
        distinct_id: "browser-1",
        $session_id: "session-1",
        $host: "clashlens.app",
        $current_url: "https://clashlens.app/players/:tag",
        $pathname: "/players/:tag",
        $referrer: "https://www.google.com",
        $referring_domain: "www.google.com",
        route: "player",
        signed_in: false,
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
      "sign_in_step",
    ]) {
      expect(sanitizeEvent({ uuid: "u", event, properties: {} })).toBeNull();
    }
  });

  it("passes the browser name and version only to count without an ID", () => {
    const sent = (cookieless: boolean) =>
      sanitizeEvent({
        uuid: "u3",
        event: "ui_error",
        properties: {
          $raw_user_agent: "Mozilla/5.0",
          ...(cookieless ? { $cookieless_mode: true } : {}),
        },
      })?.properties;
    expect(sent(true)).toMatchObject({
      $cookieless_mode: true,
      $raw_user_agent: "Mozilla/5.0",
    });
    expect(sent(false)).not.toHaveProperty("$raw_user_agent");
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
    expect(searchInput("@someone")).toBe("name");
    expect(searchInput("Bob the Builder")).toBe("name");
  });
});

describe("events read from what a page shows", () => {
  const board = (view: "live" | "daily", page: number, day?: number) => ({
    leaderboard: {
      view,
      page,
      generatedAt: "2026-10-12T06:00:00Z",
      daily: day
        ? {
            officialSeasonId: "1790000000",
            dayNumber: day,
            seasonEndAt: "2026-10-20T05:00:00Z",
          }
        : null,
    },
  });
  const rankings = (before: object, after: object) =>
    selectionEvents(
      "rankings",
      selectionOf("routes/tracked-leaderboard", before),
      selectionOf("routes/tracked-leaderboard", after)!,
    );

  it("names the Rankings controls that changed, with the shown day and Season", () => {
    expect(rankings(board("live", 1), board("daily", 3, 12))).toEqual([
      {
        event: "rankings_changed",
        properties: {
          changed: "view+page+day+season",
          view: "daily",
          page: "later",
          day: "12",
          season: "current",
        },
      },
    ]);
    expect(rankings(board("daily", 1, 12), board("daily", 1, 11))).toEqual([
      {
        event: "rankings_changed",
        properties: {
          changed: "day",
          view: "daily",
          page: "first",
          day: "11",
          season: "current",
        },
      },
    ]);
    // A search or a reload of the same board changes nothing shown.
    expect(rankings(board("live", 1), board("live", 1))).toEqual([]);
  });

  it("calls a Daily board from an ended Season the previous Season", () => {
    const ended = board("daily", 1, 28);
    ended.leaderboard.generatedAt = "2026-10-21T00:00:00Z";
    expect(selectionOf("routes/tracked-leaderboard", ended)?.labels.season).toBe(
      "previous",
    );
  });

  const armies = (selection: object, extra: object = {}) => ({
    analytics: {
      selection: {
        category: "army-types",
        population: "top-100",
        sort: "usage-rate",
        ...selection,
      },
    },
    historicalSummary: false,
    requestedSeason: "current",
    ...extra,
  });

  it("names the Armies controls that changed, with the shown labels only", () => {
    expect(
      selectionEvents(
        "armies",
        selectionOf("routes/army-analytics", armies({})),
        selectionOf(
          "routes/army-analytics",
          armies({ category: "cc-troops", population: "trophies-5000-5400" }),
        )!,
      ),
    ).toEqual([
      {
        event: "armies_changed",
        properties: {
          changed: "category+population",
          category: "cc-troops",
          population: "trophies",
          season: "current",
          sort: "usage-rate",
        },
      },
    ]);
    expect(
      selectionOf(
        "routes/army-analytics",
        armies(
          { category: "troops", population: "top-100", sort: "alice" },
          { historicalSummary: true, requestedSeason: "1785714000" },
        ),
      )?.labels,
    ).toEqual({
      category: "troops",
      population: "all",
      season: "historical",
      sort: "other",
    });
    // A selection the page refused shows no results, so nothing changed.
    expect(selectionOf("routes/army-analytics", { analytics: null })).toBeNull();
  });

  it("counts a player's Season change but not a day change", () => {
    const player = (selectedSeason: string | null) =>
      selectionOf("routes/player", { requestedTag: "#2PP", selectedSeason });
    expect(selectionEvents("player", player(null), player("1785714000")!)).toEqual([
      {
        event: "player_detail_changed",
        properties: { action: "season", season: "historical" },
      },
    ]);
    expect(selectionEvents("player", player(null), player(null)!)).toEqual([]);
  });

  it("counts opening a group comparison and changing its settings", () => {
    const group = (days: number, sort: string) =>
      selectionOf("routes/account.groups.$groupId", { comparison: {}, days, sort });
    expect(selectionEvents("group_comparison", null, group(7, "trophies")!)).toEqual([
      { event: "group_used", properties: { action: "compare" } },
    ]);
    expect(
      selectionEvents("group_comparison", group(7, "trophies"), group(14, "net")!),
    ).toEqual([
      { event: "group_used", properties: { action: "change-days" } },
      { event: "group_used", properties: { action: "change-sort" } },
    ]);
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

  it("reports the player lookup outcome the page shows", () => {
    const saved = { tag: "#2PP", trackingState: "tracking" };
    const state = (data: object) =>
      loadedPageEvents("routes/player", {
        requestedTag: "#2PP",
        player: null,
        refreshStatus: null,
        lookup: null,
        ...data,
      })[0].properties.state;
    const lookup = (value: string, reason?: string) => ({
      lookup: { tag: "#2PP", state: value, reason },
    });
    expect(state({ player: saved })).toBe("available");
    expect(state({ player: saved, ...lookup("tracking") })).toBe("available");
    // Saved results are hidden when the latest lookup found no player.
    expect(state({ player: saved, ...lookup("not_found") })).toBe("not-found");
    expect(state(lookup("not_in_legend"))).toBe("not-found");
    expect(state(lookup("checking"))).toBe("checking");
    expect(state(lookup("tracking"))).toBe("checking");
    expect(state({ player: saved, ...lookup("tracking", "no_legend_battles") })).toBe(
      "checking",
    );
    expect(state(lookup("uncertain"))).toBe("checking");
    expect(state(lookup("failed"))).toBe("failed");
    expect(state(lookup("unknown"))).toBe("failed");
    expect(state({})).toBe("failed");
    expect(state({ requestedTag: null })).toBe("invalid");
  });
});

describe("page load timing", () => {
  const timing = {
    responseStart: 420,
    loadEventEnd: 2400,
  } as PerformanceNavigationTiming;

  it("sends one load in ten as coarse steps", () => {
    expect(pagePerformance(timing, 0.05)).toEqual({
      event: "page_performance",
      properties: { response: "under-1s", load: "1-3s" },
    });
    expect(pagePerformance(timing, 0.5)).toBeNull();
    expect(pagePerformance({ ...timing, loadEventEnd: 0 }, 0.05)).toBeNull();
  });
});
