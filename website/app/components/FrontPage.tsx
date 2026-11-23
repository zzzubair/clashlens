import { StandingsCells } from "./StandingsCells";
import { Link, useRouteLoaderData, useSearchParams } from "react-router";

import type { RootLoaderData } from "../lib/root-data";
import { UnitArt } from "./ArmyPortraits";

import { DashboardTeaser } from "./DashboardTeaser";
import { ErrorNotice } from "./ErrorNotice";
import { latestObservation } from "./LeaderboardShared";
import { PageSearch } from "./PageSearch";
import {
  comparesWithReset,
  hasRecentDays,
  hasToday,
  RecentDaysHead,
} from "./ResetChange";
import { shortAge } from "../lib/age-format";
import { SavedLine } from "./SavedLine";
import { formatTimestamp, OLD_UPDATE_SECONDS, useServerTime } from "./Provenance";
import { useBackState } from "./BackLink";
import { useSeasonReread } from "./SeasonReread";
import { moveFocusWithArrows } from "./ArrowKeys";
import type {
  TrackedLeaderboard,
  TrackedPlayerEntry,
  WebsiteErrorResponse,
} from "../lib/contracts";

export const FRONT_PAGE_PLAYERS = 10;
const RANKINGS_PATH = "/leaderboards/tracked?view=live&page=1";

export interface FrontPageData {
  loginAvailable: boolean;
  dashboardEnabled: boolean;
  leaderboard: TrackedLeaderboard | null;
  error: WebsiteErrorResponse | null;
}

export function FrontPage({ data }: { data: FrontPageData }) {
  const [searchParams] = useSearchParams();
  const logoutUnrecorded = searchParams.get("logout") === "unrecorded";
  const loggedOut = searchParams.get("saved") === "logout";
  const seasonExpired = useSeasonReread(
    data.leaderboard?.generatedAt,
    !!data.leaderboard?.seasonResetPending,
    true,
    false,
    data.leaderboard?.calendar?.season_end,
  );
  const leaderboard = seasonExpired ? null : data.leaderboard;
  return (
    <main id="main-content" tabIndex={-1} className="page-shell home-page front-page">
      {logoutUnrecorded ? (
        <SavedLine failed>
          You are signed out on this browser, but Clash Lens could not record it.
        </SavedLine>
      ) : loggedOut ? (
        <SavedLine>You&apos;re signed out.</SavedLine>
      ) : null}
      <div
        className={data.dashboardEnabled ? "front-grid front-grid-pitch" : "front-grid"}
      >
        <section className="front-hero" aria-labelledby="front-title">
          <h1 id="front-title">Find any Legend League player</h1>
          <p className="front-lede">
            Every Legend day&apos;s battles and Reset ranks, for any tracked player
          </p>
          <PageSearch />
          <LiveProof leaderboard={leaderboard} />
        </section>
        {data.dashboardEnabled ? (
          <DashboardTeaser loginAvailable={data.loginAvailable} />
        ) : null}
        {data.error ? <ErrorNotice error={data.error} /> : null}
        <TopPlayers leaderboard={leaderboard} seasonExpired={seasonExpired} />
        <MoreFeatures />
      </div>
    </main>
  );
}

function MoreFeatures() {
  const crews = useRouteLoaderData<RootLoaderData>("root")?.crewsEnabled === true;
  return (
    <nav className="front-more" aria-label="More on Clash Lens">
      <div className="front-more-links">
        <Link to="/account/watchlists">
          <UnitArt unitKey="hero:0|hero:1|hero:2" />
          <strong>Watchlists</strong>
          <span>Any players you pick, side by side, day by day</span>
        </Link>
        {crews ? (
          <Link to="/crews">
            <UnitArt unitKey="troop:0|troop:1|troop:3" />
            <strong>Crews</strong>
            <span>Invite-only boards for your Legend group</span>
          </Link>
        ) : null}
        <Link to="/analytics/armies">
          <UnitArt unitKey="troop:110|troop:12|troop:11" />
          <strong>Armies</strong>
          <span>What Legend players attack with, and how it scores</span>
        </Link>
      </div>
    </nav>
  );
}

function LiveProof({ leaderboard }: { leaderboard: TrackedLeaderboard | null }) {
  const now = useServerTime(leaderboard?.generatedAt);
  const observedAt = leaderboard
    ? (leaderboard.sourceObservations?.newestObservedAt ??
      latestObservation(leaderboard.entries))
    : null;
  if (!leaderboard?.totalEntries || !observedAt) return null;
  const age = Math.max(0, Math.floor((now - Date.parse(observedAt)) / 1000));
  return (
    <p className="front-proof">
      {age <= OLD_UPDATE_SECONDS ? (
        <span className="live-pip" aria-hidden="true" />
      ) : null}
      {leaderboard.totalEntries.toLocaleString("en-GB")}{" "}
      <span className="front-proof-wide">Legend </span>players ranked ·{" "}
      <span className="front-proof-age">
        updated{" "}
        <time dateTime={observedAt} title={formatTimestamp(observedAt)}>
          {shortAge(age)} ago
        </time>
      </span>
    </p>
  );
}

function TopPlayers({
  leaderboard,
  seasonExpired,
}: {
  leaderboard: TrackedLeaderboard | null;
  seasonExpired: boolean;
}) {
  const now = useServerTime(leaderboard?.generatedAt);
  const staleEntries =
    leaderboard?.entries.filter(
      (entry) =>
        Math.floor((now - Date.parse(entry.freshness.observedAt)) / 1000) >
        OLD_UPDATE_SECONDS,
    ).length ?? 0;
  const newestAge = Math.min(
    ...(leaderboard?.entries ?? []).map((entry) =>
      Math.floor((now - Date.parse(entry.freshness.observedAt)) / 1000),
    ),
  );
  return (
    <section
      className="data-section home-leaderboard"
      aria-labelledby="live-leaderboard-title"
    >
      <div className="section-heading">
        <div>
          <h2 id="live-leaderboard-title">Top {FRONT_PAGE_PLAYERS} now</h2>
          {leaderboard && staleEntries > 0 ? (
            <p className="standings-context">
              {staleEntries === leaderboard.entries.length
                ? `Updated ${shortAge(newestAge)} ago`
                : `${staleEntries} of ${leaderboard.entries.length} more than 10 minutes old`}
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
  const compared = comparesWithReset(entries);
  const showsDays = hasRecentDays(entries);
  const showsToday = hasToday(entries);
  return (
    <div
      className="table-wrap"
      onKeyDown={(event) => moveFocusWithArrows(event, ".leaderboard-row")}
    >
      <table aria-label="Latest saved standings" className="data-table leaderboard-table">
        <caption className="sr-only">Tracked players in rank order</caption>
        <thead>
          <tr>
            <th scope="col" className="rank-cell">
              Rank
            </th>
            {compared ? (
              <th scope="col" className="move-cell">
                <span className="move-head">Since Reset</span>
              </th>
            ) : null}
            <th scope="col" className="player-cell">
              Player
            </th>
            <th scope="col" className="clan-cell">
              Clan
            </th>
            {showsDays ? <RecentDaysHead entries={entries} /> : null}
            {showsToday ? (
              <th
                scope="col"
                className="today-cell"
                title="Trophies won or lost since the last Reset"
              >
                Today
              </th>
            ) : null}
            <th scope="col" className="trophy-cell">
              Trophies
            </th>
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
              <StandingsCells
                entry={entry}
                compared={compared}
                showsDays={showsDays}
                showsToday={showsToday}
                backState={backState}
              />
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
