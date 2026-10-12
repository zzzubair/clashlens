import { useEffect, useRef, useSyncExternalStore } from "react";
import { useLocation, useMatches, useRouteLoaderData } from "react-router";

import type { PlayerLookup, PlayerPage } from "../lib/contracts";
import {
  armySort,
  loadedPageEvents,
  lookupOutcome,
  onReturnVisitsChange,
  pagePerformance,
  returnVisitsOn,
  routeOf,
  searchInput,
  selectionEvents,
  selectionOf,
  setReturnVisits,
  startAnalytics,
  track,
  type Selection,
} from "../lib/analytics";
import type { RootLoaderData } from "../root";

function currentRoute(): string {
  return routeOf(window.location.pathname).route;
}

// Clicks and searches are read from the page as a whole, so the pages
// themselves carry no tracking code. `shown` is what the page shows now.
function onClick(event: MouseEvent, shown: Selection | null) {
  const target = event.target instanceof Element ? event.target : null;
  const column = target?.closest(".analytics-sort-button")?.closest("th")?.dataset.column;
  if (column) {
    track("armies_changed", {
      ...shown?.labels,
      changed: "sort",
      sort: armySort(column),
    });
    return;
  }
  if (target?.closest(".player-refresh-form button")) {
    track("player_detail_changed", { action: "refresh", ...shown?.labels });
    return;
  }
  const link = target?.closest("a");
  const href = link?.getAttribute("href");
  if (!link || !href) return;
  if (href.startsWith("https://link.clashofclans.com/")) {
    track("copy_army_clicked");
    return;
  }
  const list = link.closest(".search-dropdown, .search-results, .rank-search-results");
  if (!list) return;
  track("search_result_opened", {
    source: list.classList.contains("rank-search-results")
      ? "rankings"
      : list.classList.contains("search-dropdown")
        ? "suggestion"
        : "results",
    result: href.startsWith("/users/")
      ? "profile"
      : href.startsWith("/players/")
        ? "player"
        : "rank",
  });
}

// Only the Home and Rankings search forms; the header search is not counted.
function onSubmit(event: SubmitEvent) {
  const form = event.target instanceof HTMLFormElement ? event.target : null;
  if (form?.getAttribute("role") !== "search" || form.closest(".site-header")) return;
  const value = form.querySelector<HTMLInputElement>('input[type="search"]')?.value ?? "";
  if (value.trim() === "") return;
  track("search_submitted", { source: currentRoute(), input: searchInput(value) });
}

// The loaded results and lookups already counted on the open page.
let counted = { path: "", keys: new Set<string>() };

function countedOn(path: string): Set<string> {
  if (counted.path !== path) counted = { path, keys: new Set() };
  return counted.keys;
}

/** Starts visitor counting when the server configured it, and sends page events. */
export function Analytics() {
  const root = useRouteLoaderData<RootLoaderData>("root");
  const token = root?.analytics?.token;
  const location = useLocation();
  const matches = useMatches();
  const page = matches[matches.length - 1];
  const routeId = page?.id;
  const data = page?.loaderData;
  // What the open page shows.
  const shown = useRef<{ path: string; selection: Selection } | null>(null);

  useEffect(() => {
    if (!token) return;
    startAnalytics({ token });
    const click = (event: MouseEvent) => onClick(event, shown.current?.selection ?? null);
    document.addEventListener("click", click, true);
    document.addEventListener("submit", onSubmit, true);
    const timing = () => {
      const result = pagePerformance(
        performance.getEntriesByType?.("navigation")[0] as
          PerformanceNavigationTiming | undefined,
      );
      if (result) track(result.event, result.properties);
    };
    // The load time is final only once the page's load event has finished.
    const afterLoad = () => setTimeout(timing);
    if (document.readyState === "complete") timing();
    else window.addEventListener("load", afterLoad, { once: true });
    return () => {
      document.removeEventListener("click", click, true);
      document.removeEventListener("submit", onSubmit, true);
      window.removeEventListener("load", afterLoad);
    };
  }, [token]);

  useEffect(() => {
    if (!token) return;
    track("$pageview", { signed_in: Boolean(root?.loggedIn) });
  }, [token, location.pathname]);

  useEffect(() => {
    if (!token || !routeId) return;
    const path = location.pathname;
    const before = shown.current?.path === path ? shown.current.selection : null;
    const selection = selectionOf(routeId, data);
    // The Armies table can be re-sorted on the page without a new load.
    const sorted = document.querySelector<HTMLElement>(
      ".analytics-results-table th[aria-sort]",
    );
    if (selection && sorted?.dataset.column)
      selection.labels.sort = armySort(sorted.dataset.column);
    if (selection) {
      shown.current = { path, selection };
      for (const { event, properties } of selectionEvents(
        routeOf(path).route,
        before,
        selection,
      )) {
        track(event, properties);
      }
    } else if (before === null) {
      shown.current = null;
    }
    const keys = countedOn(path);
    for (const { event, properties } of loadedPageEvents(routeId, data)) {
      // A search counts once, however often its results reload.
      const key = `${location.key} ${event}`;
      if (keys.has(key)) continue;
      keys.add(key);
      track(event, properties);
    }
  }, [token, routeId, data, location.key, location.pathname]);

  return null;
}

/** Counts each lookup outcome the player page shows, once per page. */
export function usePlayerLookupEvent(
  requestedTag: string | null,
  trackedPlayer: PlayerPage | null,
  lookup: PlayerLookup | null,
) {
  const { pathname } = useLocation();
  const state = lookupOutcome(requestedTag, trackedPlayer, lookup);
  useEffect(() => {
    const keys = countedOn(pathname);
    if (keys.has(`lookup ${state}`)) return;
    keys.add(`lookup ${state}`);
    track("player_lookup_result", { state });
  }, [pathname, state]);
}

/** Counts an error page once it shows. */
export function useErrorEvent(notFound: boolean) {
  useEffect(() => {
    track("ui_error", { kind: notFound ? "not_found" : "error" });
  }, [notFound]);
}

/** The return-visits switch; hidden while visitor counting is not configured. */
export function ReturnVisitsSwitch() {
  const root = useRouteLoaderData<RootLoaderData>("root");
  const on = useSyncExternalStore(onReturnVisitsChange, returnVisitsOn, () => true);
  if (!root?.analytics) return null;
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      className="return-visits-switch"
      onClick={() => setReturnVisits(!on)}
    >
      Count return visits<span aria-hidden="true">: {on ? "On" : "Off"}</span>
    </button>
  );
}
