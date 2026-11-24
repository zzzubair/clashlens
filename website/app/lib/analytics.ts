/**
 * Visitor counting with PostHog's EU cloud, only for visitors who accept.
 * Nothing loads, and nothing is sent, unless the server hands over a project
 * token and the visitor has accepted in this browser. Accepting loads PostHog
 * with a random ID kept in this browser, so a returning browser can be
 * recognised. Declining loads nothing; declining later stops all sending and
 * deletes that ID. The choice is remembered here.
 *
 * Only the events and labels below ever leave the browser: page addresses
 * lose player tags, usernames, group and crew IDs, query strings and titles,
 * no browser or device details are kept, and no IP-based location is looked
 * up.
 */
import type {
  CaptureResult,
  PostHog,
  Properties,
} from "posthog-js/dist/module.slim.no-external";

import type {
  ArmyAnalytics,
  PlayerLookup,
  PlayerPage,
  TrackedLeaderboard,
} from "./contracts";
import { normalizePlayerTag } from "./player-tag";

/** PostHog Cloud EU, in Frankfurt; the only place counts are sent. */
const POSTHOG_HOST = "https://eu.i.posthog.com";

export interface AnalyticsSettings {
  token: string;
}

export type EventProperties = Record<string, string | boolean>;

export interface AnalyticsEvent {
  event: string;
  properties: EventProperties;
}

export const ANALYTICS_CHOICE_KEY = "clashlens-analytics";

export type AnalyticsChoice = "accepted" | "declined" | null;

const EVENTS: Record<string, readonly string[]> = {
  $pageview: ["route", "signed_in"],
  search_submitted: ["route", "source", "input"],
  search_results_shown: ["route", "source", "results", "kind"],
  search_result_opened: ["route", "source", "result"],
  player_lookup_result: ["route", "state"],
  player_detail_changed: ["route", "action", "season"],
  rankings_changed: ["route", "changed", "view", "page", "day", "season"],
  armies_changed: ["route", "changed", "category", "population", "season", "sort"],
  copy_army_clicked: ["route"],
  group_used: ["route", "action"],
  page_performance: ["route", "response", "load"],
  ui_error: ["route", "kind"],
};

// What PostHog itself needs to count an event: the project, the browser ID,
// the visit, and the page with its IDs replaced.
const POSTHOG_PROPERTIES = [
  "token",
  "distinct_id",
  "$device_id",
  "$session_id",
  "$window_id",
  "$lib",
  "$lib_version",
  "$insert_id",
  "$time",
  "$host",
  "$current_url",
  "$pathname",
  "$referrer",
  "$referring_domain",
  "$process_person_profile",
];

