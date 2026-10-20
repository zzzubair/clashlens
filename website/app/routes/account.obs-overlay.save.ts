import { data } from "react-router";

import { isOverlayLinkId, type OverlaySettings } from "../lib/obs-overlay";
import type { Route } from "./+types/account.obs-overlay.save";

const NO_STORE = { "Cache-Control": "no-store" };

/**
 * POST /account/obs-overlay/save — the Stream overlay page's saves, without
 * reloading the page: `intent=save` with `tag` and `settings` (JSON), or
 * `intent=new-link` with the link it `replaces`.
 */
export async function action({ request }: Route.ActionArgs) {
  if (request.method !== "POST")
    return data({ error: "method" }, { status: 405, headers: NO_STORE });
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);
  const actions = await import("../server/actions.server");
  const { getWebsiteConfig } = await import("../server/config.server");
  if (!actions.isSameOrigin(request, getWebsiteConfig().publicOrigin))
    return data({ error: "forbidden" }, { status: 403, headers: NO_STORE });
  const form = await actions.parseBoundedFormData(request);
  const overlay = await import("../services/obs-overlay.server");
  try {
    if (form?.["intent"] === "save") {
      const settings = parseSettings(form["settings"] ?? "");
      const tag = form["tag"] || null;
      if (settings === null)
        return data({ error: "invalid" }, { status: 400, headers: NO_STORE });
      const version = await overlay.saveOverlay(identity, tag, settings);
      return data({ version }, { headers: NO_STORE });
    }
    if (form?.["intent"] === "new-link" && isOverlayLinkId(form["replaces"])) {
      const link = await overlay.newOverlayLink(identity, form["replaces"]);
      return data({ link }, { headers: NO_STORE });
    }
  } catch {
    return data({ error: "unavailable" }, { status: 503, headers: NO_STORE });
  }
  return data({ error: "invalid" }, { status: 400, headers: NO_STORE });
}

function parseSettings(text: string): OverlaySettings | null {
  try {
    const value: unknown = JSON.parse(text);
    if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
    const entries = Object.entries(value);
    if (entries.length > 20 || entries.some(([, item]) => typeof item !== "string"))
      return null;
    return Object.fromEntries(entries) as OverlaySettings;
  } catch {
    return null;
  }
}
