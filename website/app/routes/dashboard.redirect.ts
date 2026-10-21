import { redirect } from "react-router";

import type { Route } from "./+types/dashboard.redirect";

/** GET /dashboard — the Dashboard moved to `/`; an old link keeps its player. */
export function loader({ request }: Route.LoaderArgs): never {
  throw redirect(`/${new URL(request.url).search}`, 301);
}
