import { data } from "react-router";

import type { BasesResponse } from "../components/OpponentsCard";
import { normalizePlayerTag } from "../lib/player-tag";
import type { Route } from "./+types/player-bases";

const NO_STORE = { "Cache-Control": "no-store" };

/**
 * GET /resources/players/:tag/bases?day= — what Bases you attacked loads
 * for an earlier Legend day of this Season. Signed-in users only; while
 * CLASHLENS_DASHBOARD_ENABLED is off it does not exist.
 */
export async function loader({ params, request }: Route.LoaderArgs) {
  const { getWebsiteConfig, isDashboardEnabled } =
    await import("../server/config.server");
  if (!isDashboardEnabled()) throw data(null, { status: 404 });
  const { readLoginIdentity } = await import("../server/actions.server");
  const config = getWebsiteConfig();
  const identity = config.loginEnabled
    ? await readLoginIdentity(request, config).catch(() => null)
    : null;
  // Returned, not thrown: a card shows its own message, never the page's error.
  if (identity === null)
    return data<BasesResponse>({ bases: null }, { status: 401, headers: NO_STORE });

  const tag = normalizePlayerTag(params.tag);
  const day = Number(new URL(request.url).searchParams.get("day"));
  if (!tag || !Number.isInteger(day) || day < 1 || day > 28)
    return data<BasesResponse>({ bases: null }, { status: 400, headers: NO_STORE });

  const { getPlayerBases } = await import("../services/dashboard.server");
  const bases = await getPlayerBases(tag, day).catch(() => null);
  return data<BasesResponse>({ bases }, { headers: NO_STORE });
}
