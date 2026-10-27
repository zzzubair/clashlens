import { Link, useSearchParams } from "react-router";

import { DashboardTeaser } from "./DashboardTeaser";
import { ErrorNotice } from "./ErrorNotice";
import { TrophyMark, latestObservation } from "./LeaderboardShared";
import { PageSearch } from "./PageSearch";
import { SavedLine } from "./SavedLine";
import { LocalTimestamp, OLD_UPDATE_SECONDS, useServerTime } from "./Provenance";
import { useBackState } from "./BackLink";
import { useSeasonReread } from "./SeasonReread";
import { moveFocusWithArrows } from "./ArrowKeys";
import type {
  TrackedLeaderboard,
  TrackedPlayerEntry,
  WebsiteErrorResponse,
} from "../lib/contracts";
import { canonicalPlayerPath } from "../lib/player-tag";

export const FRONT_PAGE_PLAYERS = 10;
const RANKINGS_PATH = "/leaderboards/tracked?view=live&page=1";

export interface FrontPageData {
  loginAvailable: boolean;
  /** Off, the page leaves out what an account adds. */
  dashboardEnabled: boolean;
  leaderboard: TrackedLeaderboard | null;
  error: WebsiteErrorResponse | null;
}

/**
 * What `/` shows anyone not signed in, and search engines: search first,
 * then a peek at the dashboard an account unlocks, then the top players.
 */
export function FrontPage({ data }: { data: FrontPageData }) {
  // Set by /logout: logged out, or logged out here but not recorded.
  const [searchParams] = useSearchParams();
  const logoutUnrecorded = searchParams.get("logout") === "unrecorded";
  const loggedOut = searchParams.get("saved") === "logout";
  return (
    <main id="main-content" tabIndex={-1} className="page-shell home-page front-page">
      {logoutUnrecorded ? (
        <SavedLine failed>
          You are logged out on this browser, but Clash Lens could not record it.
        </SavedLine>
      ) : loggedOut ? (
        <SavedLine>You&apos;re logged out.</SavedLine>
      ) : null}
      <div
        className={data.dashboardEnabled ? "front-grid front-grid-pitch" : "front-grid"}
      >
        <section className="front-hero" aria-labelledby="front-title">
          <h1 id="front-title">Find any Legend League player</h1>
          <PageSearch />
        </section>
        {data.dashboardEnabled ? (
          <DashboardTeaser loginAvailable={data.loginAvailable} />
        ) : null}
        {data.error ? <ErrorNotice error={data.error} /> : null}
        <TopPlayers leaderboard={data.leaderboard} />
      </div>
    </main>
  );
}

function TopPlayers({ leaderboard: loaded }: { leaderboard: TrackedLeaderboard | null }) {
  const seasonExpired = useSeasonReread(
    loaded?.generatedAt,
    !!loaded?.seasonResetPending,
  );
  const leaderboard = seasonExpired ? null : loaded;
  const latestObservedAt = leaderboard ? latestObservation(leaderboard.entries) : null;
  const now = useServerTime(leaderboard?.generatedAt);
  const staleEntries =
    leaderboard?.entries.filter(
      (entry) =>
        Math.floor((now - Date.parse(entry.freshness.observedAt)) / 1000) >
        OLD_UPDATE_SECONDS,
    ).length ?? 0;
  return (
    <section
      className="data-section home-leaderboard"
      aria-labelledby="live-leaderboard-title"
    >
      <div className="section-heading">
        <div>
          <h2 id="live-leaderboard-title">Top {FRONT_PAGE_PLAYERS} now</h2>
          {leaderboard ? (
            <p className="standings-context">
              {latestObservedAt ? (
                <span>
                  Updated <LocalTimestamp value={latestObservedAt} />
                </span>
              ) : null}
              {staleEntries > 0 ? (
                <span>
                  {staleEntries} of {leaderboard.entries.length} more than 10 minutes old
                </span>
              ) : null}
            </p>
          ) : null}
        </div>
        <Link className="section-link leaderboard-more" to={RANKINGS_PATH}>
          All rankings
        </Link>
      </div>
      {seasonExpired ? (
        <p className="empty-state" role="status">
          Loading the new Season's rankings…
        </p>
      ) : leaderboard && leaderboard.entries.length > 0 ? (
        <LeaderboardTable entries={leaderboard.entries} />
      ) : leaderboard?.seasonResetPending ? (
        <div className="empty-state">
          <h3>Waiting for the new Season</h3>
          <p className="rankings-context" role="status">
            {leaderboard.seasonResetPending.toLocaleString()} tracked{" "}
            {leaderboard.seasonResetPending === 1 ? "player is" : "players are"} waiting
            for their Season reset and will be ranked once their profile shows the new
            Season.
          </p>
        </div>
      ) : leaderboard ? (
        <div className="empty-state">
          <h3>No standings available yet</h3>
          <p>Check back after player updates have been saved.</p>
        </div>
      ) : (
        <div className="empty-state">
          <h3>Tracked player data is unavailable</h3>
          <p>The saved leaderboard could not be loaded. Try again later.</p>
        </div>
      )}
    </section>
  );
}

function LeaderboardTable({ entries }: { entries: TrackedPlayerEntry[] }) {
  const backState = useBackState("Clash Lens");
  return (
    <div
      className="table-wrap"
      onKeyDown={(event) => moveFocusWithArrows(event, ".leaderboard-row")}
    >
      <table aria-label="Latest saved standings" className="data-table leaderboard-table">
        <caption className="sr-only">Tracked players in rank order</caption>
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
              key={entry.tag}
              data-testid="tracked-player-row"
              data-podium-rank={entry.rank <= 3 ? entry.rank : undefined}
            >
              <td className="rank-cell" data-label="Rank">
                <span className="rank-mark">{entry.rank}</span>
              </td>
              <th scope="row" data-label="Player">
                <Link
                  className="player-name"
                  to={canonicalPlayerPath(entry.tag)}
                  state={backState}
                >
                  <bdi>{entry.name}</bdi>
                </Link>
                <span className="player-tag">{entry.tag}</span>
              </th>
              <td data-label="Clan">
                <bdi>{entry.clan}</bdi>
              </td>
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
  );
}
