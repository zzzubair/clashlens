import type { PastSeasonFinish } from "../lib/contracts";
import { requestJson } from "./python.server";
import {
  PythonApiError,
  isInteger,
  isRecord,
  isResetTimestamp,
  isString,
} from "./python-response.server";

// Under the page's five-second streaming limit; a slow ClashKing only hides
// the section.
const PAST_SEASONS_TIMEOUT_MS = 4_000;

export async function getPastSeasons(tag: string): Promise<PastSeasonFinish[]> {
  const payload = await requestJson<unknown>(
    `/v1/players/${encodeURIComponent(tag)}/past-seasons`,
    "GET",
    undefined,
    undefined,
    undefined,
    undefined,
    undefined,
    PAST_SEASONS_TIMEOUT_MS,
  );
  if (!isRecord(payload) || payload.tag !== tag || !Array.isArray(payload.seasons))
    throw new PythonApiError(502, { error: "malformed" });
  return payload.seasons.map((item) => {
    const dated =
      isRecord(item) &&
      isString(item.season_id) &&
      /^\d+$/.test(item.season_id) &&
      isResetTimestamp(item.season_start) &&
      isResetTimestamp(item.season_end);
    const monthly =
      isRecord(item) &&
      isString(item.season_id) &&
      /^\d{4}-\d{2}$/.test(item.season_id) &&
      item.season_start === null &&
      item.season_end === null;
    if (
      !isRecord(item) ||
      !(dated || monthly) ||
      !isInteger(item.trophies) ||
      item.trophies < 0 ||
      !(
        item.global_rank === null ||
        (isInteger(item.global_rank) && item.global_rank >= 1)
      )
    )
      throw new PythonApiError(502, { error: "malformed" });
    return {
      seasonId: item.season_id as string,
      seasonStart: item.season_start as string | null,
      seasonEnd: item.season_end as string | null,
      trophies: item.trophies,
      globalRank: item.global_rank,
    };
  });
}
