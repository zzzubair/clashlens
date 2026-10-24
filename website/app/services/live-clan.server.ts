import { requestJson } from "./python.server";
import { isRecord, isString } from "./python-response.server";

/** The player's clan in the game right now; `clan` is null when they are in none. */
export interface LiveClan {
  clan: { name: string; tag: string } | null;
}

// The private API gives the game 1.5 seconds. This streams in after the page,
// under its five-second limit, so a slow answer never holds the page.
const LIVE_CLAN_TIMEOUT_MS = 2_500;

/** Null when the game gave no answer: the page keeps the saved clan. */
export async function getLiveClan(tag: string): Promise<LiveClan | null> {
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
  } catch {
    return null;
  }
  if (!isRecord(payload) || payload.tag !== tag || payload.live !== true) return null;
  const clan = payload.clan;
  if (clan === null) return { clan: null };
  if (
    !isRecord(clan) ||
    !isString(clan.name) ||
    !isString(clan.tag) ||
    !/^#[0-9A-Z]{2,15}$/.test(clan.tag)
  ) {
    return null;
  }
  return { clan: { name: clan.name, tag: clan.tag } };
}
