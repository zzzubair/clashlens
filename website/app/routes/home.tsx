import { useEffect } from "react";
import {
  Form,
  Link,
  redirect,
  useLoaderData,
  useNavigation,
  useSearchParams,
  type LoaderFunctionArgs,
} from "react-router";

import { ErrorNotice } from "../components/ErrorNotice";
import { TrophyMark, latestObservation } from "../components/LeaderboardShared";
import { SearchSuggestions, usePlayerSuggestions } from "../components/PlayerSearch";
import {
  LocalTimestamp,
  OLD_UPDATE_SECONDS,
  useServerTime,
} from "../components/Provenance";
import { canonicalPlayerPath, normalizePlayerTag } from "../lib/player-tag";
import { MAX_SEARCH_QUERY_LENGTH } from "../lib/validation";
import type {
  SearchResponse,
  TrackedLeaderboard,
  TrackedPlayerEntry,
  WebsiteErrorResponse,
} from "../lib/contracts";

export interface HomeLoaderData {
  leaderboard: TrackedLeaderboard | null;
  search: SearchResponse | null;
  query: string;
  error: WebsiteErrorResponse | null;
}

export async function loader({ request }: LoaderFunctionArgs): Promise<HomeLoaderData> {
  const rawQuery = new URL(request.url).searchParams.get("q") ?? "";
  const query = rawQuery.trim();
  const invalidQuery = rawQuery.length > MAX_SEARCH_QUERY_LENGTH;
  const exactTag = invalidQuery ? null : normalizePlayerTag(query);
  if (exactTag) throw redirect(canonicalPlayerPath(exactTag));
  const client = import("../services/python.server").then(({ createPythonClient }) =>
    createPythonClient(),
  );
  const [leaderboardResult, searchResult] = await Promise.allSettled([
    client.then((python) => python.getTrackedLeaderboard(25, "live")),
    !invalidQuery && query !== ""
      ? client.then((python) => python.searchPlayers(query))
      : Promise.resolve(null),
  ]);
  const leaderboard =
    leaderboardResult.status === "fulfilled" ? leaderboardResult.value : null;
  const search = searchResult.status === "fulfilled" ? searchResult.value : null;
  let error: WebsiteErrorResponse | null = null;
  if (leaderboardResult.status === "rejected") {
    error = await safeError(leaderboardResult.reason);
  }
  if (invalidQuery) {
    error = {
      error: {
        code: "invalid_input",
        message: "Check the submitted value and try again.",
      },
    };
  } else if (searchResult.status === "rejected" && error === null) {
    error = await safeError(searchResult.reason);
  }
  return { leaderboard, search, query, error };
}

export function headers() {
  return { "Cache-Control": "no-store" };
}

export default function Home() {
  const data = useLoaderData<typeof loader>();
  // Set by /logout when this browser logged out but the server could not record it.
  const [searchParams] = useSearchParams();
  const logoutUnrecorded = searchParams.get("logout") === "unrecorded";
  const suggestions = usePlayerSuggestions(data.query);
  const { setQuery } = suggestions;
  const searching = useNavigation().state !== "idle";

  useEffect(() => {
    setQuery(data.query);
  }, [data.query, setQuery]);

  const leaderboard = data.leaderboard;
  const latestObservedAt = leaderboard ? latestObservation(leaderboard.entries) : null;
  const now = useServerTime(leaderboard?.generatedAt);
  const staleEntries =
    leaderboard?.entries.filter(
      (entry) =>
        Math.floor((now - Date.parse(entry.freshness.observedAt)) / 1000) >
        OLD_UPDATE_SECONDS,
    ).length ?? 0;

  return (
    <main id="main-content" tabIndex={-1} className="page-shell home-page">
      {logoutUnrecorded ? (
        <div className="status-banner status-banner-warning" role="status">
          You are logged out on this browser, but Clash Lens could not record it.
        </div>
      ) : null}
      <section className="home-overview" aria-labelledby="search-title">
        <div className="home-intro">
          <img
            className="home-badge"
            src="/images/legend-league.webp"
            alt=""
            width="72"
            height="72"
          />
          <h1 id="search-title">Legend League</h1>
          <p>
            {leaderboard
              ? `Daily results, rankings and armies for ${leaderboard.totalTracked.toLocaleString()} tracked players.`
              : "Daily results, rankings and armies for tracked players."}
          </p>
          <p>
            To look someone up, enter their full player tag, including the #. Legend I
            players start tracking automatically.
          </p>
        </div>
        <div className="player-search-panel">
          <Form
            method="get"
            action="/"
            role="search"
            className="search-form"
            onSubmit={suggestions.dismiss}
            onKeyDown={(event) => {
              if (event.key === "Escape") suggestions.dismiss();
            }}
            onBlur={(event) => {
              if (!event.currentTarget.contains(event.relatedTarget))
                suggestions.dismiss();
            }}
          >
            <label className="sr-only" htmlFor="player-search">
              Search players and Clash Lens profiles
            </label>
            <div className="search-controls">
              <svg
                className="search-field-icon"
                aria-hidden="true"
                viewBox="0 0 24 24"
                width="20"
                height="20"
                fill="none"
                stroke="currentColor"
                strokeWidth="1.7"
                strokeLinecap="round"
              >
                <path d="m21 21-4.35-4.35m2.35-5.65a8 8 0 1 1-16 0 8 8 0 0 1 16 0Z" />
              </svg>
              <input
                id="player-search"
                name="q"
                type="search"
                value={suggestions.query}
                placeholder="Search player, @username or #tag"
                autoComplete="off"
                autoCapitalize="none"
                enterKeyHint="search"
                aria-controls={suggestions.open ? "player-search-suggestions" : undefined}
                aria-describedby="search-keyboard-help"
                onChange={(event) => suggestions.change(event.currentTarget.value)}
              />
              <button type="submit" disabled={searching}>
                {searching ? "Searching…" : "Search"}
              </button>
            </div>
            <span className="sr-only" id="search-keyboard-help">
              Suggestions appear below as you type. Press Tab to reach them, or Escape to
              dismiss.
            </span>
            {suggestions.open ? (
              <SearchSuggestions
                id="player-search-suggestions"
                data={suggestions.data}
                loading={suggestions.loading}
              />
            ) : null}
          </Form>
          {data.search && suggestions.query === data.query ? (
            <SearchResults search={data.search} busy={searching} />
          ) : null}
        </div>
      </section>

      {data.error ? <ErrorNotice error={data.error} /> : null}

      <section
        className="data-section home-leaderboard"
        aria-labelledby="live-leaderboard-title"
      >
        <div className="section-heading">
          <div>
            <h2 id="live-leaderboard-title">Rankings</h2>
            {leaderboard ? (
              <p className="standings-context">
                <span>
                  Top {leaderboard.entries.length} of{" "}
                  {leaderboard.totalTracked.toLocaleString()} tracked players
                </span>
                {latestObservedAt ? (
                  <span>
                    Newest player update <LocalTimestamp value={latestObservedAt} />
                  </span>
                ) : null}
                {staleEntries > 0 ? (
                  <span>
                    {staleEntries} of {leaderboard.entries.length} more than 10 minutes
                    old
                  </span>
                ) : null}
              </p>
            ) : null}
          </div>
          <Link
            className="section-link leaderboard-more"
            to="/leaderboards/tracked?view=live&page=1"
          >
            Full rankings
          </Link>
        </div>
        {leaderboard ? (
          <LeaderboardTable entries={leaderboard.entries} />
        ) : (
          <div className="empty-state">
            <h3>Tracked player data is unavailable</h3>
            <p>The saved leaderboard could not be loaded. Try again later.</p>
          </div>
        )}
      </section>
    </main>
  );
}

