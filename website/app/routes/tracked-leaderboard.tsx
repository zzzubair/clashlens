import { useEffect, useRef } from "react";
import {
  data,
  Form,
  Link,
  redirect,
  useLoaderData,
  useLocation,
  useNavigate,
  useNavigation,
  type LoaderFunctionArgs,
} from "react-router";

import { moveFocusWithArrows } from "../components/ArrowKeys";
import { useBackState } from "../components/BackLink";
import { DashboardIcon } from "../components/DashboardIcon";
import { ErrorNotice } from "../components/ErrorNotice";
import { SearchIcon } from "../components/PlayerSearch";
import { useSeasonReread } from "../components/SeasonReread";
import { RANKINGS_PAGE_SIZE, TrophyMark } from "../components/LeaderboardShared";
import {
  comparesWithReset,
  hasRecentDays,
  hasToday,
  RankMove,
  RecentDays,
  RecentDaysHead,
  shortAge,
  TodayChange,
} from "../components/ResetChange";
import {
  formatAge,
  formatTimestamp,
  LocalTimestamp,
  OLD_UPDATE_SECONDS,
  useCurrentTime,
} from "../components/Provenance";
import { publicPageMeta } from "../lib/blog";
import { canonicalPlayerPath, noMatchHint, normalizePlayerTag } from "../lib/player-tag";
import { MAX_SEARCH_QUERY_LENGTH } from "../lib/validation";
import type { SnapshotSelector, WebsiteErrorResponse } from "../lib/contracts";
import type { Route } from "./+types/tracked-leaderboard";
import "../leaderboard-search.css";

const PAGE_SIZE = RANKINGS_PAGE_SIZE;
// A saved Daily board is incomplete when none of its players was saved in the
// 30 minutes before Reset. Collection pauses at 04:55, so normal boards end about
// 5 minutes before Reset; the Oct 3, 2026 outage board ended 5 hours before.
const INCOMPLETE_INPUT_GAP_SECONDS = 30 * 60;
// Each board explains the tie order it was saved with: Daily boards since v3
// by the latest trophy change, which matched the game's Reset order more
// often; the Live board and older v2 Daily boards by attack destruction; older
// boards by player tags alone.
function rankExplanation(orderVersion: string | undefined) {
  const tieOrder =
    orderVersion === "tracked-player-order-v3"
      ? "go to the player whose trophies changed most recently that Legend day, then a fixed order based on player tags"
      : orderVersion === "tracked-player-order-v2" ||
          orderVersion === "tracked-trophies-attack-destruction-v2"
        ? "go to the higher average attack destruction this Season, then more attacks, then a fixed order based on player tags"
        : "follow a fixed order based on player tags";
  return `Rank among players tracked by Clash Lens, not the official global rank. Equal trophies are marked Tie and ${tieOrder}.`;
}

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

function playerRankUrl(tag: string, rank: number) {
  return `${leaderboardUrl("live", Math.ceil(rank / PAGE_SIZE))}&player=${encodeURIComponent(tag)}`;
}

// Each board page is its own address; a search or a picked player is not.
export function meta({ location, matches }: Route.MetaArgs) {
  const query = new URLSearchParams(location.search);
  const season = query.get("season");
  const day = query.get("day");
  return publicPageMeta(matches, {
    path: leaderboardUrl(
      query.get("view") === "daily" ? "daily" : "live",
      Number(query.get("page")) || 1,
      season && day ? { officialSeasonId: season, dayNumber: Number(day) } : undefined,
    ),
    title: "Legend League rankings",
    description:
      "Live and daily trophy rankings of the Legend I players Clash Lens tracks.",
  });
}

/**
 * The signed-in Clasher's own place on the Live board, for Jump to me: the
 * linked player their dashboard leads with, the first one being tracked, or
 * else the first. Anyone signed out, or a player off the board, gets none,
 * and a failed read never stops the board.
 */
