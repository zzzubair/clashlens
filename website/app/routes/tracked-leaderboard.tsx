import { Link, useLoaderData, type LoaderFunctionArgs } from "react-router";

import { ErrorNotice } from "../components/ErrorNotice";
import { CoverageSummary } from "../components/CoverageSummary";
import { FreshnessText, Provenance } from "../components/Provenance";
import { StateBadge } from "../components/StateBadge";
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
      <Link className="back-link" to="/">
        ← Back to home
      </Link>
      <section className="hero" aria-labelledby="leaderboard-title">
        <p className="eyebrow">
          {data.leaderboard?.view === "daily" ? "Daily snapshot" : "Live view"}
        </p>
        <h1 id="leaderboard-title">
          {data.leaderboard?.view === "daily"
            ? "Daily tracked snapshot"
            : "Full tracked leaderboard"}
        </h1>
        <p className="lede">
          This is the actively tracked Legend I cohort. It is not the official global rank
          and it does not claim complete population coverage.
        </p>
      </section>
      {data.error ? <ErrorNotice error={data.error} /> : null}
      {data.leaderboard ? (
        <section className="data-section" aria-labelledby="full-list-title">
          <div className="section-heading">
            <h2 id="full-list-title">
              {data.leaderboard.view === "daily"
                ? "Daily tracked players"
                : "Live tracked players"}
            </h2>
            <nav aria-label="Leaderboard views" className="hero-actions">
              <Link className="button secondary" to="/leaderboards/tracked?view=live">
                Live view
              </Link>
              <Link className="button secondary" to="/leaderboards/tracked?view=daily">
                Daily snapshot
              </Link>
            </nav>
          </div>
          <Provenance provenance={data.leaderboard.provenance} />
          <CoverageSummary coverage={data.leaderboard.coverage} />
          <div className="table-wrap">
            <table className="data-table">
              <caption className="sr-only">Full live tracked player list</caption>
              <thead>
                <tr>
                  <th scope="col">Position</th>
                  <th scope="col">Official rank</th>
                  <th scope="col">Player</th>
                  <th scope="col">Clan</th>
                  <th scope="col">Trophies</th>
                  <th scope="col">Observation</th>
                  <th scope="col">State</th>
                </tr>
              </thead>
              <tbody>
                {data.leaderboard.entries.map((entry) => (
                  <tr key={entry.tag}>
                    <td>{entry.rank}</td>
                    <td>{entry.officialRank ?? "Not supplied"}</td>
                    <th scope="row">
                      <Link className="player-name" to={canonicalPlayerPath(entry.tag)}>
                        {entry.name}
                      </Link>
                      <span className="player-tag">{entry.tag}</span>
                    </th>
                    <td>{entry.clan}</td>
                    <td>{entry.trophies.toLocaleString()}</td>
                    <td>
                      <FreshnessText freshness={entry.freshness} />
                    </td>
                    <td>
                      <StateBadge state={entry.state} />
                      <small className="table-detail">
                        Confidence: {entry.confidence}
                      </small>
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
