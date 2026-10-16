import { data } from "react-router";

import type { ArmiesResponse } from "../components/ArmyCards";
import type { ArmyLens } from "../lib/dashboard-armies";
import { isArmyDays } from "../lib/dashboard-armies";
import { normalizePlayerTag } from "../lib/player-tag";
import type { Route } from "./+types/player-armies";

const NO_STORE = { "Cache-Control": "no-store" };

/**
 * GET /resources/players/:tag/armies?lens=&days= — what the army cards'
 * day switches load. Signed-in users only; while
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
    return data<ArmiesResponse>({ armies: null }, { status: 401, headers: NO_STORE });

  const query = new URL(request.url).searchParams;
  const tag = normalizePlayerTag(params.tag);
  const lens = query.get("lens");
  const days = query.get("days") === "season" ? "season" : Number(query.get("days"));
  if (!tag || (lens !== "offense" && lens !== "defense") || !isArmyDays(days, lens))
    return data<ArmiesResponse>({ armies: null }, { status: 400, headers: NO_STORE });

  const { getPlayerArmies } = await import("../services/dashboard.server");
  const armies = await getPlayerArmies(tag, lens as ArmyLens, days).catch(() => null);
  return data<ArmiesResponse>({ armies }, { headers: NO_STORE });
}
