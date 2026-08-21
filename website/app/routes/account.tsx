import { Link, redirect, useLoaderData } from "react-router";

import { ErrorNotice } from "../components/ErrorNotice";
import type { AccountSummary, PrivateGroup, SavedPlayer } from "../lib/account-contracts";
import type { WebsiteErrorResponse } from "../lib/contracts";
import { canonicalPlayerPath } from "../lib/player-tag";
import type { Route } from "./+types/account";

const NO_STORE = { "Cache-Control": "no-store" };

export interface AccountLoaderData {
  summary: AccountSummary | null;
  savedPlayers: SavedPlayer[] | null;
  groups: PrivateGroup[] | null;
  error: WebsiteErrorResponse | null;
}

/**
 * GET /account — the signed-in account overview: profile, verified players,
 * saved players, and private groups, with links to the management pages. The
 * provider identity signs the private Python calls and never appears here.
 */
export async function loader({ request }: Route.LoaderArgs): Promise<AccountLoaderData> {
  const { requireLogin } = await import("../server/auth-guard.server");
  const identity = await requireLogin(request);

  let summary: AccountSummary | null = null;
  let savedPlayers: SavedPlayer[] | null = null;
  let groups: PrivateGroup[] | null = null;
  let error: WebsiteErrorResponse | null = null;
  try {
    const { createPythonClient } = await import("../services/python.server");
    const client = createPythonClient(identity);
    const results = await Promise.allSettled([
      client.getAccountSummary(),
      client.listSavedTags(),
      client.listGroups(),
    ]);
    const { isAccountNotFoundError } = await import("../server/actions.server");
    if (
      results.some(
        (result) => result.status === "rejected" && isAccountNotFoundError(result.reason),
      )
    ) {
      throw redirect("/account/setup");
    }
    if (results[0].status === "fulfilled") summary = results[0].value;
    else error = error ?? (await safeError(results[0].reason));
    if (results[1].status === "fulfilled") savedPlayers = results[1].value;
    else error = error ?? (await safeError(results[1].reason));
    if (results[2].status === "fulfilled") groups = results[2].value;
    else error = error ?? (await safeError(results[2].reason));
  } catch (cause) {
    if (cause instanceof Response) throw cause;
    error = error ?? (await safeError(cause));
  }
  return { summary, savedPlayers, groups, error };
}

async function safeError(cause: unknown): Promise<WebsiteErrorResponse> {
  const { safeWebsiteError } = await import("../server/errors.server");
  return safeWebsiteError(cause);
}

export function headers() {
  return NO_STORE;
}

export default function AccountRoute() {
  const data = useLoaderData<typeof loader>();
  return (
    <main className="page-shell">
      <section className="hero" aria-labelledby="account-title">
        <h1 id="account-title">Your account</h1>
        <p className="lede">
          {data.summary ? (
            <>
              Signed in as <strong>{data.summary.displayName}</strong>{" "}
              <span className="player-tag">@{data.summary.username}</span>
            </>
          ) : (
            "Your account details could not be loaded."
          )}
        </p>
        <p className="hero-actions">
          <Link className="button button-secondary" to="/account/profile">
            Edit profile
          </Link>
        </p>
      </section>

      {data.error ? <ErrorNotice error={data.error} /> : null}

      <section className="data-section" aria-labelledby="verified-title">
        <div className="section-heading">
          <h2 id="verified-title">Verified players</h2>
          <Link className="button button-secondary" to="/account/verify-player">
            Verify a player
          </Link>
        </div>
        {data.summary && data.summary.verifiedPlayers.length > 0 ? (
          <ul className="player-link-list">
            {data.summary.verifiedPlayers.map((player) => (
              <li key={player.tag}>
                <Link to={canonicalPlayerPath(player.tag)} reloadDocument>
                  {player.name ?? player.tag}
                </Link>
                <span className="player-tag">{player.tag}</span>
              </li>
            ))}
          </ul>
        ) : (
          <div className="empty-state">
            <h3>No verified players yet</h3>
            <p>Verify a player you own with the one-time token from Clash of Clans.</p>
          </div>
        )}
      </section>

      <section className="data-section" aria-labelledby="saved-title">
        <div className="section-heading">
          <h2 id="saved-title">Saved players</h2>
          <Link className="button button-secondary" to="/account/saved-players">
            Manage saved players
          </Link>
        </div>
        {data.savedPlayers && data.savedPlayers.length > 0 ? (
          <ul className="player-link-list">
            {data.savedPlayers.map((player) => (
              <li key={player.tag}>
                <Link to={canonicalPlayerPath(player.tag)} reloadDocument>
                  {player.name ?? player.tag}
                </Link>
                <span className="player-tag">{player.tag}</span>
              </li>
            ))}
          </ul>
        ) : (
          <div className="empty-state">
            <h3>No saved players</h3>
            <p>Save public player tags to reach them quickly later.</p>
          </div>
        )}
      </section>

      <section className="data-section" aria-labelledby="groups-title">
        <div className="section-heading">
          <h2 id="groups-title">Private groups</h2>
          <Link className="button button-secondary" to="/account/groups">
            Manage groups
          </Link>
        </div>
        {data.groups && data.groups.length > 0 ? (
          <ul className="group-name-list">
            {data.groups.map((group) => (
              <li key={group.groupId}>{group.name}</li>
            ))}
          </ul>
        ) : (
          <div className="empty-state">
            <h3>No private groups</h3>
            <p>Groups are visible only to you.</p>
          </div>
        )}
      </section>
    </main>
  );
}
