/**
 * Visitor counting with PostHog. Nothing loads, and nothing is sent, unless
 * the server hands over a project token and host. Everyone is then counted
 * without anything kept on their device (PostHog's cookieless mode). The
 * return-visits switch, on by default, adds a random ID kept in this browser
 * so a returning browser can be recognised; switching it off deletes that ID,
 * goes back to cookieless counting, and is remembered here.
 *
 * Only the events and labels below ever leave the browser: page addresses
 * lose player tags, usernames, group and crew IDs, query strings and titles,
 * and no IP-based location is looked up.
 */
import type {
  CaptureResult,
  PostHog,
  Properties,
} from "posthog-js/dist/module.slim.no-external";

import { normalizePlayerTag } from "./player-tag";

export interface AnalyticsSettings {
  token: string;
  host: string;
}

export type EventProperties = Record<string, string | boolean>;

export interface AnalyticsEvent {
  event: string;
  properties: EventProperties;
}

/**
 * This browser's choice: "off" once a visitor switches return visits off,
 * otherwise when the current browser ID was made, so it can be replaced.
 */
export const RETURN_VISITS_KEY = "clashlens-return-visits";
/** About 13 months; the browser ID is then replaced with a new one. */
export const BROWSER_ID_LIFETIME_MS = 395 * 24 * 60 * 60 * 1000;

const EVENTS = new Set([
  "$pageview",
  "search_submitted",
  "search_results_shown",
  "search_result_opened",
  "player_lookup_result",
  "player_detail_changed",
  "rankings_changed",
  "armies_changed",
  "copy_army_clicked",
  "group_used",
  "sign_in_step",
  "ui_error",
]);

// Every page the site serves, as [label, address with its IDs replaced].
const ROUTES: ReadonlyArray<readonly [string, string]> = [
  ["home", "/"],
  ["dashboard", "/dashboard"],
  ["player", "/players/:tag"],
  ["rankings", "/leaderboards/tracked"],
  ["armies", "/analytics/armies"],
  ["blog", "/blog"],
  ["blog_post", "/blog/:post"],
  ["login", "/login"],
  ["sign_in_callback", "/auth/google/callback"],
  ["sign_in_callback", "/auth/discord/callback"],
  ["account", "/account"],
  ["account_setup", "/account/setup"],
  ["account_profile", "/account/profile"],
  ["account_providers", "/account/providers"],
  ["verify_player", "/account/verify-player"],
  ["groups", "/account/groups"],
  ["group_add", "/account/groups/add/:tag"],
  ["group_comparison", "/account/groups/:group"],
  ["crews", "/crews"],
  ["crew_new", "/crews/new"],
  ["crew_join", "/crews/join/:code"],
  ["crew", "/crews/:crew"],
  ["crew_board", "/crews/:crew/boards/:board"],
  ["crew_members", "/crews/:crew/members"],
  ["crew_settings", "/crews/:crew/settings"],
  ["public_profile", "/users/:username"],
  ["privacy", "/privacy"],
];

/** The page's label and its address with every ID replaced by a placeholder. */
export function routeOf(pathname: string): { route: string; path: string } {
  const parts = pathname.split("/").filter(Boolean);
  for (const [route, template] of ROUTES) {
    const expected = template.split("/").filter(Boolean);
    if (
      expected.length === parts.length &&
      expected.every((part, index) => part.startsWith(":") || part === parts[index])
    ) {
      return { route, path: template };
    }
  }
  return { route: "other", path: "/other" };
}

function safeUrl(value: string): string | undefined {
  try {
    const url = new URL(value);
    return `${url.origin}${routeOf(url.pathname).path}`;
  } catch {
    return undefined;
  }
}

// Another site's address keeps only its origin; this site's loses its IDs.
function safeReferrer(value: string, host: unknown): string | undefined {
  if (value === "$direct") return value;
  try {
    const url = new URL(value);
    return url.host === host ? safeUrl(value) : url.origin;
  } catch {
    return undefined;
  }
}

function safeProperty(key: string, value: unknown, host: unknown): unknown {
  if (
    /title|raw_user_agent|^\$initial_|search|query|hash|utm_|clid|click_id/i.test(key)
  ) {
    return undefined;
  }
  if (/referrer/i.test(key)) {
    return typeof value === "string" ? safeReferrer(value, host) : undefined;
  }
  if (/url|href/i.test(key))
    return typeof value === "string" ? safeUrl(value) : undefined;
  if (/pathname/i.test(key)) {
    return typeof value === "string" ? routeOf(value).path : undefined;
  }
  return value;
}

