import { useEffect, useRef, useState } from "react";
import {
  Form,
  Link,
  useFetcher,
  useLoaderData,
  type LoaderFunctionArgs,
} from "react-router";

import { ErrorNotice } from "../components/ErrorNotice";
import { TrophyMark, latestObservation } from "../components/LeaderboardShared";
import { LocalTimestamp } from "../components/Provenance";
import { canonicalPlayerPath } from "../lib/player-tag";
import { MAX_SEARCH_QUERY_LENGTH } from "../lib/validation";
import type {
  SearchResponse,
  TrackedLeaderboard,
  TrackedPlayerEntry,
  WebsiteErrorResponse,
} from "../lib/contracts";
import type { PlayerSearchLoaderData } from "./player-search";

const SEARCH_DEBOUNCE_MS = 180;

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
  const searchFetcher = useFetcher<PlayerSearchLoaderData>();
  const [searchQuery, setSearchQuery] = useState(data.query);
  const [requestedQuery, setRequestedQuery] = useState("");
  const searchTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    setSearchQuery(data.query);
  }, [data.query]);

  useEffect(
    () => () => {
      if (searchTimer.current !== null) clearTimeout(searchTimer.current);
    },
    [],
  );

  const normalizedQuery = searchQuery.trim().toLocaleLowerCase();
  const requestMatchesInput =
    requestedQuery.toLocaleLowerCase() === normalizedQuery && normalizedQuery !== "";
  const suggestionData =
    requestMatchesInput &&
    searchFetcher.data?.query.toLocaleLowerCase() === normalizedQuery
      ? searchFetcher.data
      : undefined;
  const suggestionsOpen =
    requestMatchesInput &&
    (searchFetcher.state !== "idle" ||
      suggestionData?.search != null ||
      !!suggestionData?.error);

  function handleSearchInput(value: string) {
    setSearchQuery(value);
    if (searchTimer.current !== null) clearTimeout(searchTimer.current);

    const query = value.trim();
    if (query === "" || value.length > MAX_SEARCH_QUERY_LENGTH) {
      setRequestedQuery("");
      return;
    }

    searchTimer.current = setTimeout(() => {
      setRequestedQuery(query);
      void searchFetcher.load(`/resources/players/search?q=${encodeURIComponent(query)}`);
    }, SEARCH_DEBOUNCE_MS);
  }

  const leaderboard = data.leaderboard;
  const latestObservedAt = leaderboard ? latestObservation(leaderboard.entries) : null;

  return (
    <main id="main-content" tabIndex={-1} className="page-shell home-page">
      <section className="home-overview" aria-labelledby="search-title">
        <div className="home-intro">
          <h1 id="search-title">Legend League</h1>
          <p>
            {leaderboard
              ? `Daily results, rankings and armies for ${leaderboard.totalTracked.toLocaleString()} tracked players.`
              : "Daily results, rankings and armies for tracked players."}
          </p>
        </div>
        <div className="player-search-panel">
          <Form
            method="get"
            action="/"
            role="search"
            className="search-form"
            onSubmit={() => {
              if (searchTimer.current !== null) clearTimeout(searchTimer.current);
              setRequestedQuery("");
            }}
            onKeyDown={(event) => {
              if (event.key === "Escape") {
                if (searchTimer.current !== null) clearTimeout(searchTimer.current);
                setRequestedQuery("");
              }
            }}
            onBlur={(event) => {
              if (!event.currentTarget.contains(event.relatedTarget)) {
                if (searchTimer.current !== null) clearTimeout(searchTimer.current);
                setRequestedQuery("");
              }
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
                value={searchQuery}
                placeholder="Search player, @username or #tag"
                autoComplete="off"
                autoCapitalize="none"
                enterKeyHint="search"
                aria-controls={suggestionsOpen ? "player-search-suggestions" : undefined}
                aria-describedby="search-keyboard-help"
                onChange={(event) => handleSearchInput(event.currentTarget.value)}
              />
              <button type="submit">Search</button>
            </div>
            <span className="sr-only" id="search-keyboard-help">
              Suggestions appear below as you type. Press Tab to reach them, or Escape to
              dismiss.
            </span>
            {suggestionsOpen ? (
              <SearchSuggestions
                data={suggestionData}
                loading={searchFetcher.state !== "idle"}
              />
            ) : null}
          </Form>
          {data.search && searchQuery === data.query ? (
            <SearchResults search={data.search} />
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
                  Top {leaderboard.entries.length} of {leaderboard.totalTracked} tracked
                  players
                </span>
                {latestObservedAt ? (
                  <span>
                    Last updated <LocalTimestamp value={latestObservedAt} />
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

function SearchSuggestions({
  data,
  loading,
}: {
  data: PlayerSearchLoaderData | undefined;
  loading: boolean;
}) {
  const search = data?.search;
  const results = search?.results.slice(0, 5) ?? [];
  const users = search?.users.slice(0, 3) ?? [];
  const unknownExactTag =
    search?.exactTag && !results.some((result) => result.tag === search.exactTag)
      ? search.exactTag
      : null;

  return (
    <div
      id="player-search-suggestions"
      className="search-dropdown"
      role="region"
      aria-label="Player and profile search suggestions"
      aria-live="polite"
      aria-busy={loading}
    >
      {loading && !search ? <p className="search-dropdown-status">Searching…</p> : null}
      {data?.error ? <p className="search-dropdown-status">Search unavailable.</p> : null}
      {!loading &&
      search &&
      results.length === 0 &&
      users.length === 0 &&
      !unknownExactTag ? (
        <p className="search-dropdown-status">No players or profiles found.</p>
      ) : null}
      {results.length > 0 || users.length > 0 || unknownExactTag ? (
        <ul className="search-dropdown-list">
          {users.map((user) => (
            <li key={`user:${user.username}`}>
              <Link
                className="search-suggestion"
                data-testid="search-suggestion"
                to={`/users/${encodeURIComponent(user.username)}`}
              >
                <span className="search-suggestion-player">
                  <strong>{user.displayName}</strong>
                  <small>@{user.username} · Clash Lens</small>
                </span>
                <span className="search-suggestion-meta">
                  {user.linkedPlayerCount} linked{" "}
                  {user.linkedPlayerCount === 1 ? "account" : "accounts"}
                </span>
              </Link>
            </li>
          ))}
          {results.map((result) => (
            <li key={result.tag}>
              <a
                className="search-suggestion"
                data-testid="search-suggestion"
                href={canonicalPlayerPath(result.tag)}
              >
                <span className="search-suggestion-player">
                  <strong>{result.name}</strong>
                  <small>{result.tag}</small>
                </span>
                <span className="search-suggestion-meta">
                  {result.clan} · {result.trophies.toLocaleString()}
                </span>
              </a>
            </li>
          ))}
          {unknownExactTag ? (
            <li>
              <a
                className="search-suggestion"
                data-testid="search-suggestion"
                href={canonicalPlayerPath(unknownExactTag)}
              >
                <span className="search-suggestion-player">
                  <strong>Open {unknownExactTag}</strong>
                  <small>Player tag</small>
                </span>
              </a>
            </li>
          ) : null}
        </ul>
      ) : null}
    </div>
  );
}

function SearchResults({ search }: { search: SearchResponse }) {
  const users = search.users;
  return (
    <div className="search-results" aria-live="polite">
      {users.length > 0 ? (
        <section aria-labelledby="profile-search-title">
          <h3 id="profile-search-title">Clash Lens profiles</h3>
          <ul className="search-result-list">
            {users.map((user) => (
              <li key={user.username}>
                <div className="search-result">
                  <div>
                    <Link
                      className="player-name"
                      to={`/users/${encodeURIComponent(user.username)}`}
                    >
                      {user.displayName}
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
        <h3>Player found</h3>
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
          Try a Clash Lens username, display name, Clash of Clans name, or full player
          tag.
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
        <span>{result.trophies.toLocaleString()} trophies</span>
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
