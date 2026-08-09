import type { LoaderFunctionArgs } from "react-router";

import { MAX_SEARCH_QUERY_LENGTH } from "../lib/validation";
import type { SearchResponse, WebsiteErrorResponse } from "../lib/contracts";

export interface PlayerSearchLoaderData {
  search: SearchResponse | null;
  error: WebsiteErrorResponse | null;
}

export async function loader({ request }: LoaderFunctionArgs): Promise<Response> {
  const rawQuery = new URL(request.url).searchParams.get("q") ?? "";
  const query = rawQuery.trim();

  if (rawQuery.length > MAX_SEARCH_QUERY_LENGTH) {
    return noStoreJson({
      search: null,
      error: {
        error: {
          code: "invalid_input",
          message: "Check the submitted value and try again.",
        },
      },
    });
  }

  if (query === "") return noStoreJson({ search: null, error: null });

  try {
    const { createPythonClient } = await import("../services/python.server");
    return noStoreJson({
      search: await createPythonClient().searchPlayers(query, 5),
      error: null,
    });
  } catch (cause) {
    const { safeWebsiteError } = await import("../server/errors.server");
    return noStoreJson({ search: null, error: safeWebsiteError(cause) });
  }
}

function noStoreJson(payload: PlayerSearchLoaderData): Response {
  return new Response(JSON.stringify(payload), {
    headers: {
      "Cache-Control": "no-store",
      "Content-Type": "application/json; charset=utf-8",
    },
  });
}