/**
 * PostHog's last step before sending. Drops any event not on the list and any
 * person properties, and rewrites or removes every property that could carry
 * a page address, search, title or campaign tag.
 */
export function sanitizeEvent(event: CaptureResult | null): CaptureResult | null {
  if (!event || !EVENTS.has(event.event)) return null;
  const properties: Properties = {};
  for (const [key, value] of Object.entries(event.properties)) {
    const safe = safeProperty(key, value, event.properties.$host);
    if (safe !== undefined) properties[key] = safe;
  }
  properties.$geoip_disable = true;
  return {
    uuid: event.uuid,
    event: event.event,
    properties,
    timestamp: event.timestamp,
  };
}

/** What kind of thing was typed into a search box, never what was typed. */
export function searchInput(value: string): string {
  if (normalizePlayerTag(value)) return "tag";
  return value.trim().startsWith("@") ? "username" : "name";
}

export function countBucket(count: number): string {
  return count === 0 ? "zero" : count === 1 ? "one" : "many";
}

function slug(value: string | null, fallback: string): string {
  if (value === null) return fallback;
  return /^[a-z0-9-]{1,32}$/.test(value) ? value : "other";
}

const ARMY_CONTROLS: Record<string, string> = {
  category: "category",
  cc: "category",
  population: "population",
  trophy_min: "trophies",
  trophy_max: "trophies",
  season: "season",
  sort: "sort",
  offset: "page",
  saved: "saved",
  recent: "recent",
  sample: "sample",
};

function changedKeys(before: URLSearchParams, after: URLSearchParams): Set<string> {
  const keys = new Set([...before.keys(), ...after.keys()]);
  return new Set(
    [...keys].filter((key) => before.getAll(key).join() !== after.getAll(key).join()),
  );
}

/**
 * Events for a change of settings on the same page, read from the address
 * the page moved to, so the pages themselves need no tracking code.
 */
export function pageChangeEvents(
  route: string,
  before: URLSearchParams,
  after: URLSearchParams,
): AnalyticsEvent[] {
  const changed = changedKeys(before, after);
  if (route === "rankings") {
    const controls = ["view", "page", "day", "season"].filter((key) => changed.has(key));
    if (controls.length === 0) return [];
    return [
      {
        event: "rankings_changed",
        properties: {
          changed: controls.join("+"),
          view: after.get("view") === "daily" ? "daily" : "live",
          page: Number(after.get("page") ?? "1") > 1 ? "later" : "first",
          season: after.has("season") ? "previous" : "current",
        },
      },
    ];
  }
  if (route === "armies") {
    const controls = [
      ...new Set([...changed].flatMap((key) => ARMY_CONTROLS[key] ?? [])),
    ].sort();
    if (controls.length === 0) return [];
    const population = after.get("population");
    return [
      {
        event: "armies_changed",
        properties: {
          changed: controls.join("+"),
          category: slug(after.get("category"), "default"),
          population: population?.startsWith("trophies")
            ? "trophies"
            : slug(population, "default"),
          season: (after.get("season") ?? "current") === "current" ? "current" : "past",
          sort: slug(after.get("sort"), "default"),
        },
      },
    ];
  }
  if (route === "player") {
    const events: AnalyticsEvent[] = [];
    if (changed.has("refresh") && after.has("refresh")) {
      events.push({ event: "player_detail_changed", properties: { action: "refresh" } });
    }
    if (changed.has("season")) {
      events.push({
        event: "player_detail_changed",
        properties: {
          action: "season",
          season: after.has("season") ? "historical" : "current",
        },
      });
    }
    if (changed.has("day")) {
      events.push({ event: "player_detail_changed", properties: { action: "day" } });
    }
    return events;
  }
  if (route === "group_comparison") {
    return ["days", "sort"]
      .filter((key) => changed.has(key))
      .map((key) => ({
        event: "group_used",
        properties: { action: key === "days" ? "change-days" : "change-sort" },
      }));
  }
  return [];
}