const ROUTES: ReadonlyArray<readonly [string, string]> = [
  ["home", "/"],
  ["search", "/search"],
  ["player", "/players/:tag"],
  ["rankings", "/leaderboards/tracked"],
  ["armies", "/analytics/armies"],
  ["blog", "/blog"],
  ["blog_post", "/blog/:post"],
  ["login", "/login"],
  ["signup", "/signup"],
  ["sign_in_callback", "/auth/google/callback"],
  ["sign_in_callback", "/auth/discord/callback"],
  ["account", "/account"],
  ["account_setup", "/account/setup"],
  ["account_settings", "/account/settings"],
  ["unlink_player", "/account/players/:tag/unlink"],
  ["account_discord_alerts", "/account/discord-alerts"],
  ["verify_player", "/account/verify-player"],
  ["groups", "/account/watchlists"],
  ["group_add", "/account/watchlists/add/:tag"],
  ["group_comparison", "/account/watchlists/:group"],
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

export function routeOf(
  pathname: string,
  dashboard = false,
): { route: string; path: string } {
  const parts = pathname.split("/").filter(Boolean);
  for (const [route, template] of ROUTES) {
    const expected = template.split("/").filter(Boolean);
    if (
      expected.length === parts.length &&
      expected.every((part, index) => part.startsWith(":") || part === parts[index])
    ) {
      return {
        route: route === "home" && dashboard ? "dashboard" : route,
        path: template,
      };
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

function safeReferrer(value: string, host: unknown): string | undefined {
  if (value === "$direct") return value;
  try {
    const url = new URL(value);
    return url.host === host ? safeUrl(value) : url.origin;
  } catch {
    return undefined;
  }
}

export function sanitizeEvent(event: CaptureResult | null): CaptureResult | null {
  const labels = event ? EVENTS[event.event] : undefined;
  if (!event || !labels) return null;
  const source = event.properties;
  const properties: Properties = {};
  for (const key of [...POSTHOG_PROPERTIES, ...labels]) {
    if (source[key] !== undefined) properties[key] = source[key];
  }
  const rewrite = (key: string, clean: (value: string) => string | undefined) => {
    const value = properties[key];
    if (value === undefined) return;
    const safe = typeof value === "string" ? clean(value) : undefined;
    if (safe === undefined) delete properties[key];
    else properties[key] = safe;
  };
  rewrite("$current_url", safeUrl);
  rewrite("$pathname", (value) => routeOf(value).path);
  rewrite("$referrer", (value) => safeReferrer(value, source.$host));
  properties.$geoip_disable = true;
  return {
    uuid: event.uuid,
    event: event.event,
    properties,
    timestamp: event.timestamp,
  };
}

export function searchInput(value: string): string {
  return normalizePlayerTag(value) ? "tag" : "name";
}

export function countBucket(count: number): string {
  return count === 0 ? "zero" : count === 1 ? "one" : "many";
}

const ARMY_CATEGORIES = new Set([
  "army-types",
  "troops",
  "spells",
  "siege",
  "heroes",
  "pets",
  "equipment",
  "equipment-for-hero",
  "cc-troops",
  "hero-pet",
  "hero-equipment",
  "cc-composition",
]);
const ARMY_POPULATIONS = ["all", "top"];
const ARMY_SORTS = new Set([
  "name",
  "quantity",
  "usage-count",
  "usage-rate",
  "one-star-rate",
  "two-star-rate",
  "three-star-rate",
  "average-trophies",
  "average-stars",
  "average-destruction",
]);

export function armySort(column: string): string {
  return ARMY_SORTS.has(column) ? column : "other";
}

function armyPopulation(population: string): string {
  const kind = population.replace(/(-\d+)+$/, "");
  return ARMY_POPULATIONS.includes(kind) ? kind : "other";
}

export interface Selection {
  values: Record<string, string>;
  labels: EventProperties;
}

export function selectionOf(routeId: string, data: unknown): Selection | null {
  if (!data || typeof data !== "object") return null;
  const page = data as Record<string, unknown>;
  if (routeId === "routes/tracked-leaderboard" && page.leaderboard) {
    const board = page.leaderboard as TrackedLeaderboard;
    const daily = board.daily;
    const labels: EventProperties = {
      view: daily ? "reset" : "live",
      page: board.page > 1 ? "later" : "first",
      season:
        daily && Date.now() >= Date.parse(daily.seasonEndAt) ? "previous" : "current",
    };
    if (daily) labels.day = String(daily.dayNumber);
    return {
      values: {
        view: board.view,
        page: String(board.page),
        day: daily ? String(daily.dayNumber) : "",
        season: daily?.officialSeasonId ?? "",
      },
      labels,
    };
  }
  if (routeId === "routes/army-analytics" && page.analytics) {
    const { selection } = page.analytics as ArmyAnalytics;
    const historical = page.historicalSummary === true;
    const population = historical ? "all" : selection.population;
    return {
      values: {
        category: selection.category,
        population,
        season: String(page.requestedSeason),
        sort: selection.sort,
      },
      labels: {
        category: ARMY_CATEGORIES.has(selection.category) ? selection.category : "other",
        population: armyPopulation(population),
        season: historical ? "historical" : "current",
        sort: armySort(selection.sort),
      },
    };
  }
  if (routeId === "routes/player" && page.requestedTag) {
    const season = (page.selectedSeason as string | null) ?? "";
    return { values: { season }, labels: { season: season ? "historical" : "current" } };
  }
  if (routeId === "routes/account.groups.$groupId" && page.comparison) {
    return { values: { days: String(page.days), sort: String(page.sort) }, labels: {} };
  }
  return null;
}

export function selectionEvents(
  route: string,
  before: Selection | null,
  after: Selection,
): AnalyticsEvent[] {
  if (route === "group_comparison" && before === null) {
    return [{ event: "group_used", properties: { action: "compare" } }];
  }
  if (before === null) return [];
  const changed = Object.keys(after.values).filter(
    (key) => before.values[key] !== after.values[key],
  );
  if (changed.length === 0) return [];
  if (route === "rankings" || route === "armies") {
    return [
      {
        event: route === "rankings" ? "rankings_changed" : "armies_changed",
        properties: { ...after.labels, changed: changed.join("+") },
      },
    ];
  }
  if (route === "player") {
    return [
      {
        event: "player_detail_changed",
        properties: { action: "season", ...after.labels },
      },
    ];
  }
  if (route === "group_comparison") {
    return changed.map((key) => ({
      event: "group_used",
      properties: { action: key === "days" ? "change-days" : "change-sort" },
    }));
  }
  return [];
}

const LOOKUP_OUTCOMES: Partial<Record<PlayerLookup["state"], string>> = {
  checking: "checking",
  tracking: "checking",
  uncertain: "checking",
  not_found: "not-found",
  not_in_legend: "not-found",
  failed: "failed",
};

export function loadedPageEvents(routeId: string, data: unknown): AnalyticsEvent[] {
  if (!data || typeof data !== "object") return [];
  const page = data as Record<string, unknown>;
  if (
    (routeId === "routes/search" || routeId === "routes/tracked-leaderboard") &&
    page.query
  ) {
    const search = page.search as { results: unknown[]; users?: unknown[] } | null;
    const players = search?.results.length ?? 0;
    const profiles = search?.users?.length ?? 0;
    const properties: EventProperties = {
      source: routeId === "routes/search" ? "home" : "rankings",
      results: search ? countBucket(players + profiles) : "error",
    };
    if (players + profiles > 0) {
      properties.kind = players && profiles ? "both" : profiles ? "profiles" : "players";
    }
    return [{ event: "search_results_shown", properties }];
  }
  return [];
}

export function lookupOutcome(
  requestedTag: string | null,
  trackedPlayer: PlayerPage | null,
  lookup: PlayerLookup | null,
): string {
  if (requestedTag === null) return "invalid";
  if (trackedPlayer) return "available";
  return (lookup && LOOKUP_OUTCOMES[lookup.state]) ?? "failed";
}

function timeBucket(milliseconds: number): string {
  if (milliseconds < 1000) return "under-1s";
  if (milliseconds < 3000) return "1-3s";
  if (milliseconds < 10_000) return "3-10s";
  return "over-10s";
}

export function pagePerformance(
  timing: PerformanceNavigationTiming | undefined,
  sample = Math.random(),
): AnalyticsEvent | null {
  if (!timing || timing.loadEventEnd <= 0 || sample >= 0.1) return null;
  return {
    event: "page_performance",
    properties: {
      response: timeBucket(timing.responseStart),
      load: timeBucket(timing.loadEventEnd),
    },
  };
}

/**
 * PostHog in this page, which loads only after Accept (`startAnalytics`):
 *
 * - `notLoaded`: not asked for yet.
 * - `loading`: being fetched and set up.
 *   Until it is `ready`, up to 50 events tracked after Accept wait in
 *   `queue`; later ones are dropped.
 * - `ready`: loaded. The waiting events went first, each under the choice of
 *   that moment, and every later event goes at once.
 * - `failed`: it could not load; events are dropped until the page loads
 *   again, which is the only retry.
 *
 * Nothing survives a page load. A Decline while `loading` or `ready` is
 * applied before any event leaves (`send`), so nothing is counted after it.
 */
type ClientLoad =
  | {
      kind: "notLoaded" | "loading";
      queue: Array<{ event: string; properties: Properties }>;
    }
  | { kind: "ready"; posthog: PostHog }
  | { kind: "failed" };

let load: ClientLoad = { kind: "notLoaded", queue: [] };
const readyClient = (): PostHog | null => (load.kind === "ready" ? load.posthog : null);
let clientCounting = false;
let unsavedChoice: AnalyticsChoice | undefined;
let reopened = false;
let watchingTabs = false;
let withdrawals = 0;
let dashboardShown = false;
const choiceListeners = new Set<() => void>();

export function analyticsChoice(): AnalyticsChoice {
  if (unsavedChoice !== undefined) return unsavedChoice;
  try {
    const saved = window.localStorage.getItem(ANALYTICS_CHOICE_KEY);
    return saved === "accepted" || saved === "declined" ? saved : null;
  } catch {
    return null;
  }
}

export function analyticsWithdrawals(): number {
  return withdrawals;
}

/**
 * Where the visitor's choice stands in this tab: `undecided` (no choice
 * saved), `accepted`, `declined`, or `reopened` (a choice was made and the
 * question is showing again, the old choice still in force until a new one).
 * Accept or Decline saves it in this browser, or for this page only when the
 * browser refuses to save it; a choice saved in another tab reaches this one
 * at once. A Decline anywhere withdraws counting here too.
 */
export type ConsentStage = "undecided" | "accepted" | "declined" | "reopened";

export function consentStage(): ConsentStage {
  if (reopened) return "reopened";
  return analyticsChoice() ?? "undecided";
}

export function askingForChoice(): boolean {
  const stage = consentStage();
  return stage === "reopened" || stage === "undecided";
}

function forgetPostHog(): void {
  for (const name of ["localStorage", "sessionStorage"] as const) {
    try {
      const storage = window[name];
      for (const key of Object.keys(storage)) {
        if (key.startsWith("ph_") || key.startsWith("__ph_")) storage.removeItem(key);
      }
    } catch {}
  }
}

function applyChoice(
  posthog: PostHog,
  accepted = analyticsChoice() === "accepted",
): boolean {
  if (accepted && !clientCounting) {
    posthog.opt_in_capturing({ captureEventName: false });
  } else if (!accepted && clientCounting) {
    posthog.opt_out_capturing();
    // A new ID in memory, so accepting again never brings the old one back.
    posthog.reset(true);
  }
  clientCounting = accepted;
  if (!accepted) forgetPostHog();
  return accepted;
}

// `withdrawn` is a Decline from another tab, applied even if it was accepted
// again before this tab heard of it.
function notifyChoice(withdrawn = false): void {
  if (withdrawn || analyticsChoice() !== "accepted") withdrawals++;
  const client = readyClient();
  if (client) {
    if (withdrawn) applyChoice(client, false);
    applyChoice(client);
  } else if (analyticsChoice() !== "accepted") forgetPostHog();
  for (const listener of choiceListeners) listener();
}

export function setAnalyticsChoice(choice: "accepted" | "declined"): void {
  try {
    window.localStorage.setItem(ANALYTICS_CHOICE_KEY, choice);
    unsavedChoice = undefined;
  } catch {
    unsavedChoice = choice;
  }
  reopened = false;
  notifyChoice();
}

export function reopenAnalyticsChoice(): void {
  reopened = true;
  for (const listener of choiceListeners) listener();
}

export function onAnalyticsChoiceChange(listener: () => void): () => void {
  if (!watchingTabs) {
    watchingTabs = true;
    window.addEventListener("storage", (event) => {
      if (event.key === ANALYTICS_CHOICE_KEY || event.key === null) {
        unsavedChoice = undefined;
        notifyChoice(event.newValue !== "accepted");
      }
    });
  }
  choiceListeners.add(listener);
  return () => choiceListeners.delete(listener);
}

function send(posthog: PostHog, event: string, properties: Properties): void {
  if (applyChoice(posthog)) posthog.capture(event, properties, { send_instantly: true });
}

export function setDashboardShown(shown: boolean): void {
  dashboardShown = shown;
}

export function startAnalytics(settings: AnalyticsSettings): void {
  if (load.kind !== "notLoaded") return;
  const { queue } = load;
  load = { kind: "loading", queue };
  void import("posthog-js/dist/module.slim.no-external")
    .then(({ default: posthog }) => {
      posthog.init(settings.token, {
        api_host: POSTHOG_HOST,
        opt_out_capturing_by_default: true,
        opt_out_persistence_by_default: true,
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
      posthog._retryQueue = undefined;
      load = { kind: "ready", posthog };
      for (const { event, properties } of queue) send(posthog, event, properties);
      applyChoice(posthog);
    })
    .catch(() => {
      load = { kind: "failed" };
    });
}

export function track(
  event: string,
  labels: EventProperties = {},
  pathname = window.location.pathname,
): void {
  const allowed = EVENTS[event];
  if (!allowed || analyticsChoice() !== "accepted") return;
  const { route, path } = routeOf(pathname, dashboardShown);
  const properties: Properties = {
    route,
    $current_url: `${window.location.origin}${path}`,
    $pathname: path,
  };
  for (const key of allowed) {
    const value = labels[key];
    if (typeof value === "boolean" || /^[a-z0-9_+-]{1,80}$/.test(value ?? "")) {
      properties[key] = value;
    }
  }
  if (load.kind === "ready") send(load.posthog, event, properties);
  else if (load.kind !== "failed" && load.queue.length < 50)
    load.queue.push({ event, properties });
}