async function myPlace(request: Request): Promise<{ tag: string; rank: number } | null> {
  try {
    const { getWebsiteConfig } = await import("../server/config.server");
    const { readLoginIdentity } = await import("../server/actions.server");
    const identity = await readLoginIdentity(request, getWebsiteConfig());
    if (identity === null) return null;
    const { createPythonClient } = await import("../services/python.server");
    const client = createPythonClient(identity);
    const { username } = await client.getAccount();
    const players = (await client.getPublicUser(username)).verifiedPlayers;
    const lead = players.find((player) => player.state === "tracking") ?? players[0];
    return lead?.rank ? { tag: lead.tag, rank: lead.rank } : null;
  } catch {
    return null;
  }
}

export async function loader({ request }: LoaderFunctionArgs) {
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
  // Go to rank: the page holding that rank, with its row picked.
  const rankValue = url.searchParams.get("rank");
  const focusRank = rankValue === null ? null : Number(rankValue);
  if (rankValue !== null) {
    if (!/^[1-9][0-9]{0,8}$/.test(rankValue)) throw new Response(null, { status: 422 });
    const rankPage = Math.ceil(Number(rankValue) / PAGE_SIZE);
    if (url.searchParams.get("page") !== String(rankPage))
      throw redirect(
        `${leaderboardUrl(viewValue, rankPage, selector)}&rank=${rankValue}`,
      );
  }
  const pageValue = url.searchParams.get("page");
  if (pageValue === null) throw redirect(leaderboardUrl(viewValue, 1, selector));
  if (!/^[1-9][0-9]*$/.test(pageValue)) throw new Response(null, { status: 422 });
  const page = Number(pageValue);
  if (!Number.isSafeInteger(page) || !Number.isSafeInteger((page - 1) * PAGE_SIZE))
    throw new Response(null, { status: 422 });
  const query = (url.searchParams.get("q") ?? "").trim();
  // The Live search also goes to a rank: a plain number such as 130 or 1,250
  // is a rank; "#130" stays a player tag.
  const typedRank = query.replace(/[,\s]/g, "");
  if (viewValue === "live" && /^[1-9][0-9]{0,8}$/.test(typedRank))
    throw redirect(
      `${leaderboardUrl("live", Math.ceil(Number(typedRank) / PAGE_SIZE))}&rank=${typedRank}`,
    );
  const player = url.searchParams.get("player");
  const focusTag = player === null ? null : normalizePlayerTag(player);
  if (
    query.length > MAX_SEARCH_QUERY_LENGTH ||
    (player !== null && !focusTag) ||
    (viewValue === "daily" && (query || player !== null)) ||
    (focusRank !== null && (query || player !== null))
  )
    throw new Response(null, { status: 422 });
  const me = viewValue === "live" ? myPlace(request) : Promise.resolve(null);
  try {
    const { createPythonClient } = await import("../services/python.server");
    const client = createPythonClient();
    const search = query ? await client.searchLeaderboard(query) : null;
    if (search?.exactTag)
      throw redirect(playerRankUrl(search.exactTag, search.results[0].rank));
    const leaderboard = await client.getTrackedLeaderboard(
      PAGE_SIZE,
      viewValue,
      (page - 1) * PAGE_SIZE,
      selector,
      focusTag ?? undefined,
    );
    return {
      leaderboard,
      error: null,
      pageUnavailableUrl: null,
      // The newest Daily board, read without a Season and day: the page shows
      // it at once and then names its Season and day in the address.
      latestDailyUrl:
        viewValue === "daily" && !selector && leaderboard.daily
          ? leaderboardUrl("daily", page, leaderboard.daily)
          : null,
      view: viewValue,
      query,
      search,
      focusTag,
      focusRank,
      me: await me,
    } as const;
  } catch (cause) {
    const { PythonApiError } = await import("../services/python.server");
    if (focusTag && cause instanceof PythonApiError && cause.status === 404) {
      const { createPythonClient } = await import("../services/python.server");
      const missing: WebsiteErrorResponse = {
        error: {
          code: "missing",
          message:
            "That player is no longer on the Live Leaderboard. Search again to find another player.",
        },
      };
      return {
        leaderboard: await createPythonClient().getTrackedLeaderboard(PAGE_SIZE, "live"),
        error: missing,
        pageUnavailableUrl: null,
        latestDailyUrl: null,
        view: viewValue,
        query,
        search: null,
        focusTag: null,
        focusRank: null,
        me: await me,
      } as const;
    }
    if (cause instanceof PythonApiError && cause.status === 404 && page > 1)
      return data(
        {
          leaderboard: null,
          error: null,
          pageUnavailableUrl: leaderboardUrl(viewValue, 1, selector),
          latestDailyUrl: null,
          view: viewValue,
          query,
          search: null,
          focusTag: null,
          // A rank asked for past the board's end says so.
          focusRank,
          me: null,
        } as const,
        { status: 404 },
      );
    if (cause instanceof PythonApiError && (cause.status === 404 || cause.status === 422))
      throw new Response(null, { status: cause.status });
    if (cause instanceof Response) throw cause;
    const { safeWebsiteError } = await import("../server/errors.server");
    return {
      leaderboard: null,
      error: safeWebsiteError(cause),
      pageUnavailableUrl: null,
      latestDailyUrl: null,
      view: viewValue,
      query,
      search: null,
      focusTag: null,
      focusRank: null,
      me: null,
    } as const;
  }
}

