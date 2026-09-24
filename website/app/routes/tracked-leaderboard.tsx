import { Link, redirect, useLoaderData, type LoaderFunctionArgs } from "react-router";

import { ErrorNotice } from "../components/ErrorNotice";
import { TrophyMark, latestObservation } from "../components/LeaderboardShared";
import { LocalTimestamp } from "../components/Provenance";
import { canonicalPlayerPath } from "../lib/player-tag";
import type {
  SnapshotSelector,
  TrackedLeaderboard,
  WebsiteErrorResponse,
} from "../lib/contracts";

const PAGE_SIZE = 100;
const formatDate = (value: string) =>
  new Intl.DateTimeFormat("en", { dateStyle: "medium", timeZone: "UTC" }).format(
    new Date(value),
  );

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
  const entries = leaderboard?.entries ?? [];
  const newestObservedAt = leaderboard
    ? (leaderboard.sourceObservations?.newestObservedAt ?? latestObservation(entries))
    : null;
  const oldestObservedAt = leaderboard?.sourceObservations?.oldestObservedAt ?? null;

  return (
    <main id="main-content" tabIndex={-1} className="page-shell rankings-page">
      <section className="rankings-header" aria-labelledby="leaderboard-title">
        <div className="rankings-heading">
          <div>
            <p className="rankings-kicker">Tracked player rankings</p>
            <h1 id="leaderboard-title">
              {daily ? `Day ${daily.dayNumber} standings` : "Latest saved standings"}
            </h1>
          </div>
          <nav aria-label="Leaderboard views" className="leaderboard-view-switch">
            <Link
              className="button secondary"
              aria-current={view === "live" ? "page" : undefined}
              to={leaderboardUrl("live", 1)}
            >
              Latest
            </Link>
            <Link
              className="button secondary"
              aria-current={view === "daily" ? "page" : undefined}
              to="/leaderboards/tracked?view=daily&page=1"
            >
              Daily
            </Link>
          </nav>
        </div>
        {daily ? (
          <p className="rankings-context">
            Legend season {formatDate(daily.seasonStartAt)} –{" "}
            {formatDate(daily.seasonEndAt)} · Day reset{" "}
            <LocalTimestamp value={daily.resetAt} />
          </p>
        ) : leaderboard ? (
          <p className="rankings-context">
            Latest saved player records
            {newestObservedAt ? (
              <>
                , last updated{" "}
                <LocalTimestamp value={newestObservedAt} />
              </>
            ) : null}
            {oldestObservedAt && oldestObservedAt !== newestObservedAt ? (
              <>
                . Updates shown from{" "}
                <LocalTimestamp value={oldestObservedAt} />
              </>
            ) : null}
            .
          </p>
        ) : null}
      </section>

      {error ? <ErrorNotice error={error} /> : null}

      {leaderboard ? (
        <section className="standings-board" aria-labelledby="standings-table-title">
          <div className="standings-toolbar">
            <div>
              <h2 id="standings-table-title">Standings</h2>
              <p>
                {entries.length > 0
                  ? `Ranks ${entries[0].rank}–${entries[entries.length - 1].rank}`
                  : "No ranks on this page"}
                {` · ${leaderboard.totalTracked.toLocaleString()} tracked players`}
              </p>
            </div>
            <span className="page-position">
              Page {leaderboard.page} of {leaderboard.pageCount}
            </span>
          </div>
          <div
            aria-label={`${view === "daily" ? "Daily" : "Latest saved"} leaderboard table`}
            className="table-wrap tracked-leaderboard-viewport"
            role="region"
            tabIndex={0}
          >
            <table
              aria-label={view === "daily" ? "Daily leaderboard" : "Latest saved standings"}
              className="data-table leaderboard-table"
            >
              <caption className="sr-only">
                {view === "daily"
                  ? "Players in the saved daily snapshot"
                  : "Players in the latest saved standings"}
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
                {entries.map((entry) => (
                  <tr
                    className="leaderboard-row"
                    data-podium-rank={entry.rank <= 3 ? entry.rank : undefined}
                    key={entry.tag}
                  >
                    <td className="rank-cell" data-label="Rank">
                      <span className="rank-mark">{entry.rank}</span>
                    </td>
                    <th scope="row" data-label="Player">
                      <Link
                        className="player-name"
                        to={canonicalPlayerPath(entry.tag)}
                        reloadDocument
                      >
                        {entry.name}
                      </Link>
                      <span className="player-tag">{entry.tag}</span>
                    </th>
                    <td data-label="Clan">{entry.clan}</td>
                    <td className="trophy-cell" data-label="Trophies">
                      <TrophyMark />
                      <strong>{entry.trophies.toLocaleString()}</strong>
                    </td>
                    <td data-label="Last updated">
                      <LocalTimestamp value={entry.freshness.observedAt} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <nav aria-label="Leaderboard pages" className="standings-pagination">
            {leaderboard.hasPrevious ? (
              <Link
                className="button button-secondary"
                to={leaderboardUrl(view, leaderboard.page - 1, daily ?? undefined)}
              >
                Previous
              </Link>
            ) : null}
            <span className="pagination-status">
              Page {leaderboard.page} of {leaderboard.pageCount}
            </span>
            {leaderboard.hasNext ? (
              <Link
                className="button button-secondary"
                to={leaderboardUrl(view, leaderboard.page + 1, daily ?? undefined)}
              >
                Next
              </Link>
            ) : null}
          </nav>
          {daily ? (
            <nav aria-label="Daily snapshots" className="snapshot-pagination">
              <span>Saved day snapshots</span>
              <div>
                {daily.previousSnapshot ? (
                  <Link to={leaderboardUrl("daily", 1, daily.previousSnapshot)}>Older</Link>
                ) : null}
                {daily.nextSnapshot ? (
                  <Link to={leaderboardUrl("daily", 1, daily.nextSnapshot)}>Newer</Link>
                ) : null}
              </div>
            </nav>
          ) : null}
        </section>
      ) : (
        <div className="empty-state">
          <h2>Leaderboard unavailable</h2>
          <p>We couldn’t load the standings. Please try again in a moment.</p>
        </div>
      )}
    </main>
  );
}