/** Events read from what a page loaded: search results and player lookups. */
export function loadedPageEvents(routeId: string, data: unknown): AnalyticsEvent[] {
  if (!data || typeof data !== "object") return [];
  const page = data as Record<string, unknown>;
  if (
    (routeId === "routes/home" || routeId === "routes/tracked-leaderboard") &&
    page.query
  ) {
    const search = page.search as { results: unknown[]; users?: unknown[] } | null;
    const players = search?.results.length ?? 0;
    const profiles = search?.users?.length ?? 0;
    const properties: EventProperties = {
      source: routeId === "routes/home" ? "home" : "rankings",
      results: search ? countBucket(players + profiles) : "error",
    };
    if (players + profiles > 0) {
      properties.kind = players && profiles ? "both" : profiles ? "profiles" : "players";
    }
    return [{ event: "search_results_shown", properties }];
  }
  if (routeId === "routes/player") {
    const lookup = page.lookup as { state: string } | null;
    const state = page.player
      ? "available"
      : lookup
        ? lookup.state
        : page.requestedTag === null
          ? "invalid"
          : "failed";
    return [{ event: "player_lookup_result", properties: { state } }];
  }
  return [];
}

// Browser state. `queue` holds up to 50 events until PostHog has loaded; it
// is dropped once PostHog has loaded or failed to.
let client: PostHog | null = null;
let loading: Promise<void> | null = null;
let queue: AnalyticsEvent[] | null = [];
const switchListeners = new Set<() => void>();

export function returnVisitsOn(): boolean {
  try {
    return window.localStorage.getItem(RETURN_VISITS_KEY) !== "off";
  } catch {
    return true;
  }
}

function applyReturnVisits(posthog: PostHog): void {
  const consent = posthog.get_explicit_consent_status();
  if (!returnVisitsOn()) {
    // Forgets the stored ID and keeps counting without one.
    if (consent !== "denied") posthog.opt_out_capturing();
    return;
  }
  if (consent !== "granted") posthog.opt_in_capturing({ captureEventName: false });
  try {
    const made = Number(window.localStorage.getItem(RETURN_VISITS_KEY));
    if (made > 0 && Date.now() - made < BROWSER_ID_LIFETIME_MS) return;
    if (made > 0) posthog.reset(true);
    window.localStorage.setItem(RETURN_VISITS_KEY, String(Date.now()));
  } catch {
    // Without storage PostHog keeps the ID for this page only.
  }
}

/** Saves the visitor's choice in this browser and applies it straight away. */
export function setReturnVisits(on: boolean): void {
  try {
    if (on) window.localStorage.removeItem(RETURN_VISITS_KEY);
    else window.localStorage.setItem(RETURN_VISITS_KEY, "off");
  } catch {
    // Without storage the choice lasts for this page only.
  }
  if (client) applyReturnVisits(client);
  for (const listener of switchListeners) listener();
}

export function onReturnVisitsChange(listener: () => void): () => void {
  switchListeners.add(listener);
  return () => switchListeners.delete(listener);
}

export function startAnalytics(settings: AnalyticsSettings): void {
  if (loading) return;
  loading = import("posthog-js/dist/module.slim.no-external")
    .then(({ default: posthog }) => {
      posthog.init(settings.token, {
        api_host: settings.host,
        // A stored ID only while return visits are on; cookieless otherwise.
        cookieless_mode: "on_reject",
        persistence: "localStorage",
        person_profiles: "never",
        autocapture: false,
        capture_pageview: false,
        capture_pageleave: false,
        capture_dead_clicks: false,
        capture_exceptions: false,
        capture_performance: false,
        capture_heatmaps: false,
        rageclick: false,
        disable_session_recording: true,
        disable_surveys: true,
        disable_conversations: true,
        disable_product_tours: true,
        disable_web_experiments: true,
        disable_external_dependency_loading: true,
        advanced_disable_flags: true,
        save_referrer: false,
        save_campaign_params: false,
        before_send: sanitizeEvent,
      });
      applyReturnVisits(posthog);
      client = posthog;
      for (const { event, properties } of queue ?? []) posthog.capture(event, properties);
      queue = null;
    })
    .catch(() => {
      queue = null;
    });
}

/** Sends one listed event with label-only properties; a no-op when counting is off. */
export function track(event: string, properties: EventProperties = {}): void {
  if (!EVENTS.has(event)) return;
  const safe: EventProperties = {};
  for (const [key, value] of Object.entries(properties)) {
    if (typeof value === "boolean" || /^[a-z0-9_+-]{1,80}$/.test(value))
      safe[key] = value;
  }
  if (client) client.capture(event, safe);
  else if (queue && queue.length < 50) queue.push({ event, properties: safe });
}