function SearchResults({ search, busy }: { search: SearchResponse; busy: boolean }) {
  const users = search.users;
  return (
    <div className="search-results" aria-live="polite" aria-busy={busy}>
      {users.length > 0 ? (
        <section aria-labelledby="profile-search-title">
          <h3 id="profile-search-title">Clash Lens profiles</h3>
          <ul className="search-result-list">
            {users.map((user) => (
              <li key={user.username}>
                <div className="search-result search-result-profile">
                  <div>
                    <Link
                      className="player-name"
                      to={`/users/${encodeURIComponent(user.username)}`}
                    >
                      {user.displayName}{" "}
                      <span className="profile-badge">Clash Lens profile</span>
                    </Link>
                    <span className="player-tag">@{user.username}</span>
                  </div>
                  <span>
                    {user.linkedPlayerCount} linked{" "}
                    {user.linkedPlayerCount === 1 ? "account" : "accounts"}
                  </span>
                </div>
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      {search.results.length > 0 || search.exactTag || users.length === 0 ? (
        <PlayerSearchResults search={search} />
      ) : null}
    </div>
  );
}

function PlayerSearchResults({ search }: { search: SearchResponse }) {
  if (search.exactTag) {
    const result = search.results.find((entry) => entry.tag === search.exactTag);
    return (
      <section>
        <h3>Player tag</h3>
        {result ? (
          <SearchResult result={result} />
        ) : (
          <p>
            We haven't saved a profile for <strong>{search.exactTag}</strong> yet.
          </p>
        )}
        {!result ? (
          <a
            className="button button-secondary"
            href={canonicalPlayerPath(search.exactTag)}
          >
            Open player profile
          </a>
        ) : null}
      </section>
    );
  }
  if (search.results.length === 0) {
    return (
      <section>
        <h3>No players or profiles found</h3>
        <p>
          Name search only finds players and profiles Clash Lens has already saved. To
          look up anyone else, enter their full player tag, including the #.
        </p>
      </section>
    );
  }
  return (
    <section>
      <h3>Clash of Clans players</h3>
      <p className="section-note">
        Names are not unique. Tag, clan, trophies, and data age distinguish each result.
      </p>
      <ul className="search-result-list">
        {search.results.map((result) => (
          <li key={result.tag}>
            <SearchResult result={result} />
          </li>
        ))}
      </ul>
    </section>
  );
}

function SearchResult({ result }: { result: SearchResponse["results"][number] }) {
  return (
    <div className="search-result">
      <div>
        <a className="player-name" href={canonicalPlayerPath(result.tag)}>
          {result.name}
        </a>
        <span className="player-tag">{result.tag}</span>
      </div>
      <div className="search-context">
        <span>{result.clan}</span>
        <span>
          {result.trophies === null
            ? "Waiting for this player's Season reset"
            : `${result.trophies.toLocaleString()} trophies`}
        </span>
      </div>
    </div>
  );
}

function LeaderboardTable({ entries }: { entries: TrackedPlayerEntry[] }) {
  return (
    <div className="table-wrap">
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
                <a className="player-name" href={canonicalPlayerPath(entry.tag)}>
                  {entry.name}
                </a>
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
  );
}

async function safeError(cause: unknown): Promise<WebsiteErrorResponse> {
  const { safeWebsiteError } = await import("../server/errors.server");
  return safeWebsiteError(cause);
}
