import { data } from "react-router";

import { normalizePlayerTag } from "../lib/player-tag";
import type { Route } from "./+types/obs-overlay.player";

const NO_STORE = { "Cache-Control": "no-store" };

/**
 * GET /resources/players/:tag/overlay — one player's live Legend day,
 * trophies and Rank, as the OBS overlay shows them. Overlays read it every
 * 20 seconds; every overlay showing a player shares one read per 15 seconds.
 */
export async function loader({ params }: Route.LoaderArgs) {
  const tag = normalizePlayerTag(params.tag ?? "");
  if (!tag) return data({ error: "invalid_tag" }, { status: 400, headers: NO_STORE });
  const { getOverlayPlayer } = await import("../services/obs-overlay.server");
  try {
    return data(await getOverlayPlayer(tag), { headers: NO_STORE });
  } catch {
    return data({ error: "unavailable" }, { status: 503, headers: NO_STORE });
  }
}
