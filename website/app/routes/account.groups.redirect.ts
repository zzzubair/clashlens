import { redirect } from "react-router";

import type { Route } from "./+types/account.groups.redirect";

/** GET /account/groups/* — Groups became Watchlists; an old link opens the same page there. */
export function loader({ request }: Route.LoaderArgs): never {
  const url = new URL(request.url);
  const rest = url.pathname.slice("/account/groups".length);
  throw redirect(`/account/watchlists${rest}${url.search}`, 301);
}
