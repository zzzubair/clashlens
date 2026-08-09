import { useEffect, useRef, useState } from "react";
import { Link, useFetcher, useLoaderData, type LoaderFunctionArgs } from "react-router";

import { ErrorNotice } from "../components/ErrorNotice";
import { formatTimestamp } from "../components/Provenance";
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
  let leaderboard: TrackedLeaderboard | null = null;
  let search: SearchResponse | null = null;
  let error: WebsiteErrorResponse | null = null;
  try {
    const { createPythonClient } = await import("../services/python.server");
    leaderboard = await createPythonClient().getTrackedLeaderboard(25, "live");
  } catch (cause) {
    error = await safeError(cause);
  }
  if (rawQuery.length > MAX_SEARCH_QUERY_LENGTH) {
    error = {
      error: {
        code: "invalid_input",
        message: "Check the submitted value and try again.",
      },
    };
  } else if (query !== "") {
    try {
      const { createPythonClient } = await import("../services/python.server");
      search = await createPythonClient().searchPlayers(query);
    } catch (cause) {
      error = error ?? (await safeError(cause));
    }
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
  const suggestionData = requestMatchesInput ? searchFetcher.data : undefined;
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

  return (
    <main className="page-shell">
      <section className="hero" aria-labelledby="home-title">
        <div className="brand-lockup">
          <img
            className="brand-mark"
            src="/brand/clashlens-mark.svg"
            alt=""
            width="128"
            height="128"
          />
          <h1 id="home-title">Clash Lens</h1>
        </div>
      </section>

      <section className="search-panel" aria-labelledby="search-title">
        <h2 id="search-title">Find a player</h2>
        <form method="get" className="search-form">
          <label htmlFor="player-search">Search player tags or names</label>
          <div className="search-controls">
            <input
              id="player-search"
              name="q"
              type="search"
              value={searchQuery}
              placeholder="Player tag or Name"
              autoComplete="off"
              onChange={(event) => handleSearchInput(event.currentTarget.value)}
            />
            <button type="submit">Search</button>
          </div>
          {suggestionsOpen ? (
            <SearchSuggestions
              data={suggestionData}
              loading={searchFetcher.state !== "idle"}
            />
          ) : null}
        </form>
        {data.search && searchQuery === data.query ? (
          <SearchResults search={data.search} />
        ) : null}
      </section>

      {data.error ? <ErrorNotice error={data.error} /> : null}

      <section className="data-section" aria-labelledby="live-leaderboard-title">
        <div className="section-heading">
          <h2 id="live-leaderboard-title">Live leaderboard</h2>
          <Link className="button secondary" to="/leaderboards/tracked">
            View all →
          </Link>
        </div>
        {data.leaderboard ? (
          <LeaderboardTable entries={data.leaderboard.entries} />
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
  const unknownExactTag =
    search?.exactTag && results.length === 0 ? search.exactTag : null;

  return (
    <div
      id="player-search-suggestions"
      className="search-dropdown"
      role="region"
      aria-label="Player search suggestions"
      aria-live="polite"
      aria-busy={loading}
    >
      {loading && !search ? <p className="search-dropdown-status">Searching…</p> : null}
      {data?.error ? <p className="search-dropdown-status">Search unavailable.</p> : null}
      {!loading && search && results.length === 0 && !unknownExactTag ? (
        <p className="search-dropdown-status">No players found.</p>
      ) : null}
      {results.length > 0 || unknownExactTag ? (
        <ul className="search-dropdown-list">
          {results.map((result) => (
            <li key={result.tag}>
              <Link
                className="search-suggestion"
                data-testid="search-suggestion"
                to={canonicalPlayerPath(result.tag)}
              >
                <span className="search-suggestion-player">
                  <strong>{result.name}</strong>
                  <small>{result.tag}</small>
                </span>
                <span className="search-suggestion-meta">
                  {result.clan} · {result.trophies.toLocaleString()}
                </span>
              </Link>
            </li>
          ))}
          {unknownExactTag ? (
            <li>
              <Link
                className="search-suggestion"
                data-testid="search-suggestion"
                to={canonicalPlayerPath(unknownExactTag)}
              >
                <span className="search-suggestion-player">
                  <strong>Open {unknownExactTag}</strong>
                  <small>Player tag</small>
                </span>
              </Link>
            </li>
          ) : null}
        </ul>
      ) : null}
    </div>
  );
}

function SearchResults({ search }: { search: SearchResponse }) {
  if (search.exactTag) {
    const result = search.results[0];
    return (
      <div className="search-results" aria-live="polite">
        <h3>Exact valid player tag</h3>
        {result ? (
          <SearchResult result={result} />
        ) : (
          <p>
            <strong>{search.exactTag}</strong> is valid but is not in the known fixture
            cohort.
          </p>
        )}
        <Link className="text-link" to={canonicalPlayerPath(search.exactTag)}>
          Open the canonical player page
        </Link>
      </div>
    );
  }
  if (search.results.length === 0) {
    return (
      <div className="search-results" aria-live="polite">
        <h3>No known players found</h3>
        <p>No refresh or discovery work was created.</p>
      </div>
    );
  }
  return (
    <div className="search-results" aria-live="polite">
      <h3>Known Clash Lens players</h3>
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
    </div>
  );
}

function SearchResult({ result }: { result: SearchResponse["results"][number] }) {
  return (
    <div className="search-result">
      <div>
        <Link className="player-name" to={canonicalPlayerPath(result.tag)}>
          {result.name}
        </Link>
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
      <table aria-label="Live leaderboard" className="data-table">
        <caption className="sr-only">First 25 players on the live leaderboard</caption>
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
            <tr key={entry.tag} data-testid="tracked-player-row">
              <td>{entry.rank}</td>
              <th scope="row">
                <Link className="player-name" to={canonicalPlayerPath(entry.tag)}>
                  {entry.name}
                </Link>
                <span className="player-tag">{entry.tag}</span>
              </th>
              <td>{entry.clan}</td>
              <td>{entry.trophies.toLocaleString()}</td>
              <td>
                <time dateTime={entry.freshness.observedAt}>
                  {formatTimestamp(entry.freshness.observedAt)}
                </time>
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