export function headers() {
  return { "Cache-Control": "no-store" };
}

export default function TrackedLeaderboardRoute() {
  const {
    leaderboard: savedLeaderboard,
    error,
    pageUnavailableUrl,
    latestDailyUrl,
    view,
    query,
    search: savedSearch,
    focusTag,
    focusRank,
    me,
  } = useLoaderData<typeof loader>();
  const location = useLocation();
  const navigate = useNavigate();
  const shownWithoutDay = !new URLSearchParams(location.search).has("season");
  useEffect(() => {
    // Give the shown board its shareable address without reading it again.
    if (latestDailyUrl && shownWithoutDay)
      void navigate(latestDailyUrl, {
        replace: true,
        preventScrollReset: true,
        defaultShouldRevalidate: false,
      });
  }, [latestDailyUrl, shownWithoutDay, location.key, navigate]);
  const seasonExpired = useSeasonReread(
    savedLeaderboard?.generatedAt,
    !!savedLeaderboard?.seasonResetPending,
    view === "live",
  );
  const leaderboard = seasonExpired ? null : savedLeaderboard;
  const search = seasonExpired ? null : savedSearch;
  const unlistedTag = search?.results.length === 0 ? normalizePlayerTag(query) : null;
  const navigation = useNavigation();
  // Only a name search says so; Jump to me or a page turn loads quietly.
  const searching = navigation.state !== "idle" && !!navigation.formData?.has("q");
  const backState = useBackState("Rankings");
  const selectedRow = useRef<HTMLTableRowElement>(null);
  const rankInput = useRef<HTMLInputElement>(null);
  const board = useRef<HTMLElement>(null);
  useEffect(() => {
    if ((focusTag || focusRank) && selectedRow.current) {
      selectedRow.current.focus({ preventScroll: true });
      selectedRow.current.scrollIntoView({ block: "center", inline: "nearest" });
      // The rows don't light under a pointer left where it pressed until it
      // moves, so only the picked row stands out.
      if (board.current) board.current.dataset.still = "";
    }
  }, [focusTag, focusRank, leaderboard]);
  // A player found by name or tag, or the row of a rank asked for.
  const isPicked = (entry: { tag: string; rank: number }) =>
    focusTag ? entry.tag === focusTag : entry.rank === focusRank;
  const daily = leaderboard?.daily;
  const entries = leaderboard?.entries ?? [];
  const lastRank = entries.at(-1)?.rank ?? 0;
  const shown = entries.length
    ? `${entries[0].rank.toLocaleString()}–${lastRank.toLocaleString()}`
    : null;
  const total = ` of ${leaderboard?.totalEntries.toLocaleString()}`;
  const nextCount = leaderboard
    ? Math.min(
        leaderboard.pageSize,
        leaderboard.totalEntries - leaderboard.page * leaderboard.pageSize,
      )
    : 0;
  const pageKey = location.pathname + location.search;
  // ‹ or ›, the same at the board's head and its foot; a missing step keeps
  // its place, faded.
  const pageStep = (step: "prev" | "next") => {
    const to = step === "prev" ? leaderboard?.hasPrevious : leaderboard?.hasNext;
    const arrow = step === "prev" ? "‹" : "›";
    const label = step === "prev" ? `Previous ${PAGE_SIZE}` : `Next ${nextCount}`;
    return leaderboard && to ? (
      <Link
        className="button button-secondary"
        rel={step}
        aria-label={label}
        title={label}
        to={leaderboardUrl(
          view,
          leaderboard.page + (step === "prev" ? -1 : 1),
          daily ?? undefined,
        )}
      >
        {arrow}
      </Link>
    ) : (
      <span className="button button-secondary" aria-hidden="true">
        {arrow}
      </span>
    );
  };
  const compared = comparesWithReset(entries);
  const showsDays = hasRecentDays(entries);
  const showsToday = hasToday(entries);
  const newestObservedAt = leaderboard?.sourceObservations?.newestObservedAt ?? null;
  const now = useCurrentTime(leaderboard?.generatedAt);
  const ageSeconds = (observedAt: string) =>
    Math.max(0, Math.floor((now - Date.parse(observedAt)) / 1000));
  // The board's update time shows once, under the title. A Live row only
  // gets a dot when it fell over 10 minutes behind the board's newest update,
  // not when the whole board has aged; its exact time is on hover or tap.
  const isOld = (observedAt: string) =>
    view === "live" &&
    !!newestObservedAt &&
    (Date.parse(newestObservedAt) - Date.parse(observedAt)) / 1000 > OLD_UPDATE_SECONDS;
  const oldNote = <span className="sr-only"> · Older than the rest of the board</span>;
  // The website reads a profile without a clan as "Unknown"; it shows as no clan.
  const clanName = (clan: string) =>
    clan === "Unknown" ? <span className="no-clan">No clan</span> : <bdi>{clan}</bdi>;
  const secondsBeforeReset = (observedAt: string) =>
    daily ? Math.max(0, (Date.parse(daily.resetAt) - Date.parse(observedAt)) / 1000) : 0;
  const newestInput = daily ? leaderboard?.provenance.observedAt : null;
  const incomplete =
    !!newestInput && secondsBeforeReset(newestInput) > INCOMPLETE_INPUT_GAP_SECONDS;
  const savedBeforeReset = (observedAt: string) =>
    incomplete && secondsBeforeReset(observedAt) > INCOMPLETE_INPUT_GAP_SECONDS ? (
      <span className="player-update-age">
        {formatAge(secondsBeforeReset(observedAt))} before Reset
      </span>
    ) : null;
  // Live's search also takes a rank; At Reset, without a player search, has
  // a box of its own for any rank on the board.
  const rankJump =
    daily && leaderboard && leaderboard.totalEntries > PAGE_SIZE ? (
      <Form method="get" className="rank-jump" aria-label="Go to rank">
        <input type="hidden" name="view" value={view} />
        <input type="hidden" name="season" value={daily.officialSeasonId} />
        <input type="hidden" name="day" value={daily.dayNumber} />
        <label htmlFor="rank-jump-input" className="sr-only">
          Rank to go to
        </label>
        <input
          id="rank-jump-input"
          type="number"
          name="rank"
          min={1}
          max={leaderboard.totalEntries}
          required
          inputMode="numeric"
          placeholder="Go to rank"
          key={pageKey}
        />
        <button type="submit">Go</button>
      </Form>
    ) : null;
  // Which board: at the head of the board, beside its ranks, or over the
  // page's message when there is no board.
  const boardShown = !pageUnavailableUrl && !seasonExpired && !!leaderboard;
  const viewSwitch = (
    <nav aria-label="Leaderboard views" className="leaderboard-view-switch">
      <Link
        className="button secondary"
        aria-current={view === "live" ? "page" : undefined}
        to={leaderboardUrl("live", 1)}
      >
        Live
      </Link>
      <Link
        className="button secondary"
        aria-current={view === "daily" ? "page" : undefined}
        to="/leaderboards/tracked?view=daily&page=1"
      >
        At Reset
      </Link>
    </nav>
  );

  return (
    <main id="main-content" tabIndex={-1} className="page-shell rankings-page">
      {/* The title and the board's one time line: its Reset, or its update. */}
      <header className="page-hero rankings-header">
        <h1 id="leaderboard-title">
          {daily
            ? "At Reset"
            : view === "live"
              ? "Live Leaderboard"
              : "Daily leaderboard"}
        </h1>
        {daily ? (
          <p className="rankings-date">
            {/* The private alert check reads the board's Reset from this time
                (website_daily_board in python/src/clashlens/alerts.py). */}
            Day {daily.dayNumber} ·{" "}
            <LocalTimestamp className="board-reset" value={daily.resetAt} />
          </p>
        ) : newestObservedAt ? (
          <p className="rankings-date">
            {/* One update time for the whole board; rows show theirs only
                when they fall behind. The pulse goes once the board does. */}
            {ageSeconds(newestObservedAt) <= OLD_UPDATE_SECONDS ? (
              <span className="live-pip" aria-hidden="true" />
            ) : null}
            {/* A phone keeps the age; the exact time stays for screen readers. */}
            Updated{" "}
            <span className="rankings-stamp">
              <LocalTimestamp value={newestObservedAt} /> ·{" "}
            </span>
            <span className="rankings-age">
              {shortAge(ageSeconds(newestObservedAt))} ago
            </span>
          </p>
        ) : null}
      </header>
      {/* The Season's last board shows Complete days' EODs, as the game's
            Season-end result does. */}
      {daily && Date.parse(daily.resetAt) === Date.parse(daily.seasonEndAt) ? (
        <p className="rankings-context">
          Complete days show trophies after the automatic defense loss.
        </p>
      ) : null}
      {!daily && leaderboard?.seasonResetPending ? (
        <p className="rankings-context" role="status">
          {leaderboard.seasonResetPending.toLocaleString()} tracked{" "}
          {leaderboard.seasonResetPending === 1 ? "player is" : "players are"} waiting for
          their Season reset.
        </p>
      ) : null}
      {incomplete && newestInput ? (
        <div
          className="status-banner status-banner-warning daily-incomplete"
          role="status"
        >
          <p>
            <strong>Incomplete:</strong> no player updates were saved in the{" "}
            {formatAge(secondsBeforeReset(newestInput))} before this Reset. Newest:{" "}
            <LocalTimestamp value={newestInput} />.
          </p>
        </div>
      ) : null}

      {/* Finding a player or a rank on the board, and the Clasher's own place:
          one row of controls over the board. */}
      <div className="rankings-controls">
        {boardShown ? null : viewSwitch}
        {daily && (daily.previousSnapshot || daily.nextSnapshot) ? (
          <nav aria-label="Other days" className="snapshot-pagination">
            {daily.previousSnapshot ? (
              <Link
                aria-label={`Go to Day ${daily.previousSnapshot.dayNumber}`}
                to={leaderboardUrl("daily", 1, daily.previousSnapshot)}
              >
                ‹ Day {daily.previousSnapshot.dayNumber}
              </Link>
            ) : null}
            {daily.nextSnapshot ? (
              <Link
                aria-label={`Go to Day ${daily.nextSnapshot.dayNumber}`}
                to={leaderboardUrl("daily", 1, daily.nextSnapshot)}
              >
                Day {daily.nextSnapshot.dayNumber} ›
              </Link>
            ) : null}
          </nav>
        ) : null}
        {view === "live" ? (
          <section
            className="leaderboard-search"
            aria-label="Find your rank"
            // The page's own search, so the header's shows as its icon alone.
            data-page-search
            onKeyDown={(event) =>
              moveFocusWithArrows(event, ".rank-search-results a", rankInput.current)
            }
          >
            <Form method="get" role="search" aria-label="Find your leaderboard rank">
              <input type="hidden" name="view" value="live" />
              <input type="hidden" name="page" value="1" />
              <label htmlFor="rank-query" className="sr-only">
                Find your rank
              </label>
              <div className="search-controls rank-search-controls">
                <SearchIcon className="search-field-icon" />
                <input
                  ref={rankInput}
                  id="rank-query"
                  type="search"
                  name="q"
                  defaultValue={query}
                  key={query}
                  placeholder="Name, #tag or rank"
                  maxLength={MAX_SEARCH_QUERY_LENGTH}
                  autoCapitalize="none"
                  autoCorrect="off"
                  enterKeyHint="search"
                />
                <button type="submit" disabled={searching}>
                  {searching ? "Searching…" : "Search"}
                </button>
              </div>
            </Form>
            {search ? (
              <div aria-live="polite" aria-busy={searching}>
                {search.results.length ? null : (
                  <p>
                    {unlistedTag ? (
                      <>
                        {unlistedTag} isn't on this board.{" "}
                        <a href={canonicalPlayerPath(unlistedTag)}>
                          Open their player page
                        </a>
                      </>
                    ) : (
                      `No players on this board match “${query}”. ${noMatchHint(query)}`
                    )}
                  </p>
                )}
                <ul className="rank-search-results">
                  {search.results.map((entry) => (
                    <li key={entry.tag}>
                      <Link to={playerRankUrl(entry.tag, entry.rank)}>
                        <span>
                          <strong>
                            <bdi>{entry.name}</bdi>
                          </strong>
                          <small>{entry.tag}</small>
                        </span>
                        <span>
                          <strong>Rank {entry.rank.toLocaleString()}</strong>
                          <small>{entry.trophies.toLocaleString()} trophies</small>
                        </span>
                      </Link>
                    </li>
                  ))}
                </ul>
                {search.hasMore ? <p>First 20 matches shown.</p> : null}
              </div>
            ) : null}
          </section>
        ) : null}
        {rankJump}
        {me && view === "live" ? (
          <Link
            className="button secondary jump-to-me"
            to={playerRankUrl(me.tag, me.rank)}
            aria-label={`Jump to me, rank ${me.rank.toLocaleString()}`}
          >
            <DashboardIcon name="target" />
            {/* A phone says "You #130", as the row's own pill. */}
            <span className="jump-words">Jump to me</span>
            <span className="jump-you">You</span>
            <span className="jump-rank">#{me.rank.toLocaleString()}</span>
          </Link>
        ) : null}
      </div>

      {/* Without a board, its own message below says what went wrong. */}
      {error && leaderboard ? <ErrorNotice error={error} /> : null}

      {pageUnavailableUrl ? (
        <div className="empty-state">
          {focusRank ? (
            <>
              <h2>No rank {focusRank.toLocaleString()} on this board</h2>
              <p>It ranks fewer players than that right now.</p>
            </>
          ) : (
            <>
              <h2>This standings page is unavailable</h2>
              <p>The standings may have changed since this link was saved.</p>
            </>
          )}
          <Link className="button secondary" to={pageUnavailableUrl}>
            Go to page 1
          </Link>
        </div>
      ) : seasonExpired ? (
        <p className="empty-state" role="status">
          Loading the new Season's rankings…
        </p>
      ) : leaderboard ? (
        // The page's main table sits open on the page, with no box round it.
        <section
          ref={board}
          className="standings-board open-board"
          aria-labelledby="standings-table-title"
          onPointerMove={(event) => {
            if (event.movementX || event.movementY)
              delete event.currentTarget.dataset.still;
          }}
        >
          <div className="standings-toolbar">
            {viewSwitch}
            <h2 id="standings-table-title">
              {shown ? (
                <>
                  <span className="standings-word">Ranks </span>
                  {shown}
                  <span className="standings-total">{total}</span>
                </>
              ) : (
                "No listed players"
              )}
            </h2>
            {/* The pager again, small, so the next page is a click from
                the top as well as the foot. */}
            {entries.length && (leaderboard.hasPrevious || leaderboard.hasNext) ? (
              <nav aria-label="Previous and next page" className="standings-steps">
                {pageStep("prev")}
                {pageStep("next")}
              </nav>
            ) : null}
          </div>
          {entries.length === 0 ? (
            <div className="empty-state">
              <h3>No standings available yet</h3>
              <p>Check back after player updates have been saved.</p>
            </div>
          ) : (
            <>
              <p className="sr-only" id="rank-explanation">
                {rankExplanation(leaderboard?.provenance.version)}
              </p>
              <div
                aria-label={`${view === "daily" ? "Daily" : "Live"} leaderboard table`}
                className="table-wrap tracked-leaderboard-viewport"
                role="region"
                tabIndex={0}
                onKeyDown={(event) => moveFocusWithArrows(event, ".leaderboard-row")}
              >
                <table
                  aria-label={view === "daily" ? "Daily leaderboard" : "Live Leaderboard"}
                  className="data-table leaderboard-table"
                >
                  <caption className="sr-only">
                    {view === "daily"
                      ? "Players in the saved daily snapshot"
                      : "Players in the Live Leaderboard"}
                  </caption>
                  <thead>
                    <tr>
                      <th
                        scope="col"
                        aria-describedby="rank-explanation"
                        title={rankExplanation(leaderboard?.provenance.version)}
                        className="rank-cell"
                      >
                        Rank
                      </th>
                      {compared ? (
                        <th scope="col" className="move-cell">
                          {/* Says once what the ▲▼ count from; phones keep it
                              for screen readers. */}
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
                      <th scope="col" className="tie-slot">
                        <span className="sr-only">Tie</span>
                      </th>
                      <th scope="col" className="trophy-cell">
                        Trophies
                      </th>
                      <th
                        scope="col"
                        className="update-cell"
                        title={
                          view === "live"
                            ? "When we last confirmed each player's profile, even if it was unchanged."
                            : undefined
                        }
                      >
                        <span className="sr-only">Last updated</span>
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {entries.map((entry) => (
                      <tr
                        className="leaderboard-row"
                        data-selected={isPicked(entry) ? "true" : undefined}
                        data-you={entry.tag === me?.tag || undefined}
                        aria-label={
                          isPicked(entry)
                            ? `Selected player ${entry.name}, rank ${entry.rank}`
                            : undefined
                        }
                        ref={isPicked(entry) ? selectedRow : undefined}
                        tabIndex={isPicked(entry) ? -1 : undefined}
                        data-podium-rank={entry.rank <= 3 ? entry.rank : undefined}
                        key={entry.tag}
                      >
                        <td className="rank-cell" data-label="Rank">
                          <span className="rank-mark">{entry.rank.toLocaleString()}</span>
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
                            {entry.tag === me?.tag ? (
                              <span className="you-badge">You</span>
                            ) : null}
                            <span className="player-tag">{entry.tag}</span>
                          </span>
                          {/* A phone puts the clan on a smaller line under the
                              name; wider screens give it the Clan column. */}
                          <span className="player-sub">
                            <span className="player-clan" data-label="Clan">
                              {clanName(entry.clan)}
                            </span>
                            {/* As on wide screens, a phone shows a row's update
                                only when it fell behind the board: an old Live
                                row's age, or an At Reset row saved long before an
                                incomplete Reset. Each opens its exact time; screen
                                readers hear every row's. */}
                            <details
                              className="player-update-mobile"
                              data-old={isOld(entry.freshness.observedAt) || undefined}
                              data-shown={
                                isOld(entry.freshness.observedAt) ||
                                !!savedBeforeReset(entry.freshness.observedAt) ||
                                undefined
                              }
                            >
                              <summary>
                                <span className="sr-only">Last updated </span>
                                {view === "live" ? (
                                  <span className="update-words">
                                    {shortAge(ageSeconds(entry.freshness.observedAt))} ago
                                  </span>
                                ) : (
                                  <LocalTimestamp
                                    value={entry.freshness.observedAt}
                                    timeOnly
                                  />
                                )}
                                {isOld(entry.freshness.observedAt) ? oldNote : null}
                                {savedBeforeReset(entry.freshness.observedAt)}
                              </summary>
                              <LocalTimestamp value={entry.freshness.observedAt} />
                            </details>
                          </span>
                        </th>
                        {/* Wide screens add the tag on hover. */}
                        <td className="clan-cell">
                          {clanName(entry.clan)}
                          <span className="clan-tag" aria-hidden="true">
                            {entry.tag}
                          </span>
                        </td>
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
                        <td className="tie-slot">
                          {entry.tied ? (
                            <span
                              className="tie-pill"
                              title="Same trophies as another player"
                            >
                              Tie
                            </span>
                          ) : null}
                        </td>
                        <td className="trophy-cell" data-label="Trophies">
                          <TrophyMark />
                          <strong>{entry.trophies.toLocaleString()}</strong>
                        </td>
                        <td className="update-cell" data-label="Last updated">
                          {/* Live reads each row's age out, and shows only a dot on
                              a row behind the rest, its exact time in the tooltip. */}
                          {view === "live" ? (
                            <time
                              className="local-time player-update-age"
                              dateTime={entry.freshness.observedAt}
                              title={formatTimestamp(entry.freshness.observedAt)}
                              data-old={isOld(entry.freshness.observedAt) || undefined}
                            >
                              <span className="update-words">
                                {shortAge(ageSeconds(entry.freshness.observedAt))} ago
                              </span>
                              {isOld(entry.freshness.observedAt) ? oldNote : null}
                            </time>
                          ) : (
                            <>
                              {/* The board's date is in its heading; the time is enough. */}
                              <LocalTimestamp
                                value={entry.freshness.observedAt}
                                timeOnly
                              />
                              {savedBeforeReset(entry.freshness.observedAt)}
                            </>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {/* The same pager at the board's foot: ‹ 1–50 of 9,000 ›. */}
              {leaderboard.hasPrevious || leaderboard.hasNext ? (
                <nav aria-label="Leaderboard pages" className="standings-pagination">
                  {pageStep("prev")}
                  <span className="standings-range">
                    {shown}
                    <span className="standings-total">{total}</span>
                  </span>
                  {pageStep("next")}
                </nav>
              ) : null}
            </>
          )}
        </section>
      ) : (
        <div className="empty-state" role="alert">
          <h2>Leaderboard unavailable</h2>
          <p>
            We couldn’t load the standings.
            {error?.error.retryAfterSeconds
              ? ` Try again in about ${error.error.retryAfterSeconds} seconds.`
              : " Try again in a moment."}
          </p>
          <p className="empty-state-actions">
            <Link
              className="button secondary"
              to={location.pathname + location.search}
              reloadDocument
            >
              Try again
            </Link>
            {Number(new URLSearchParams(location.search).get("page") ?? 1) > 1 ? (
              <Link className="button secondary" to={leaderboardUrl(view, 1)}>
                Go to page 1
              </Link>
            ) : null}
          </p>
        </div>
      )}
    </main>
  );
}
