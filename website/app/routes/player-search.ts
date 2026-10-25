import type { LoaderFunctionArgs } from "react-router";

import { MAX_SEARCH_QUERY_LENGTH } from "../lib/validation";
import type { SearchResponse, WebsiteErrorResponse } from "../lib/contracts";

export interface PlayerSearchLoaderData {
  query: string;
  search: SearchResponse | null;
  error: WebsiteErrorResponse | null;
}

export async function loader({
  request,
  context,
}: LoaderFunctionArgs): Promise<Response> {
  const rawQuery = new URL(request.url).searchParams.get("q") ?? "";
  const query = rawQuery.trim();

  if (rawQuery.length > MAX_SEARCH_QUERY_LENGTH) {
    return noStoreJson({
      query,
      search: null,
      error: {
        error: {
          code: "invalid_input",
          message: "Check the submitted value and try again.",
        },
      },
    });
  }

  if (query === "") return noStoreJson({ query, search: null, error: null });

  const { clientAddressContext } = await import("../server/client-address.server");
  const { allowSearch, visitorRefusal } = await import("../server/abuse.server");
  // Both search addresses share one allowance, checked before any backend search.
  const refusal = visitorRefusal(context?.get(clientAddressContext), allowSearch);
  if (refusal) {
    const { safeWebsiteError } = await import("../server/errors.server");
    return noStoreJson(
      { query, search: null, error: safeWebsiteError(refusal) },
      refusal.status,
    );
  }

  try {
    const { createPythonClient } = await import("../services/python.server");
    return noStoreJson({
      query,
      search: await createPythonClient().searchPlayers(query, 5),
      error: null,
    });
  } catch (cause) {
    const { safeWebsiteError } = await import("../server/errors.server");
    return noStoreJson({ query, search: null, error: safeWebsiteError(cause) });
  }
}

function noStoreJson(payload: PlayerSearchLoaderData, status = 200): Response {
  const retryAfter = payload.error?.error.retryAfterSeconds;
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Cache-Control": "no-store",
      "Content-Type": "application/json; charset=utf-8",
      ...(retryAfter ? { "Retry-After": String(retryAfter) } : {}),
    },
  });
}
