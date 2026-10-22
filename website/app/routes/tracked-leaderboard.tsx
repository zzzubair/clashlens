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
import { ErrorNotice } from "../components/ErrorNotice";
import { useSeasonReread } from "../components/SeasonReread";
import { TrophyMark } from "../components/LeaderboardShared";
import {
  formatAge,
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

const PAGE_SIZE = 100;
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
  const pageValue = url.searchParams.get("page");
  if (pageValue === null) throw redirect(leaderboardUrl(viewValue, 1, selector));
  if (!/^[1-9][0-9]*$/.test(pageValue)) throw new Response(null, { status: 422 });
  const page = Number(pageValue);
  if (!Number.isSafeInteger(page) || !Number.isSafeInteger((page - 1) * PAGE_SIZE))
    throw new Response(null, { status: 422 });
  const query = (url.searchParams.get("q") ?? "").trim();
  const player = url.searchParams.get("player");
  const focusTag = player === null ? null : normalizePlayerTag(player);
  if (
    query.length > MAX_SEARCH_QUERY_LENGTH ||
    (player !== null && !focusTag) ||
    (viewValue === "daily" && (query || player !== null))
  )
    throw new Response(null, { status: 422 });
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
  const backState = useBackState("Rankings");
  const selectedRow = useRef<HTMLTableRowElement>(null);
  const rankInput = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (focusTag && selectedRow.current) {
      selectedRow.current.focus({ preventScroll: true });
      selectedRow.current.scrollIntoView({ block: "center", inline: "nearest" });
    }
  }, [focusTag, leaderboard]);
  const daily = leaderboard?.daily;
  const entries = leaderboard?.entries ?? [];
  const lastRank = entries.at(-1)?.rank ?? 0;
  const range =
    leaderboard && entries.length
      ? `${entries[0].rank.toLocaleString()}–${lastRank.toLocaleString()} of ${leaderboard.totalEntries.toLocaleString()}`
      : null;
  const nextCount = leaderboard
    ? Math.min(
        leaderboard.pageSize,
        leaderboard.totalEntries - leaderboard.page * leaderboard.pageSize,
      )
    : 0;
  const newestObservedAt = leaderboard?.sourceObservations?.newestObservedAt ?? null;
  const now = useCurrentTime(leaderboard?.generatedAt);
  const ageSeconds = (observedAt: string) =>
    Math.max(0, Math.floor((now - Date.parse(observedAt)) / 1000));
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

  return (
    <main id="main-content" tabIndex={-1} className="page-shell rankings-page">
      <section className="rankings-header" aria-labelledby="leaderboard-title">
        <div className="rankings-heading">
          <div>
            {daily ? <p className="rankings-kicker">Day {daily.dayNumber}</p> : null}
            <h1 id="leaderboard-title">
              {daily ? (
                <>
                  Leaderboard at <LocalTimestamp value={daily.resetAt} />
                </>
              ) : view === "live" ? (
                "Live Leaderboard"
              ) : (
                "Daily leaderboard"
              )}
            </h1>
          </div>
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
              Reset
            </Link>
          </nav>
        </div>
        {!daily && newestObservedAt ? (
          <p className="rankings-context">
            Updated <LocalTimestamp value={newestObservedAt} />
          </p>
        ) : null}
        {!daily && leaderboard?.seasonResetPending ? (
          <p className="rankings-context" role="status">
            {leaderboard.seasonResetPending.toLocaleString()} tracked{" "}
            {leaderboard.seasonResetPending === 1 ? "player is" : "players are"} waiting
            for their Season reset.
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
      </section>

      {error ? <ErrorNotice error={error} /> : null}

      {view === "live" ? (
        <section
          className="leaderboard-search"
          aria-label="Find your rank"
          onKeyDown={(event) =>
            moveFocusWithArrows(event, ".rank-search-results a", rankInput.current)
          }
        >
          <Form method="get" role="search" aria-label="Find your leaderboard rank">
            <input type="hidden" name="view" value="live" />
            <input type="hidden" name="page" value="1" />
            <label htmlFor="rank-query">Find your rank</label>
            <div className="rank-search-controls">
              <input
                ref={rankInput}
                id="rank-query"
                type="search"
                name="q"
                defaultValue={query}
                key={query}
                placeholder="Player name or #tag"
                maxLength={MAX_SEARCH_QUERY_LENGTH}
                autoCapitalize="none"
                autoCorrect="off"
                enterKeyHint="search"
              />
              <button
                className="button button-primary"
                type="submit"
                disabled={navigation.state !== "idle"}
              >
                {navigation.state !== "idle" ? "Searching…" : "Search"}
              </button>
            </div>
          </Form>
          {search ? (
            <div aria-live="polite" aria-busy={navigation.state !== "idle"}>
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
                        Rank {entry.rank.toLocaleString()}
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

      {pageUnavailableUrl ? (
        <div className="empty-state">
          <h2>This standings page is unavailable</h2>
          <p>The standings may have changed since this link was saved.</p>
          <Link className="button secondary" to={pageUnavailableUrl}>
            Go to page 1
          </Link>
        </div>
      ) : seasonExpired ? (
        <p className="empty-state" role="status">
          Loading the new Season's rankings…
        </p>
      ) : leaderboard ? (
        <section className="standings-board" aria-labelledby="standings-table-title">
          <div className="standings-toolbar">
            <div>
              <h2 id="standings-table-title">Standings</h2>
              <p>{range ?? "No listed players"}</p>
            </div>
          </div>
          {daily && (daily.previousSnapshot || daily.nextSnapshot) ? (
            <nav aria-label="Other days" className="snapshot-pagination">
              {daily.previousSnapshot ? (
                <Link to={leaderboardUrl("daily", 1, daily.previousSnapshot)}>
                  Go to Day {daily.previousSnapshot.dayNumber}
                </Link>
              ) : null}
              {daily.nextSnapshot ? (
                <Link to={leaderboardUrl("daily", 1, daily.nextSnapshot)}>
                  Go to Day {daily.nextSnapshot.dayNumber}
                </Link>
              ) : null}
            </nav>
          ) : null}
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
                      >
                        Rank
                      </th>
                      <th scope="col">Player</th>
                      <th scope="col">Clan</th>
                      <th scope="col">Trophies</th>
                      <th
                        scope="col"
                        title={
                          view === "live"
                            ? "When we last confirmed each player's profile, even if it was unchanged."
                            : undefined
                        }
                      >
                        Last updated
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {entries.map((entry) => (
                      <tr
                        className="leaderboard-row"
                        data-selected={entry.tag === focusTag ? "true" : undefined}
                        aria-label={
                          entry.tag === focusTag
                            ? `Selected player ${entry.name}, rank ${entry.rank}`
                            : undefined
                        }
                        ref={entry.tag === focusTag ? selectedRow : undefined}
                        tabIndex={entry.tag === focusTag ? -1 : undefined}
                        data-podium-rank={entry.rank <= 3 ? entry.rank : undefined}
                        key={entry.tag}
                      >
                        <td className="rank-cell" data-label="Rank">
                          <span className="rank-mark">{entry.rank.toLocaleString()}</span>
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
                          <details className="player-update-mobile">
                            <summary>
                              Last updated{" "}
                              {view === "live" ? (
                                `${formatAge(ageSeconds(entry.freshness.observedAt))} ago`
                              ) : (
                                <LocalTimestamp value={entry.freshness.observedAt} />
                              )}
                              {view === "live" &&
                              ageSeconds(entry.freshness.observedAt) >
                                OLD_UPDATE_SECONDS ? (
                                <span className="player-update-age">Over 10 min old</span>
                              ) : null}
                              {savedBeforeReset(entry.freshness.observedAt)}
                            </summary>
                            <LocalTimestamp value={entry.freshness.observedAt} />
                          </details>
                        </th>
                        <td data-label="Clan">{entry.clan}</td>
                        <td className="trophy-cell" data-label="Trophies">
                          <TrophyMark />
                          <strong>{entry.trophies.toLocaleString()}</strong>
                          <span className="tie-slot">
                            {entry.tied ? (
                              <span
                                className="tie-pill"
                                title="Same trophies as another player"
                              >
                                Tie
                              </span>
                            ) : null}
                          </span>
                        </td>
                        <td data-label="Last updated">
                          <LocalTimestamp value={entry.freshness.observedAt} />
                          {view === "live" ? (
                            <span className="player-update-age">
                              {formatAge(ageSeconds(entry.freshness.observedAt))} ago
                              {ageSeconds(entry.freshness.observedAt) > OLD_UPDATE_SECONDS
                                ? " · Over 10 min old"
                                : null}
                            </span>
                          ) : (
                            savedBeforeReset(entry.freshness.observedAt)
                          )}
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
                    Previous {PAGE_SIZE}
                  </Link>
                ) : null}
                <span className="pagination-status">{range}</span>
                {leaderboard.hasNext ? (
                  <Link
                    className="button button-secondary"
                    to={leaderboardUrl(view, leaderboard.page + 1, daily ?? undefined)}
                  >
                    Next {nextCount}
                  </Link>
                ) : null}
              </nav>
            </>
          )}
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
