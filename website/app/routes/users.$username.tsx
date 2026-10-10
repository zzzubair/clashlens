import {
  Link,
  data,
  redirect,
  useLoaderData,
  useLocation,
  useRouteLoaderData,
  useSearchParams,
} from "react-router";

import type { LinkedPlayerCard, PublicUser } from "../lib/account-contracts";
import { normalizeUsername } from "../lib/account-validation";
import type { WebsiteErrorResponse } from "../lib/contracts";
import { LOOKUP_MESSAGES, lookupExplanation } from "../lib/player-lookup-text";
import { canonicalPlayerPath } from "../lib/player-tag";
import type { Route } from "./+types/users.$username";
import type { RootLoaderData } from "../root";

import "../user-profile.css";

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
  const location = useLocation();
  const navigation = useRouteLoaderData<RootLoaderData>("root");
  const isOwnProfile = Boolean(
    data.user && navigation?.accountUsername === data.user.username,
  );
  // Set by a successful link on /account/verify-player.
  const [searchParams] = useSearchParams();
  const linkedTag = searchParams.get("linked");
  const justLinked = data.user?.verifiedPlayers.find(
    (player) => player.tag === linkedTag,
  );
  if (data.notFound) {
    return (
      <main id="main-content" tabIndex={-1} className="page-shell narrow-shell">
        <section className="hero" aria-labelledby="user-not-found-title">
          <h1 id="user-not-found-title">User not found</h1>
          <p>No Clash Lens user exists at this address.</p>
        </section>
      </main>
    );
  }
  if (data.error) {
    return (
      <main id="main-content" tabIndex={-1} className="page-shell narrow-shell">
        <h1>User profile</h1>
        <aside className="notice notice-unavailable" role="alert">
          <strong>Profile could not be loaded.</strong>{" "}
          <a href={location.pathname}>Try again</a>
        </aside>
      </main>
    );
  }
  return (
    <main id="main-content" tabIndex={-1} className="page-shell narrow-shell">
      <section className="hero" aria-labelledby="user-title">
        <h1 id="user-title">{data.user?.displayName ?? "User"}</h1>
        <p className="player-tag">@{data.user?.username ?? ""}</p>
        {isOwnProfile ? (
          <p className="hero-actions">
            <Link className="button button-secondary" to="/account/profile">
              Edit profile
            </Link>
            <Link className="button button-secondary" to="/account/providers">
              Sign-in connections
            </Link>
          </p>
        ) : null}
      </section>

      {justLinked ? (
        <div className="status-banner status-banner-success" role="status">
          Linked {justLinked.tag}
        </div>
      ) : null}

      <section className="data-section" aria-labelledby="user-players-title">
        <div className="section-heading">
          <h2 id="user-players-title">Linked accounts</h2>
          {isOwnProfile ? (
            <Link className="button button-secondary" to="/account/verify-player">
              Link your Clash player
            </Link>
          ) : null}
        </div>
        {data.user && data.user.verifiedPlayers.length > 0 ? (
          <ul className="linked-player-cards">
            {data.user.verifiedPlayers.map((player) => (
              <li key={player.tag}>
                <LinkedPlayer player={player} />
              </li>
            ))}
          </ul>
        ) : (
          <div className="empty-state">
            <h3>No linked accounts yet</h3>
            <p>This user has not linked any Clash of Clans accounts yet.</p>
          </div>
        )}
      </section>
    </main>
  );
}

/**
 * The whole card is one link to the player page, named by the player's name
 * and tag; the numbers are its description. A player without current results
 * also says what their own page says.
 */
function LinkedPlayer({ player }: { player: LinkedPlayerCard }) {
  const id = `linked-player-${player.tag.slice(1)}`;
  const note =
    player.state === "tracking" && player.reason === null
      ? null
      : ((player.state === "tracking"
          ? lookupExplanation(player.reason, player.name)
          : null) ?? LOOKUP_MESSAGES[player.state]);
  return (
    <a
      className="linked-player-card"
      href={canonicalPlayerPath(player.tag)}
      aria-labelledby={`${id}-name ${id}-tag`}
      aria-describedby={`${player.clan ? `${id}-clan ` : ""}${id}-details${note === null ? "" : ` ${id}-note`}`}
    >
      <span className="linked-player-identity">
        <strong className="linked-player-name" id={`${id}-name`}>
          {player.name ?? player.tag}
        </strong>
        <span className="player-tag" id={`${id}-tag`}>
          {player.tag}
        </span>
        {player.clan ? (
          <span className="linked-player-clan" id={`${id}-clan`}>
            {player.clan}
          </span>
        ) : null}
      </span>
      <span className="linked-player-stats" id={`${id}-details`}>
        <span className="linked-player-stat">
          <small>Trophies</small>
          {player.trophies === null ? (
            <span className="linked-player-wait">
              {player.seasonResetPending
                ? "Waiting for this player's Season reset"
                : "Unknown"}
            </span>
          ) : (
            <strong>{player.trophies.toLocaleString("en-GB")}</strong>
          )}
        </span>
        <span className="linked-player-stat">
          <small>Rank</small>
          <strong>
            {player.rank === null
              ? (player.league ?? "Unranked")
              : `#${player.rank.toLocaleString("en-GB")}`}
          </strong>
        </span>
        <span className="linked-player-stat linked-player-today">
          <small>Today</small>
          {player.today === null ? (
            <span className="linked-player-wait">Not available yet</span>
          ) : (
            <>
              <span>
                <strong className={netTone(player.today.net)}>
                  {formatNet(player.today.net)}
                </strong>
                {player.today.net === null ? null : " so far"}
              </span>
              {player.today.attacks !== null && player.today.defenses !== null ? (
                <span className="linked-player-battles">
                  {player.today.attacks}/8 attacks · {player.today.defenses}/8 defenses
                </span>
              ) : null}
            </>
          )}
        </span>
      </span>
      {note === null ? null : (
        <span className="linked-player-note" id={`${id}-note`}>
          {note}
        </span>
      )}
    </a>
  );
}

function formatNet(value: number | null): string {
  if (value === null) return "Unknown";
  return value > 0 ? `+${value}` : String(value);
}

function netTone(value: number | null): string | undefined {
  if (value === null || value === 0) return undefined;
  return value > 0 ? "linked-player-gain" : "linked-player-loss";
}
