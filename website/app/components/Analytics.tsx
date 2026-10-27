import { useEffect, useLayoutEffect, useRef, useSyncExternalStore } from "react";
import { Link, useLocation, useMatches, useRouteLoaderData } from "react-router";

import type { PlayerLookup, PlayerPage } from "../lib/contracts";
import {
  analyticsChoice,
  analyticsWithdrawals,
  armySort,
  askingForChoice,
  loadedPageEvents,
  lookupOutcome,
  onAnalyticsChoiceChange,
  pagePerformance,
  reopenAnalyticsChoice,
  routeOf,
  searchInput,
  selectionEvents,
  selectionOf,
  setAnalyticsChoice,
  setDashboardShown,
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
  // The Armies page's sort headings and sort chips.
  const column = target?.closest<HTMLElement>(".analytics-sort-button")?.dataset.column;
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
  if (href.startsWith("https://link.clashofclans.com/en?action=CopyArmy&")) {
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

/** The visitor's choice, or null before one and while the page is drawn on the server. */
export function useAnalyticsChoice() {
  return useSyncExternalStore(onAnalyticsChoiceChange, analyticsChoice, () => null);
}

/**
 * Starts visitor counting once the server configured it and the visitor
 * accepted, and sends page events.
 */
export function Analytics() {
  const root = useRouteLoaderData<RootLoaderData>("root");
  const accepted = useAnalyticsChoice() === "accepted";
  const token = accepted ? root?.analytics?.token : undefined;
  const location = useLocation();
  const matches = useMatches();
  const page = matches[matches.length - 1];
  const routeId = page?.id;
  const data = page?.loaderData;
  // What the open page shows.
  const shown = useRef<{ path: string; selection: Selection } | null>(null);
  const withdrawals = useSyncExternalStore(
    onAnalyticsChoiceChange,
    analyticsWithdrawals,
    () => 0,
  );
  const dashboard = Boolean(root?.loggedIn && root?.dashboardEnabled);

  // Before any page's events, which all go out from later effects.
  useLayoutEffect(() => setDashboardShown(dashboard), [dashboard]);

  useEffect(() => {
    counted = { path: "", keys: new Set() };
    shown.current = null;
  }, [withdrawals]);

  useEffect(() => {
    if (!token) return;
    startAnalytics({ token });
    const click = (event: MouseEvent) => onClick(event, shown.current?.selection ?? null);
    document.addEventListener("click", click, true);
    document.addEventListener("submit", onSubmit, true);
    const timing = () => {
      const measured = performance.getEntriesByType?.("navigation")[0] as
        PerformanceNavigationTiming | undefined;
      const result = pagePerformance(measured);
      if (result && measured) {
        track(result.event, result.properties, new URL(measured.name).pathname);
      }
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
  const accepted = useAnalyticsChoice() === "accepted";
  const state = lookupOutcome(requestedTag, trackedPlayer, lookup);
  useEffect(() => {
    if (!accepted) return;
    const keys = countedOn(pathname);
    if (keys.has(`lookup ${state}`)) return;
    keys.add(`lookup ${state}`);
    track("player_lookup_result", { state });
  }, [accepted, pathname, state]);
}

/** Counts an error page once it shows; a page that isn't one, null, counts nothing. */
export function useErrorEvent(kind: "not_found" | "error" | null) {
  const accepted = useAnalyticsChoice() === "accepted";
  useEffect(() => {
    if (accepted && kind !== null) track("ui_error", { kind });
  }, [accepted, kind]);
}

/** The Accept or Decline question; hidden while visitor counting is not configured. */
export function AnalyticsChoiceBanner() {
  const configured = Boolean(useRouteLoaderData<RootLoaderData>("root")?.analytics);
  const asking = useSyncExternalStore(
    onAnalyticsChoiceChange,
    askingForChoice,
    () => false,
  );
  const banner = useRef<HTMLElement>(null);
  // Where the visitor was when they reopened the question.
  const opener = useRef<HTMLElement | null>(null);
  const shown = configured && asking;

  useEffect(() => {
    const root = document.documentElement;
    if (!shown || !banner.current) return;
    // Room below the page, so the banner never covers its last controls.
    const observer = new ResizeObserver(([entry]) =>
      root.style.setProperty(
        "--analytics-choice-space",
        `${entry.borderBoxSize[0].blockSize}px`,
      ),
    );
    observer.observe(banner.current);
    if (analyticsChoice() !== null) {
      opener.current = document.activeElement as HTMLElement | null;
      banner.current.focus();
    }
    return () => {
      observer.disconnect();
      root.style.removeProperty("--analytics-choice-space");
      opener.current?.focus();
      opener.current = null;
    };
  }, [shown]);

  if (!shown) return null;
  return (
    <section
      ref={banner}
      className="analytics-choice"
      aria-labelledby="analytics-choice-text"
      tabIndex={-1}
    >
      <p id="analytics-choice-text">
        May we use PostHog to see how this site is used? It keeps a random ID in your
        browser, and loads only if you accept. <Link to="/privacy">Privacy Policy</Link>
      </p>
      <div className="analytics-choice-buttons">
        <button
          type="button"
          className="button"
          onClick={() => setAnalyticsChoice("accepted")}
        >
          Accept
        </button>
        <button
          type="button"
          className="button"
          onClick={() => setAnalyticsChoice("declined")}
        >
          Decline
        </button>
      </div>
    </section>
  );
}

/** Reopens the Accept or Decline question; hidden while visitor counting is not configured. */
export function AnalyticsChoicesButton({ children }: { children: string }) {
  const root = useRouteLoaderData<RootLoaderData>("root");
  if (!root?.analytics) return null;
  return (
    <button
      type="button"
      className="analytics-choices-button"
      onClick={reopenAnalyticsChoice}
    >
      {children}
    </button>
  );
}
