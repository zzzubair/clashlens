import { data } from "react-router";

import { isOverlayLinkId } from "../lib/obs-overlay";
import type { Route } from "./+types/obs-overlay.settings";

const NO_STORE = { "Cache-Control": "no-store", "Referrer-Policy": "no-referrer" };

export async function loader({ params }: Route.LoaderArgs) {
  const link = params.link ?? "";
  if (!isOverlayLinkId(link))
    return data({ error: "not_found" }, { status: 404, headers: NO_STORE });
  const { readOverlayLink } = await import("../services/obs-overlay.server");
  try {
    const overlay = await readOverlayLink(link);
    if (overlay === null)
      return data({ error: "not_found" }, { status: 404, headers: NO_STORE });
    return data(overlay, { headers: NO_STORE });
  } catch {
    return data({ error: "unavailable" }, { status: 503, headers: NO_STORE });
  }
}
