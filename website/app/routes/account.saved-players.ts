import { redirect } from "react-router";

/** GET /account/saved-players — Saved Players became Watchlists, so an old link opens them. */
export async function loader(): Promise<never> {
  throw redirect("/account/watchlists");
}
