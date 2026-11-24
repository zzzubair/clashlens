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
import { type LoginIdentity } from "../server/auth.server";
import { searchLeaderboard } from "./leaderboard-search.server";
import { getPlayer } from "./players.server";
import { PythonApiError, isRecord } from "./python-response.server";
import { requestJson } from "./python-transport.server";

export async function getOverlay(identity: LoginIdentity): Promise<OverlayAccount> {
  const payload = await requestJson<unknown>("/v1/account/obs-overlay", {
    method: "GET",
    expectedKind: "obs-overlay",
    identity: identity,
  });
  const overlay = parseOverlayAccount(payload);
  if (overlay === null) throw new PythonApiError(502, { error: "malformed" });
  return overlay;
}

export async function saveOverlay(
  identity: LoginIdentity,
  tag: string | null,
  settings: OverlaySettings,
): Promise<number> {
  const payload = await requestJson<{ version: number }>("/v1/account/obs-overlay", {
    method: "POST",
    body: Buffer.from(JSON.stringify({ tag, settings }), "utf8"),
    expectedKind: "obs-overlay-saved",
    identity: identity,
  });
  return payload.version;
}

export async function newOverlayLink(
  identity: LoginIdentity,
  replaces: string,
): Promise<string> {
  const payload = await requestJson<{ link?: unknown }>("/v1/account/obs-overlay/link", {
    method: "POST",
    body: Buffer.from(JSON.stringify({ replaces }), "utf8"),
    expectedKind: "obs-overlay",
    identity: identity,
  });
  if (!isOverlayLinkId(payload.link))
    throw new PythonApiError(502, { error: "malformed" });
  return payload.link;
}

export async function readOverlayLink(link: string): Promise<OverlayLink | null> {
  const payload = await requestJson<unknown>(`/v1/overlays/${encodeURIComponent(link)}`, {
    method: "GET",
    allowedStatuses: [404],
  });
  if (isRecord(payload) && payload.error === "overlay_not_found") return null;
  const overlay = parseOverlayLink(payload);
  if (overlay === null) throw new PythonApiError(502, { error: "malformed" });
  return overlay;
}

const PLAYER_CACHE_MS = 15_000;
const MAX_CACHED_PLAYERS = 2_000;
const players = new Map<string, { at: number; value: Promise<OverlayPlayer> }>();

export function getOverlayPlayer(tag: string): Promise<OverlayPlayer> {
  const now = Date.now();
  const hit = players.get(tag);
  if (hit && now - hit.at < PLAYER_CACHE_MS) return hit.value;
  for (const [key, entry] of players) {
    if (now - entry.at < PLAYER_CACHE_MS) break;
    players.delete(key);
  }
  players.delete(tag);
  if (players.size >= MAX_CACHED_PLAYERS) {
    const oldest = players.keys().next().value;
    if (oldest !== undefined) players.delete(oldest);
  }
  const value = readPlayer(tag);
  const entry = { at: now, value };
  players.set(tag, entry);
  value.catch(() => {
    if (players.get(tag) === entry) players.delete(tag);
  });
  return value;
}

async function readPlayer(tag: string): Promise<OverlayPlayer> {
  const searchRead = searchLeaderboard(tag).catch(() => null);
  let page;
  try {
    page = await getPlayer(tag);
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
      of: page.calendar.day_count,
      gained,
      lost,
      net: day.trophyChange ?? day.presentation!.battle_net_complete,
      battles,
    };
  }
  if (out.state === "legend") {
    const search = await searchRead;
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
