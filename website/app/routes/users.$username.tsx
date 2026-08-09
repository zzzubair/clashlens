import { Link, data, redirect, useLoaderData } from "react-router";

import { ErrorNotice } from "../components/ErrorNotice";
import type { PublicUser } from "../lib/account-contracts";
import { normalizeUsername } from "../lib/account-validation";
import type { WebsiteErrorResponse } from "../lib/contracts";
import { canonicalPlayerPath } from "../lib/player-tag";
import type { Route } from "./+types/users.$username";

const NO_STORE = { "Cache-Control": "no-store" };

export interface UserLoaderData {
  user: PublicUser | null;
  notFound: boolean;
  error: WebsiteErrorResponse | null;
}

/**
 * GET /users/:username — the public user page from the anonymous Python
 * client. Only the canonical username, display name, and verified player
 * links are shown; Google identity, saved tags, groups, preferences, and
 * internal IDs never appear.
 */
export async function loader({ params }: Route.LoaderArgs) {
  const rawUsername = params.username ?? "";
  const username = normalizeUsername(rawUsername);
  if (username === null) {
    return data<UserLoaderData>(
      { user: null, notFound: true, error: null },
      { status: 404, headers: NO_STORE },
    );
  }
  if (username !== rawUsername) {
    throw redirect(`/users/${encodeURIComponent(username)}`);
  }
  try {
    const { createPythonClient } = await import("../services/python.server");
    const user = await createPythonClient().getPublicUser(username);
    return data<UserLoaderData>(
      { user, notFound: false, error: null },
      { headers: NO_STORE },
    );
  } catch (cause) {
    const { safeWebsiteError } = await import("../server/errors.server");
    const error = safeWebsiteError(cause);
    if (error.error.code === "missing") {
      return data<UserLoaderData>(
        { user: null, notFound: true, error: null },
        { status: 404, headers: NO_STORE },
      );
    }
    return data<UserLoaderData>(
      { user: null, notFound: false, error },
      { status: 422, headers: NO_STORE },
    );
  }
}

export function headers() {
  return NO_STORE;
}

export default function UserRoute() {
  const data = useLoaderData<typeof loader>();
  if (data.notFound) {
    return (
      <main className="page-shell narrow-shell">
        <section className="hero" aria-labelledby="user-not-found-title">
          <h1 id="user-not-found-title">User not found</h1>
          <p>No Clash Lens user exists at this address.</p>
        </section>
        <p className="back-link">
          <Link to="/">← Back to home</Link>
        </p>
      </main>
    );
  }
  return (
    <main className="page-shell narrow-shell">
      <section className="hero" aria-labelledby="user-title">
        <h1 id="user-title">{data.user?.displayName ?? "User"}</h1>
        <p className="player-tag">@{data.user?.username ?? ""}</p>
      </section>

      {data.error ? <ErrorNotice error={data.error} /> : null}

      <section className="data-section" aria-labelledby="user-players-title">
        <h2 id="user-players-title">Verified players</h2>
        {data.user && data.user.verifiedPlayers.length > 0 ? (
          <ul className="player-link-list">
            {data.user.verifiedPlayers.map((player) => (
              <li key={player.tag}>
                <Link to={canonicalPlayerPath(player.tag)}>
                  {player.name ?? player.tag}
                </Link>
                <span className="player-tag">{player.tag}</span>
              </li>
            ))}
          </ul>
        ) : (
          <div className="empty-state">
            <h3>No verified players</h3>
            <p>This user has not verified any player links yet.</p>
          </div>
        )}
      </section>

      <p className="back-link">
        <Link to="/">← Back to home</Link>
      </p>
    </main>
  );
}
