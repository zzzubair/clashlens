import { useEffect, useRef, useSyncExternalStore } from "react";
import { useLocation, useMatches, useRouteLoaderData } from "react-router";

import {
  loadedPageEvents,
  onReturnVisitsChange,
  pageChangeEvents,
  returnVisitsOn,
  routeOf,
  searchInput,
  setReturnVisits,
  startAnalytics,
  track,
} from "../lib/analytics";
import type { RootLoaderData } from "../root";

function currentRoute(): string {
  return routeOf(window.location.pathname).route;
}

// Clicks and searches are read from the page as a whole, so the pages
// themselves carry no tracking code.
function onClick(event: MouseEvent) {
  const link = event.target instanceof Element ? event.target.closest("a") : null;
  const href = link?.getAttribute("href");
  if (!link || !href) return;
  if (href.startsWith("https://link.clashofclans.com/")) {
    track("copy_army_clicked", { source: currentRoute() });
    return;
  }
  const provider = /^\/auth\/(google|discord)(\?|$)/.exec(href)?.[1];
  if (provider && currentRoute() === "login") {
    track("sign_in_step", { stage: "started", provider });
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

function onSubmit(event: SubmitEvent) {
  const form = event.target instanceof HTMLFormElement ? event.target : null;
  if (form?.getAttribute("role") !== "search") return;
  const value = form.querySelector<HTMLInputElement>('input[type="search"]')?.value ?? "";
  if (value.trim() === "") return;
  track("search_submitted", {
    source: form.closest(".site-header") ? "header" : currentRoute(),
    input: searchInput(value),
  });
}

/** Starts visitor counting when the server configured it, and sends page events. */
export function Analytics() {
  const root = useRouteLoaderData<RootLoaderData>("root");
  const token = root?.analytics?.token;
  const host = root?.analytics?.host;
  const location = useLocation();
  const matches = useMatches();
  const page = matches[matches.length - 1];
  const shown = useRef<{ pathname: string; search: string } | null>(null);
  const loaded = useRef(new Set<string>());

  useEffect(() => {
    if (!token || !host) return;
    startAnalytics({ token, host });
    document.addEventListener("click", onClick, true);
    document.addEventListener("submit", onSubmit, true);
    return () => {
      document.removeEventListener("click", onClick, true);
      document.removeEventListener("submit", onSubmit, true);
    };
  }, [token, host]);

  useEffect(() => {
    if (!token) return;
    const before = shown.current;
    shown.current = { pathname: location.pathname, search: location.search };
    const { route } = routeOf(location.pathname);
    if (before?.pathname !== location.pathname) {
      track("$pageview", { route, signed_in: Boolean(root?.loggedIn) });
      // A completed sign-in redirects away; only a failure shows this page.
      const provider = /^\/auth\/(google|discord)\//.exec(location.pathname)?.[1];
      if (provider) track("sign_in_step", { stage: "failed", provider });
    } else if (before.search !== location.search) {
      for (const { event, properties } of pageChangeEvents(
        route,
        new URLSearchParams(before.search),
        new URLSearchParams(location.search),
      )) {
        track(event, properties);
      }
    }
  }, [token, location.pathname, location.search]);

  useEffect(() => {
    if (!token || !page) return;
    for (const { event, properties } of loadedPageEvents(page.id, page.loaderData)) {
      // Once per visit to a page and result, however often its data reloads.
      const key = `${location.key} ${event} ${JSON.stringify(properties)}`;
      if (loaded.current.has(key)) continue;
      loaded.current.add(key);
      track(event, properties);
    }
  }, [token, page, location.key]);

  return null;
}

/** Counts an error page once it shows. */
export function useErrorEvent(notFound: boolean) {
  useEffect(() => {
    track("ui_error", { route: currentRoute(), kind: notFound ? "not_found" : "error" });
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
