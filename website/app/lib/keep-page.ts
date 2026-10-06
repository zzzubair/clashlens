import { useEffect } from "react";
import {
  isRouteErrorResponse,
  useLocation,
  useMatches,
  type DataStrategyResult,
  type MiddlewareFunction,
} from "react-router";

// What the browser shows now, by route, so a failed re-read can keep it.
let shown: { url: string; data: Map<string, unknown> } | null = null;

export function rememberShownPage(url: string, data: Map<string, unknown>) {
  shown = { url, data };
}

export function useRememberShownPage() {
  const { pathname, search } = useLocation();
  const matches = useMatches();
  useEffect(() => {
    rememberShownPage(
      pathname + search,
      new Map(matches.map((match) => [match.id, match.loaderData])),
    );
  });
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

// Pages reread their own data in the background, such as when a phone wakes.
// A re-read that cannot reach the website keeps the page as it is, and the next
// re-read tries again; only a page the browser has not shown yet can fail.
export const keepPageOnLostConnection: MiddlewareFunction<
  Record<string, DataStrategyResult>
> = async ({ request }, next) => {
  const results = await next();
  const { pathname, search } = new URL(request.url);
  const page = shown;
  const lost = Object.entries(results).filter(
    ([, result]) => result.type === "error" && lostConnection(result.result),
  );
  if (
    request.method !== "GET" ||
    page?.url !== pathname + search ||
    lost.length === 0 ||
    !lost.every(([id]) => page.data.has(id))
  )
    return results;
  return {
    ...results,
    ...Object.fromEntries(
      lost.map(([id]) => [id, { type: "data", result: page.data.get(id) }]),
    ),
  };
};
