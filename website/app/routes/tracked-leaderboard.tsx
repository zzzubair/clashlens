import { Link, redirect, useLoaderData, type LoaderFunctionArgs } from "react-router";

import { ErrorNotice } from "../components/ErrorNotice";
import { formatTimestamp } from "../components/Provenance";
import { canonicalPlayerPath } from "../lib/player-tag";
import type {
  SnapshotSelector,
  TrackedLeaderboard,
  WebsiteErrorResponse,
} from "../lib/contracts";

const PAGE_SIZE = 100;

function leaderboardUrl(
  view: "live" | "daily",
  page: number,
  selector?: SnapshotSelector,
) {
  const query = new URLSearchParams({ view });
  if (view === "daily" && selector) {
    query.set("season", selector.officialSeasonId);
    query.set("day", String(selector.dayNumber));
  }
  query.set("page", String(page));
  return `/leaderboards/tracked?${query.toString()}`;
}

export async function loader({ request }: LoaderFunctionArgs): Promise<{
  leaderboard: TrackedLeaderboard | null;
  error: WebsiteErrorResponse | null;
}> {
  const url = new URL(request.url);
  const viewValue = url.searchParams.get("view");
  if (viewValue === null) throw redirect(leaderboardUrl("live", 1));
  if (viewValue !== "live" && viewValue !== "daily")
    throw new Response(null, { status: 422 });
  const season = url.searchParams.get("season");
  const dayValue = url.searchParams.get("day");
  if (viewValue === "live" && (season !== null || dayValue !== null))
    throw new Response(null, { status: 422 });
  if (viewValue === "daily" && (season === null) !== (dayValue === null))
    throw new Response(null, { status: 422 });
  let selector: SnapshotSelector | undefined;
  if (season !== null && dayValue !== null) {
    if (!season || !/^(?:[1-9]|1[0-9]|2[0-8])$/.test(dayValue))
      throw new Response(null, { status: 422 });
    selector = { officialSeasonId: season, dayNumber: Number(dayValue) };
  }
  const pageValue = url.searchParams.get("page");
  if (pageValue === null) throw redirect(leaderboardUrl(viewValue, 1, selector));
  if (!/^[1-9][0-9]*$/.test(pageValue)) throw new Response(null, { status: 422 });
  const page = Number(pageValue);
  if (!Number.isSafeInteger(page) || !Number.isSafeInteger((page - 1) * PAGE_SIZE))
    throw new Response(null, { status: 422 });
  try {
    const { createPythonClient } = await import("../services/python.server");
    const leaderboard = await createPythonClient().getTrackedLeaderboard(
      PAGE_SIZE,
      viewValue,
      (page - 1) * PAGE_SIZE,
      selector,
    );
    if (viewValue === "daily" && !selector && leaderboard.daily)
      throw redirect(leaderboardUrl("daily", page, leaderboard.daily));
    return { leaderboard, error: null };
  } catch (cause) {
    const { PythonApiError } = await import("../services/python.server");
    if (cause instanceof PythonApiError && (cause.status === 404 || cause.status === 422))
      throw new Response(null, { status: cause.status });
    if (cause instanceof Response) throw cause;
    const { safeWebsiteError } = await import("../server/errors.server");
    return { leaderboard: null, error: safeWebsiteError(cause) };
  }
}

export function headers() {
  return { "Cache-Control": "no-store" };
}

export default function TrackedLeaderboardRoute() {
  const { leaderboard, error } = useLoaderData<typeof loader>();
  const view = leaderboard?.view ?? "live";
  const daily = leaderboard?.daily;
  return (
    <main className="page-shell">
      <section className="hero" aria-labelledby="leaderboard-title">
        <h1 id="leaderboard-title">
          {daily ? `Daily leaderboard · Day ${daily.dayNumber}` : "Live leaderboard"}
        </h1>
        {daily ? (
          <p>
            Legend season · reset{" "}
            <time dateTime={daily.resetAt}>{formatTimestamp(daily.resetAt)}</time>
          </p>
        ) : null}
        <nav aria-label="Leaderboard views" className="hero-actions">
          <Link className="button secondary" to={leaderboardUrl("live", 1)}>
            Live
          </Link>
          <Link className="button secondary" to="/leaderboards/tracked?view=daily&page=1">
            Daily
          </Link>
        </nav>
      </section>
      {error ? <ErrorNotice error={error} /> : null}
      {leaderboard ? (
        <section className="data-section" aria-label="Leaderboard entries">
          <div
            aria-label={`${view === "daily" ? "Daily" : "Live"} leaderboard table`}
            className="table-wrap tracked-leaderboard-viewport"
            role="region"
            tabIndex={0}
          >
            <table
              aria-label={view === "daily" ? "Daily leaderboard" : "Live leaderboard"}
              className="data-table"
            >
              <caption className="sr-only">Players on the {view} leaderboard</caption>
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
                {leaderboard.entries.map((entry) => (
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
          <nav aria-label="Leaderboard pages" className="hero-actions">
            {leaderboard.hasPrevious ? (
              <Link to={leaderboardUrl(view, leaderboard.page - 1, daily ?? undefined)}>
                Previous
              </Link>
            ) : null}
            <span>
              Page {leaderboard.page} of {leaderboard.pageCount}
            </span>
            {leaderboard.hasNext ? (
              <Link to={leaderboardUrl(view, leaderboard.page + 1, daily ?? undefined)}>
                Next
              </Link>
            ) : null}
          </nav>
          {daily ? (
            <nav aria-label="Daily snapshots" className="hero-actions">
              {daily.previousSnapshot ? (
                <Link to={leaderboardUrl("daily", 1, daily.previousSnapshot)}>Older</Link>
              ) : null}
              {daily.nextSnapshot ? (
                <Link to={leaderboardUrl("daily", 1, daily.nextSnapshot)}>Newer</Link>
              ) : null}
            </nav>
          ) : null}
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
