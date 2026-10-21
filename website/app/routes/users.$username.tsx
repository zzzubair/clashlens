import {
  Link,
  data,
  redirect,
  useLoaderData,
  useLocation,
  useRouteLoaderData,
  useSearchParams,
} from "react-router";

import { useBackState } from "../components/BackLink";
import { SavedLine } from "../components/SavedLine";
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
  /** Only for the signed-in owner of this profile, null for everyone else. */
  owner: OwnerTools | null;
}

/** The status line on each of the owner's tools; null when it could not be read. */
export interface OwnerTools {
  alerts: string | null;
  overlay: string | null;
}

/**
 * GET /users/:username — the public user page from the anonymous Python
 * client. Only the canonical username, display name, and verified player
 * links are shown; Google identity, groups, preferences, and
 * internal IDs never appear. The signed-in owner also gets the status of
 * their Discord alerts and Stream overlay.
 */
export async function loader({ params, request }: Route.LoaderArgs) {
  const rawUsername = params.username ?? "";
  const username = normalizeUsername(rawUsername);
  if (username === null) {
    return data<UserLoaderData>(
      { user: null, notFound: true, error: null, owner: null },
      { status: 404, headers: NO_STORE },
    );
  }
  if (username !== rawUsername) {
    throw redirect(`/users/${encodeURIComponent(username)}`);
  }
  try {
    const { createPythonClient } = await import("../services/python.server");
    const [user, owner] = await Promise.all([
      createPythonClient().getPublicUser(username),
      ownerTools(request, username),
    ]);
    return data<UserLoaderData>(
      { user, notFound: false, error: null, owner },
      { headers: NO_STORE },
    );
  } catch (cause) {
    const { safeWebsiteError } = await import("../server/errors.server");
    const error = safeWebsiteError(cause);
    if (error.error.code === "missing") {
      return data<UserLoaderData>(
        { user: null, notFound: true, error: null, owner: null },
        { status: 404, headers: NO_STORE },
      );
    }
    return data<UserLoaderData>(
      { user: null, notFound: false, error, owner: null },
      { status: 422, headers: NO_STORE },
    );
  }
}

/** The owner's tool statuses, or null when the visitor is anyone else. */
async function ownerTools(
  request: Request,
  username: string,
): Promise<OwnerTools | null> {
  try {
    const { getWebsiteConfig } = await import("../server/config.server");
    const actions = await import("../server/actions.server");
    const identity = await actions.readLoginIdentity(request, getWebsiteConfig());
    if (identity === null) return null;
    const { createPythonClient } = await import("../services/python.server");
    const account = await createPythonClient(identity).getAccount();
    if (account.username !== username) return null;
    const connected = account.providers.includes("discord");
    const { getAttackAlerts } = await import("../services/discord-alerts.server");
    const { getOverlay } = await import("../services/obs-overlay.server");
    const [alerts, overlay] = await Promise.all([
      connected ? getAttackAlerts(identity).catch(() => null) : null,
      getOverlay(identity).catch(() => null),
    ]);
    const on = alerts?.players.filter((player) => player.on).length;
    const total = alerts?.players.length ?? 0;
    const shown = overlay?.players.find((player) => player.tag === overlay.tag);
    return {
      alerts: !connected
        ? "Discord not connected"
        : alerts === null
          ? null
          : !on
            ? "Off"
            : total === 1
              ? "On"
              : `On for ${on} of ${total} players`,
      overlay:
        overlay === null
          ? null
          : overlay.tag === null
            ? "Not set up"
            : `Showing ${shown?.name ?? overlay.tag}`,
    };
  } catch {
    return null;
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
  const players = data.user?.verifiedPlayers ?? [];
  return (
    <main id="main-content" tabIndex={-1} className="page-shell narrow-shell">
      {justLinked ? (
        <SavedLine>{justLinked.name ?? justLinked.tag} linked.</SavedLine>
      ) : null}
      <section className="hero profile-hero" aria-labelledby="user-title">
        <div className="profile-name">
          <h1 id="user-title">{data.user?.displayName ?? "User"}</h1>
          {isOwnProfile ? (
            <Link
              className="button button-secondary profile-edit"
              to="/account/settings"
              aria-label="Edit your profile in Settings"
            >
              Edit
            </Link>
          ) : null}
        </div>
        <p className="player-tag">@{data.user?.username ?? ""}</p>
      </section>

      {isOwnProfile ? <YourTools tools={data.owner} /> : null}

      <section className="data-section" aria-labelledby="user-players-title">
        <h2 id="user-players-title">Clash players</h2>
        {players.length > 0 ? (
          <ul className="linked-player-cards">
            {players.map((player) => (
              <li key={player.tag}>
                <LinkedPlayer
                  player={player}
                  backLabel={isOwnProfile ? "Your profile" : data.user!.displayName}
                />
              </li>
            ))}
          </ul>
        ) : isOwnProfile ? (
          <div className="empty-state">
            <p>Link your Clash player to see your Legend days here.</p>
            <Link className="button button-primary" to="/account/verify-player">
              Link your Clash player
            </Link>
          </div>
        ) : (
          <div className="empty-state">
            <p>This user hasn&apos;t linked a Clash player yet.</p>
          </div>
        )}
      </section>
    </main>
  );
}

/** The owner's two tools, each one card with what it does and its status. */
function YourTools({ tools }: { tools: OwnerTools | null }) {
  const cards = [
    {
      to: "/account/discord-alerts",
      title: "Discord alerts",
      text: "A Discord message when someone attacks your players.",
      status: tools?.alerts ?? null,
    },
    {
      to: "/account/obs-overlay",
      title: "Stream overlay",
      text: "Your live Legend day on your stream.",
      status: tools?.overlay ?? null,
    },
  ];
  return (
    <section className="data-section" aria-labelledby="your-tools-title">
      <h2 id="your-tools-title">Your tools</h2>
      <ul className="profile-tools">
        {cards.map((card) => (
          <li key={card.to}>
            <Link className="profile-tool" to={card.to}>
              <strong>{card.title}</strong>
              <span>{card.text}</span>
              {card.status === null ? null : (
                <span className="profile-tool-status">{card.status}</span>
              )}
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}

/**
 * The whole card is one link to the player page, named by the player's name
 * and tag; the numbers are its description. A player without current results
 * also says what their own page says.
 */
function LinkedPlayer({
  player,
  backLabel,
}: {
  player: LinkedPlayerCard;
  backLabel: string;
}) {
  const backState = useBackState(backLabel);
  const id = `linked-player-${player.tag.slice(1)}`;
  const note =
    player.state === "tracking" && player.reason === null
      ? null
      : ((player.state === "tracking"
          ? lookupExplanation(player.reason, player.name)
          : null) ?? LOOKUP_MESSAGES[player.state]);
  return (
    <Link
      className="linked-player-card"
      to={canonicalPlayerPath(player.tag)}
      state={backState}
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
    </Link>
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
