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
  owner: OwnerControls | null;
}

export interface OwnerControls {
  /** The default account: the player the Discord bot's /stats uses when none is named. */
  defaultPlayer: string | null;
  idempotencyKey: string;
}

/**
 * GET /users/:username — the public user page from the anonymous Python
 * client. Only the canonical username, display name, and verified player
 * links are shown; Google identity, groups, preferences, and
 * internal IDs never appear. The signed-in owner also gets their default
 * account and the key to change it.
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
      ownerControls(request, username),
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

/** The owner's default account, or null when the visitor is anyone else. */
async function ownerControls(
  request: Request,
  username: string,
): Promise<OwnerControls | null> {
  try {
    const { getWebsiteConfig } = await import("../server/config.server");
    const actions = await import("../server/actions.server");
    const identity = await actions.readLoginIdentity(request, getWebsiteConfig());
    if (identity === null) return null;
    const { createPythonClient } = await import("../services/python.server");
    const summary = await createPythonClient(identity).getAccountSummary();
    if (summary.username !== username) return null;
    return {
      defaultPlayer: summary.defaultPlayer,
      idempotencyKey: actions.freshIdempotencyKey(),
    };
  } catch {
    return null;
  }
}

/**
 * POST /users/:username — the signed-in Clasher makes one of their own
 * linked accounts the default account.
 */
export async function action({ params, request }: Route.ActionArgs): Promise<Response> {
  if (request.method !== "POST") {
    return new Response(null, { status: 405, headers: NO_STORE });
  }
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);
  const actions = await import("../server/actions.server");
  const { getWebsiteConfig } = await import("../server/config.server");
  let config;
  try {
    config = getWebsiteConfig();
  } catch {
    throw redirect("/login");
  }
  if (!actions.isSameOrigin(request, config.publicOrigin)) {
    return new Response(null, { status: 403, headers: NO_STORE });
  }
  const form = await actions.parseBoundedFormData(request);
  const idempotencyKey = form?.["idempotencyKey"] ?? "";
  if (
    form === null ||
    !actions.isIdempotencyKey(idempotencyKey) ||
    form["intent"] !== "default-player"
  ) {
    return new Response(null, { status: 400, headers: NO_STORE });
  }
  const back = `/users/${encodeURIComponent(normalizeUsername(params.username ?? "") ?? "")}`;
  try {
    const { createPythonClient } = await import("../services/python.server");
    await createPythonClient(identity).setDefaultPlayer(
      form["tag"] ?? "",
      idempotencyKey,
    );
  } catch {
    return redirect(`${back}?default=failed`, { headers: NO_STORE });
  }
  return redirect(`${back}?default=saved`, { headers: NO_STORE });
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
  const defaultSaved = searchParams.get("default");
  const owner = data.owner;
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
            <Link className="button button-secondary" to="/account/discord-alerts">
              Discord alerts
            </Link>
          </p>
        ) : null}
      </section>

      {justLinked ? (
        <div className="status-banner status-banner-success" role="status">
          Linked {justLinked.tag}
        </div>
      ) : null}
      {owner && defaultSaved === "saved" ? (
        <div className="status-banner status-banner-success" role="status">
          Default account saved.
        </div>
      ) : owner && defaultSaved === "failed" ? (
        <div className="status-banner status-banner-warning" role="alert">
          The default account could not be saved. Try again.
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
        {owner && data.user && data.user.verifiedPlayers.length > 0 ? (
          <p className="form-help linked-player-hint">
            The Discord bot&apos;s /stats shows your default account when you don&apos;t
            name a player.
          </p>
        ) : null}
        {data.user && data.user.verifiedPlayers.length > 0 ? (
          <ul className="linked-player-cards">
            {data.user.verifiedPlayers.map((player) => (
              <li key={player.tag} className={owner ? "linked-player-owned" : undefined}>
                <LinkedPlayer
                  player={player}
                  backLabel={isOwnProfile ? "Your account" : data.user!.displayName}
                />
                {owner ? (
                  <DefaultAccount
                    tag={player.tag}
                    isDefault={owner.defaultPlayer === player.tag}
                    idempotencyKey={owner.idempotencyKey}
                  />
                ) : null}
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

/** The top-right corner of an owner's card: a Default badge, or the button to make it so. */
function DefaultAccount({
  tag,
  isDefault,
  idempotencyKey,
}: {
  tag: string;
  isDefault: boolean;
  idempotencyKey: string;
}) {
  const id = `linked-player-${tag.slice(1)}`;
  if (isDefault) {
    return (
      <span className="linked-player-default">
        <span aria-hidden="true">✓</span> Default
      </span>
    );
  }
  return (
    <form method="post" className="linked-player-make-default">
      <input type="hidden" name="intent" value="default-player" />
      <input type="hidden" name="tag" value={tag} />
      <input type="hidden" name="idempotencyKey" value={idempotencyKey} />
      <button
        type="submit"
        className="button button-secondary"
        aria-describedby={`${id}-name ${id}-tag`}
      >
        Make default
      </button>
    </form>
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
