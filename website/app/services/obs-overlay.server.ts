import {
  isOverlayLinkId,
  parseOverlayAccount,
  parseOverlayLink,
  type OverlayAccount,
  type OverlayBattle,
  type OverlayLink,
  type OverlayPlayer,
  type OverlaySettings,
} from "../lib/obs-overlay";
import { PythonApiError, requestJson, type GoogleAccountIdentity } from "./python.server";

/** The account's overlay, made on first ask, and its verified players. */
export async function getOverlay(
  identity: GoogleAccountIdentity,
): Promise<OverlayAccount> {
  const payload = await requestJson<unknown>(
    "/v1/account/obs-overlay",
    "GET",
    undefined,
    "obs-overlay",
    undefined,
    identity,
  );
  const overlay = parseOverlayAccount(payload);
  if (overlay === null) throw new PythonApiError(502, { error: "malformed" });
  return overlay;
}

/** Save the player shown and the settings; returns the new version. */
export async function saveOverlay(
  identity: GoogleAccountIdentity,
  tag: string | null,
  settings: OverlaySettings,
): Promise<number> {
  const payload = await requestJson<{ version: number }>(
    "/v1/account/obs-overlay",
    "POST",
    Buffer.from(JSON.stringify({ tag, settings }), "utf8"),
    "obs-overlay-saved",
    undefined,
    identity,
  );
  return payload.version;
}

/** Replace the private link `replaces`; the old one shows nothing from then
 * on. Returns the new link. */
export async function newOverlayLink(
  identity: GoogleAccountIdentity,
  replaces: string,
): Promise<string> {
  const payload = await requestJson<{ link?: unknown }>(
    "/v1/account/obs-overlay/link",
    "POST",
    Buffer.from(JSON.stringify({ replaces }), "utf8"),
    "obs-overlay",
    undefined,
    identity,
  );
  if (!isOverlayLinkId(payload.link))
    throw new PythonApiError(502, { error: "malformed" });
  return payload.link;
}

/** What an overlay in OBS shows, by its private link; null once replaced. */
export async function readOverlayLink(link: string): Promise<OverlayLink | null> {
  const payload = await requestJson<unknown>(
    `/v1/overlays/${encodeURIComponent(link)}`,
    "GET",
    undefined,
    undefined,
    undefined,
    undefined,
    [404],
  );
  if ((payload as { error?: unknown } | null)?.error === "overlay_not_found") return null;
  const overlay = parseOverlayLink(payload);
  if (overlay === null) throw new PythonApiError(502, { error: "malformed" });
  return overlay;
}

// Every overlay showing a player shares one read of it per 15 seconds.
const PLAYER_CACHE_MS = 15_000;
const players = new Map<string, { at: number; value: Promise<OverlayPlayer> }>();

/** One player's live Legend day, trophies and Rank, as the overlay shows it. */
export function getOverlayPlayer(tag: string): Promise<OverlayPlayer> {
  const now = Date.now();
  const hit = players.get(tag);
  if (hit && now - hit.at < PLAYER_CACHE_MS) return hit.value;
  for (const [key, entry] of players)
    if (now - entry.at >= PLAYER_CACHE_MS) players.delete(key);
  const value = readPlayer(tag);
  players.set(tag, { at: now, value });
  value.catch(() => players.delete(tag));
  return value;
}

async function readPlayer(tag: string): Promise<OverlayPlayer> {
  const { createPythonClient } = await import("./python.server");
  const client = createPythonClient();
  let page;
  try {
    page = await client.getPlayer(tag);
  } catch (error) {
    if (error instanceof PythonApiError && error.status === 404)
      return { tag, state: "missing" };
    throw error;
  }
  const profile = page.profile;
  const out: OverlayPlayer = {
    tag,
    state: page.trackingState === "tracking" ? "legend" : page.trackingState,
    name: profile.name,
    trophies: profile.trophies,
    profileReadAt: profile.freshness.observedAt,
  };
  const day = page.currentDay;
  if (day) {
    const battles: OverlayBattle[] = [
      ...day.offenseEvents.map((event) => battle(event, "attack")),
      ...day.defenseEvents.map((event) => battle(event, "defense")),
    ].sort((a, b) => a.at.localeCompare(b.at));
    const complete = day.battlesComplete === true;
    const gained = complete ? day.offense.trophyGain : null;
    const lost = complete ? day.defense.trophyLoss : null;
    out.day = {
      number: day.dayNumber,
      of: page.season?.dayCount ?? 28,
      gained,
      lost,
      net: day.trophyChange ?? (gained !== null && lost !== null ? gained - lost : null),
      battles,
    };
  }
  if (out.state === "legend") {
    // Rank is the Live Leaderboard's place among the players Clash Lens tracks.
    const search = await client.searchLeaderboard(tag).catch(() => null);
    const entry = search?.results.find((result) => result.tag === tag);
    if (entry) out.rank = entry.rank;
  }
  return out;
}

function battle(
  event: {
    battleId: string;
    battleTimestamp: string;
    stars: number;
    destructionPercentage: number;
    trophyChange: number;
    opponent: { name: string | null };
  },
  kind: "attack" | "defense",
): OverlayBattle {
  return {
    id: `${kind}-${event.battleId}`,
    kind,
    at: event.battleTimestamp,
    stars: event.stars,
    destruction: event.destructionPercentage,
    trophies: event.trophyChange,
    opponent: event.opponent.name,
  };
}
