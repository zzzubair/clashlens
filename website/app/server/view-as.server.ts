/**
 * View as: the site owner opens the dashboard as any player, read only.
 *
 * The Python service alone decides who the owner is, from the signed-in
 * account (python/src/clashlens/api_view_as.py); nothing the browser sends
 * can grant it. The only thing the browser names is which player, through
 * the dashboard address's `view_as`, and only the dashboard's read of its
 * cards uses it. Every other request keeps the signed-in account, and any
 * request that could change something refuses to run with `view_as` on it.
 */

import { mapLinkedPlayerCard, type LinkedPlayerCard } from "../lib/account-contracts";
import { VIEW_AS_PARAM } from "../lib/dashboard";
import { normalizePlayerTag } from "../lib/player-tag";
import { requestJson, type GoogleAccountIdentity } from "../services/python.server";
import { isRecord } from "../services/python-response.server";

export type ViewAsAccess =
  | { owner: false }
  /** `player` is null when no player has the tag asked for. */
  | { owner: true; player: LinkedPlayerCard | null };

/** What anyone the owner check cannot confirm gets: the normal dashboard. */
const NOT_OWNER: ViewAsAccess = { owner: false };

/**
 * A typed tag, with or without its #; null when it cannot be a tag.
 */
export function viewAsTag(value: string | null): string | null {
  if (value === null) return null;
  const typed = value.trim();
  return normalizePlayerTag(typed.startsWith("#") ? typed : `#${typed}`);
}

/**
 * Whether the signed-in account is the owner and, for the owner, the card of
 * the player `typed` names. Anyone else, and any failed read, is not the
 * owner: their dashboard is the same whatever they typed.
 */
export async function readViewAs(
  identity: GoogleAccountIdentity,
  typed: string | null,
): Promise<ViewAsAccess> {
  const tag = viewAsTag(typed);
  try {
    const payload = await requestJson<unknown>(
      `/v1/account/view-as${tag === null ? "" : `?tag=${encodeURIComponent(tag)}`}`,
      "GET",
      undefined,
      "view-as",
      undefined,
      identity,
    );
    if (!isRecord(payload) || payload.owner !== true) return NOT_OWNER;
    if (payload.player === null) return { owner: true, player: null };
    const player = mapLinkedPlayerCard(payload.player);
    return player === null || player.tag !== tag ? NOT_OWNER : { owner: true, player };
  } catch {
    return NOT_OWNER;
  }
}

/**
 * Root middleware: `view_as` is read by one thing only, a GET or HEAD of the
 * dashboard (`/`, or its data at `/_.data`). Any other request carrying it is
 * refused before any action or loader runs, whoever sends it: a save, a link,
 * a sign-in callback, a Crew, Watchlist, Discord or settings change, or a page
 * whose load starts work, so nothing can change from a viewed dashboard.
 */
export function refuseViewAsChanges(request: Request): Response | null {
  const url = new URL(request.url);
  if (!url.searchParams.has(VIEW_AS_PARAM)) return null;
  const method = request.method.toUpperCase();
  const dashboard = url.pathname === "/" || url.pathname === "/_.data";
  if (dashboard && (method === "GET" || method === "HEAD")) return null;
  return new Response("View as is read only. Exit View as to make changes.", {
    status: 400,
    headers: { "Cache-Control": "private, no-store", "Content-Type": "text/plain" },
  });
}
