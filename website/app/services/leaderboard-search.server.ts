import type { LeaderboardSearch } from "../lib/contracts";
import { MAX_SEARCH_QUERY_LENGTH } from "../lib/validation";
import { requestJson } from "./python.server";
import {
  PythonApiError,
  isCanonicalPlayerTag,
  isInteger,
  isRecord,
  isString,
} from "./python-response.server";

export async function searchLeaderboard(query: string): Promise<LeaderboardSearch> {
  if (!query.trim() || query.length > MAX_SEARCH_QUERY_LENGTH)
    throw new PythonApiError(400, { error: "invalid_input" });
  const payload = await requestJson<unknown>(
    `/v1/leaderboards/live/search?${new URLSearchParams({ q: query })}`,
    "GET",
    undefined,
    undefined,
  );
  if (
    !isRecord(payload) ||
    !(payload.exact_tag === null || isCanonicalPlayerTag(payload.exact_tag)) ||
    typeof payload.has_more !== "boolean" ||
    !Array.isArray(payload.results) ||
    payload.results.length > 20
  )
    throw new PythonApiError(502, { error: "malformed" });
  const results = payload.results.map((item) => {
    if (
      !isRecord(item) ||
      !isCanonicalPlayerTag(item.tag) ||
      !isString(item.name) ||
      !isInteger(item.rank) ||
      item.rank < 1 ||
      !isInteger(item.trophies) ||
      item.trophies < 0
    )
      throw new PythonApiError(502, { error: "malformed" });
    return { tag: item.tag, name: item.name, rank: item.rank, trophies: item.trophies };
  });
  if (
    payload.exact_tag !== null &&
    (results.length !== 1 || results[0].tag !== payload.exact_tag || payload.has_more)
  )
    throw new PythonApiError(502, { error: "malformed" });
  return { exactTag: payload.exact_tag, hasMore: payload.has_more, results };
}
