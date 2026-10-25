import { allowLiveClanRead } from "../server/abuse.server";
import { PythonApiError, requestJson } from "./python.server";
import { isRecord, isString } from "./python-response.server";

/** The player's clan in the game right now; `clan` is null when they are in none. */
export interface LiveClan {
  clan: { name: string; tag: string } | null;
}

// The private API gives the game 1.5 seconds. This streams in after the page,
// under its five-second limit, so a slow answer never holds the page.
const LIVE_CLAN_TIMEOUT_MS = 2_500;

/**
 * Null when the game gave no answer (logged with why), or this visitor asked
 * too often: the page keeps the saved clan.
 */
export async function getLiveClan(
  identity: string | undefined,
  tag: string,
): Promise<LiveClan | null> {
  if (!identity || !allowLiveClanRead(identity)) return null;
  let payload: unknown;
  try {
    payload = await requestJson<unknown>(
      `/v1/players/${encodeURIComponent(tag)}/clan`,
      "GET",
      undefined,
      undefined,
      undefined,
      undefined,
      undefined,
      LIVE_CLAN_TIMEOUT_MS,
    );
  } catch (cause) {
    const timedOut =
      cause instanceof PythonApiError &&
      isRecord(cause.payload) &&
      cause.payload.error === "timeout";
    return fallback(timedOut ? "website_timeout" : "api_error");
  }
  if (!isRecord(payload) || payload.tag !== tag) return fallback("unusable_answer");
  if (payload.live !== true) {
    return fallback(isString(payload.reason) ? payload.reason : "unusable_answer");
  }
  const clan = payload.clan;
  if (clan === null) return { clan: null };
  if (
    !isRecord(clan) ||
    !isString(clan.name) ||
    !isString(clan.tag) ||
    !/^#[0-9A-Z]{2,15}$/.test(clan.tag)
  ) {
    return fallback("unusable_answer");
  }
  return { clan: { name: clan.name, tag: clan.tag } };
}

function fallback(reason: string): null {
  console.warn(JSON.stringify({ event: "live_clan", status: "fallback", reason }));
  return null;
}
