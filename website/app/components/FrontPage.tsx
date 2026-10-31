import { Link, useRouteLoaderData, useSearchParams } from "react-router";

import type { RootLoaderData } from "../root";
import { UnitArt } from "./ArmyPortraits";

import { DashboardTeaser } from "./DashboardTeaser";
import { ErrorNotice } from "./ErrorNotice";
import { TrophyMark, latestObservation } from "./LeaderboardShared";
import { PageSearch } from "./PageSearch";
import {
  comparesWithReset,
  hasRecentDays,
  hasToday,
  RankMove,
  RecentDays,
  RecentDaysHead,
  shortAge,
  TodayChange,
} from "./ResetChange";
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
 * then what an account adds with an example dashboard, then the top players.
 */
export function FrontPage({ data }: { data: FrontPageData }) {
  // Set by /logout: signed out, or signed out here but not recorded.
  const [searchParams] = useSearchParams();
  const logoutUnrecorded = searchParams.get("logout") === "unrecorded";
  const loggedOut = searchParams.get("saved") === "logout";
  const seasonExpired = useSeasonReread(
    data.leaderboard?.generatedAt,
    !!data.leaderboard?.seasonResetPending,
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
            Every Legend day&apos;s battles and Reset ranks, for any tracked player.
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

/** Three more things Clash Lens does, each led by three game portraits in
 *  the same tiles, a line apiece. Watchlists and Crews ask a visitor to sign
 *  in, and say why. */
function MoreFeatures() {
  const crews = useRouteLoaderData<RootLoaderData>("root")?.crewsEnabled === true;
  return (
    <nav className="front-more" aria-label="More on Clash Lens">
      <div className="front-more-links">
        <Link to="/account/watchlists">
          <UnitArt unitKey="hero:0|hero:1|hero:2" />
          <strong>Watchlists</strong>
          <span>Any players you pick, side by side, day by day.</span>
        </Link>
        {crews ? (
          <Link to="/crews">
            <UnitArt unitKey="troop:0|troop:1|troop:3" />
            <strong>Crews</strong>
            <span>Invite-only boards for your Legend group.</span>
          </Link>
        ) : null}
        <Link to="/analytics/armies">
          <UnitArt unitKey="troop:110|troop:12|troop:11" />
          <strong>Armies</strong>
          <span>What Legend players attack with, and how it scores.</span>
        </Link>
      </div>
    </nav>
  );
}

/** Proof the board is live, under the search: how many players it ranks
 *  and how long ago it last changed, the page's one update time. */
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
      {/* Wraps as one piece, never leaving "ago" alone on a line. */}
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
  return (
    <section
      className="data-section home-leaderboard"
      aria-labelledby="live-leaderboard-title"
    >
      <div className="section-heading">
        <div>
          <h2 id="live-leaderboard-title">Top {FRONT_PAGE_PLAYERS} now</h2>
          {/* The update time is the hero's; a note only when rows lag. */}
          {leaderboard && staleEntries > 0 ? (
            <p className="standings-context">
              {staleEntries} of {leaderboard.entries.length} more than 10 minutes old
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

/** The website reads a profile without a clan as "Unknown"; it shows as no
 *  clan. */
function clanName(clan: string) {
  return clan === "Unknown" ? (
    <span className="no-clan">No clan</span>
  ) : (
    <bdi>{clan}</bdi>
  );
}

// The Top 10 as one list in Rankings' row style, under the same headings:
// plate, places moved since Reset, name, clan (under the name on a phone),
// the last 7 days, trophies since Reset and trophies.
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
              <td className="rank-cell" data-label="Rank">
                <span className="rank-mark">{entry.rank}</span>
              </td>
              {compared ? (
                <td className="move-cell">
                  <RankMove entry={entry} />
                </td>
              ) : null}
              <th scope="row" className="player-cell" data-label="Player">
                <span className="player-id">
                  <Link
                    className="player-name"
                    to={canonicalPlayerPath(entry.tag)}
                    state={backState}
                  >
                    <bdi>{entry.name}</bdi>
                  </Link>
                </span>
                {/* A phone puts the clan on a smaller line under the name, as
                    on Rankings; wider screens give it the Clan column. */}
                <span className="player-sub">
                  <span className="player-clan">{clanName(entry.clan)}</span>
                </span>
                {/* Left to screen readers, to tell two players of one name apart. */}
                <span className="sr-only"> {entry.tag}</span>
              </th>
              <td className="clan-cell">{clanName(entry.clan)}</td>
              {showsDays ? (
                <td className="recent-cell">
                  <RecentDays entry={entry} />
                </td>
              ) : null}
              {showsToday ? (
                <td className="today-cell">
                  <TodayChange entry={entry} />
                </td>
              ) : null}
              <td className="trophy-cell" data-label="Trophies">
                <TrophyMark />
                <strong>{entry.trophies.toLocaleString()}</strong>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
