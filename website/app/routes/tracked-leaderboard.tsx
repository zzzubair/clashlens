import { Link, useLoaderData, type LoaderFunctionArgs } from "react-router";

import { ErrorNotice } from "../components/ErrorNotice";
import { formatTimestamp } from "../components/Provenance";
import { canonicalPlayerPath } from "../lib/player-tag";
import type { TrackedLeaderboard, WebsiteErrorResponse } from "../lib/contracts";

export async function loader({ request }: LoaderFunctionArgs): Promise<{
  leaderboard: TrackedLeaderboard | null;
  error: WebsiteErrorResponse | null;
}> {
  const view =
    new URL(request.url).searchParams.get("view") === "daily" ? "daily" : "live";
  try {
    const { createPythonClient } = await import("../services/python.server");
    return {
      leaderboard: await createPythonClient().getTrackedLeaderboard(30, view),
      error: null,
    };
  } catch (cause) {
    const { safeWebsiteError } = await import("../server/errors.server");
    return { leaderboard: null, error: safeWebsiteError(cause) };
  }
}

export function headers() {
  return { "Cache-Control": "no-store" };
}

export default function TrackedLeaderboardRoute() {
  const data = useLoaderData<typeof loader>();
  return (
    <main className="page-shell">
      <section className="hero" aria-labelledby="leaderboard-title">
        <h1 id="leaderboard-title">
          {data.leaderboard?.view === "daily" ? "Daily leaderboard" : "Live leaderboard"}
        </h1>
        <nav aria-label="Leaderboard views" className="hero-actions">
          <Link className="button secondary" to="/leaderboards/tracked?view=live">
            Live
          </Link>
          <Link className="button secondary" to="/leaderboards/tracked?view=daily">
            Daily
          </Link>
        </nav>
      </section>
      {data.error ? <ErrorNotice error={data.error} /> : null}
      {data.leaderboard ? (
        <section className="data-section" aria-label="Leaderboard entries">
          <div className="table-wrap">
            <table
              aria-label={
                data.leaderboard.view === "daily"
                  ? "Daily leaderboard"
                  : "Live leaderboard"
              }
              className="data-table responsive-table"
            >
              <caption className="sr-only">
                {data.leaderboard.view === "daily"
                  ? "Players on the daily leaderboard"
                  : "Players on the live leaderboard"}
              </caption>
              <thead>
                <tr>
                  <th scope="col">Rank</th>
                  <th scope="col">Player</th>
                  <th scope="col">Clan</th>
                  <th scope="col">Trophies</th>
                  <th scope="col">Last updated</th>
                </tr>
              </thead>
              <tbody>
                {data.leaderboard.entries.map((entry) => (
                  <tr key={entry.tag}>
                    <td data-label="Rank">{entry.rank}</td>
                    <th scope="row" data-label="Player">
                      <Link
                        className="player-name"
                        to={canonicalPlayerPath(entry.tag)}
                        reloadDocument
                      >
                        {entry.name}
                      </Link>
                      <span className="player-tag">{entry.tag}</span>
                      <Link
                        className="view-player-link"
                        to={canonicalPlayerPath(entry.tag)}
                        reloadDocument
                      >
                        View player →
                      </Link>
                    </th>
                    <td data-label="Clan">{entry.clan}</td>
                    <td data-label="Trophies">{entry.trophies.toLocaleString()}</td>
                    <td data-label="Last updated">
                      <time dateTime={entry.freshness.observedAt}>
                        {formatTimestamp(entry.freshness.observedAt)}
                      </time>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : (
        <div className="empty-state">
          <h2>Leaderboard unavailable</h2>
          <p>Saved data remains protected while the live service is unavailable.</p>
        </div>
      )}
    </main>
  );
}
