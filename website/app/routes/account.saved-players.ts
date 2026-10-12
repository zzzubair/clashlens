import { redirect } from "react-router";

import type { Route } from "./+types/account.saved-players";

/**
 * GET /account/saved-players — Saved Players became groups, so an old link
 * opens Your groups. Signing in first keeps this address as the place to
 * return to.
 */
export async function loader({ request }: Route.LoaderArgs): Promise<Response> {
  const { requireLogin } = await import("../server/auth-guard.server");
  await requireLogin(request);
  throw redirect("/account/groups");
}
