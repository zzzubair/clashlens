import { useEffect } from "react";
import {
  isRouteErrorResponse,
  useLocation,
  type DataStrategyResult,
  type MiddlewareFunction,
} from "react-router";

import type { WebsiteErrorResponse } from "./contracts";
import type { PlayerSearchLoaderData } from "../routes/player-search";

// The address the browser shows now, so only its own re-read can keep it.
let shownPage: string | null = null;

export function rememberShownPage(url: string) {
  shownPage = url;
}

export function useRememberShownPage() {
  const { pathname, search } = useLocation();
  useEffect(() => rememberShownPage(pathname + search));
}

// No answer from the website: the phone was offline or just woke, the
// connection dropped mid-answer, or a proxy sent its own error page.
function lostConnection(error: unknown) {
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
// browser has not shown yet can fail. Other pages show the error as before.
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
  return Object.fromEntries(Object.entries(results).filter(([id]) => !lost.includes(id)));
};
