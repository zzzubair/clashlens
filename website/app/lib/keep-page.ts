import { useContext, useEffect } from "react";
import { preloadModule } from "react-dom";
import {
  isRouteErrorResponse,
  UNSAFE_FrameworkContext,
  useLocation,
  type DataStrategyResult,
  type MiddlewareFunction,
} from "react-router";

import type { WebsiteErrorResponse } from "./contracts";
import type { RootLoaderData } from "../root";
import type { PlayerSearchLoaderData } from "../routes/player-search";

// What the header shows when the account cannot be read.
export const LOGGED_OUT: RootLoaderData = {
  loggedIn: false,
  accountLabel: null,
  accountUsername: null,
  logoutIdempotencyKey: null,
  updateStatus: null,
};

// The address the browser shows now, so only its own re-read can keep it.
let shownPage: string | null = null;

export function rememberShownPage(url: string) {
  shownPage = url;
}

export function useRememberShownPage() {
  const { pathname, search } = useLocation();
  useEffect(() => rememberShownPage(pathname + search));
}

// The browser fetches a route's code the first time it is used, and React
// Router reloads the whole page when that fetch fails. Download the code for
// the requests that answer inline with the page, so they still work offline.
export function usePreloadInlineAnswerCode() {
  const manifest = useContext(UNSAFE_FrameworkContext)?.manifest;
  for (const id of ["routes/player-search", "routes/refresh"]) {
    const route = manifest?.routes[id];
    if (route === undefined) continue;
    for (const href of [route.module, ...(route.imports ?? [])]) preloadModule(href);
  }
}

// No answer from the website: the phone was offline or just woke, the
// connection dropped mid-answer, or a proxy sent its own error page.
// Form submissions arrive wrapped as data(error), so look inside first.
function lostConnection(result: unknown) {
  const error =
    result instanceof Object &&
    "type" in result &&
    result.type === "DataWithResponseInit" &&
    "data" in result
      ? result.data
      : result;
  return (
    error instanceof TypeError ||
    (isRouteErrorResponse(error) && error.status >= 500) ||
    (error instanceof Error && error.message === "Unable to decode turbo-stream response")
  );
}

const UNAVAILABLE: WebsiteErrorResponse = {
  error: {
    code: "unavailable",
    message: "Saved data is still available, but the live service is unavailable.",
  },
};

// Refresh and search show their own unavailable notice, as when the service is down.
function inlineAnswer({ pathname, searchParams }: URL) {
  if (/^\/resources\/players\/[^/]+\/refresh$/.test(pathname)) return UNAVAILABLE;
  if (pathname === "/resources/players/search") {
    const answer: PlayerSearchLoaderData = {
      query: (searchParams.get("q") ?? "").trim(),
      search: null,
      error: UNAVAILABLE,
    };
    return answer;
  }
  return undefined;
}

// Player profiles reread their own data in the background, such as when a phone
// wakes. A re-read that cannot reach the website leaves the profile as the
// browser already has it, and the next re-read tries again; only a page the
// browser has not shown yet can fail. The header never keeps an account it
// could not read, so it shows signed out. Other pages show the error as before.
export const keepPageOnLostConnection: MiddlewareFunction<
  Record<string, DataStrategyResult>
> = async ({ request }, next) => {
  const results = await next();
  const url = new URL(request.url);
  const lost = Object.keys(results).filter(
    (id) => results[id].type === "error" && lostConnection(results[id].result),
  );
  if (lost.length === 0) return results;
  const answer = inlineAnswer(url);
  if (answer !== undefined)
    return {
      ...results,
      ...Object.fromEntries(lost.map((id) => [id, { type: "data", result: answer }])),
    };
  if (
    request.method !== "GET" ||
    !url.pathname.startsWith("/players/") ||
    shownPage !== url.pathname + url.search
  )
    return results;
  return Object.fromEntries(
    Object.entries(results).flatMap(([id, result]) =>
      !lost.includes(id)
        ? [[id, result]]
        : id === "root"
          ? [[id, { type: "data", result: LOGGED_OUT }]]
          : [],
    ),
  );
};
