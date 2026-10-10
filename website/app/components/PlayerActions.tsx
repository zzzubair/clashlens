import type { ReactNode } from "react";
import { Link, useRouteLoaderData } from "react-router";

import type { RootLoaderData } from "../root";

// The profile's one action row: the page's own actions, then Add to group for
// a signed-in Clasher.
export function PlayerActions({ tag, children }: { tag: string; children?: ReactNode }) {
  const navigation = useRouteLoaderData<RootLoaderData>("root");
  const add = navigation?.loggedIn ? (
    <Link
      className="button button-secondary"
      to={`/account/groups/add?tag=${encodeURIComponent(tag)}`}
    >
      Add to group
    </Link>
  ) : null;
  return children || add ? (
    <div className="player-actions">
      {children}
      {add}
    </div>
  ) : null;
}
