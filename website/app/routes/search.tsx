import {
  Link,
  redirect,
  useLoaderData,
  useNavigation,
  type LoaderFunctionArgs,
} from "react-router";

import { useBackState } from "../components/BackLink";
import { ErrorNotice } from "../components/ErrorNotice";
import { PageSearch } from "../components/PageSearch";
import { canonicalPlayerPath, noMatchHint, normalizePlayerTag } from "../lib/player-tag";
import { MAX_SEARCH_QUERY_LENGTH } from "../lib/validation";
import type { SearchResponse, WebsiteErrorResponse } from "../lib/contracts";

export interface SearchLoaderData {
  search: SearchResponse | null;
  query: string;
  error: WebsiteErrorResponse | null;
}

export function meta() {
  return [{ title: "Search · Clash Lens" }];
}

/**
 * GET /search?q= — every saved player and Clash Lens profile matching the
 * words. A complete #tag opens that player's page instead.
 */
export async function loader({ request }: LoaderFunctionArgs): Promise<SearchLoaderData> {
  const params = new URL(request.url).searchParams;
  const rawQuery = params.get("q") ?? "";
  const query = rawQuery.trim();
  if (rawQuery.length > MAX_SEARCH_QUERY_LENGTH) {
    return {
      search: null,
      query,
      error: {
        error: {
          code: "invalid_input",
          message: "Check the submitted value and try again.",
        },
      },
    };
  }
  const exactTag = normalizePlayerTag(query);
  if (exactTag) {
    const retry = params.get("recheck") === exactTag ? "?retry=1" : "";
    throw redirect(`${canonicalPlayerPath(exactTag)}${retry}`);
  }
  if (query === "") return { search: null, query, error: null };
  try {
    const { createPythonClient } = await import("../services/python.server");
    return {
      search: await createPythonClient().searchPlayers(query),
      query,
      error: null,
    };
  } catch (cause) {
    return { search: null, query, error: await safeError(cause) };
  }
}

export function headers() {
  return { "Cache-Control": "no-store" };
}

export default function Search() {
  const data = useLoaderData<typeof loader>();
  const searching = useNavigation().state !== "idle";
  return (
    <main id="main-content" tabIndex={-1} className="page-shell home-page search-page">
      <h1>Search players</h1>
      <PageSearch
        query={data.query}
        results={
          data.search ? <SearchResults search={data.search} busy={searching} /> : null
        }
      />
      {data.error ? <ErrorNotice error={data.error} /> : null}
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
                      <bdi>{user.displayName}</bdi>{" "}
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
        <p>{noMatchHint(search.query)}</p>
      </section>
    );
  }
  return (
    <section>
      <h3>Clash of Clans players</h3>
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
  const backState = useBackState("Search");
  return (
    <div className="search-result">
      <div>
        <Link
          className="player-name"
          to={canonicalPlayerPath(result.tag)}
          state={backState}
        >
          <bdi>{result.name}</bdi>
        </Link>
        <span className="player-tag">{result.tag}</span>
      </div>
      <div className="search-context">
        <bdi>{result.clan}</bdi>
        <span>
          {result.trophies === null
            ? "Waiting for this player's Season reset"
            : `${result.trophies.toLocaleString()} trophies`}
        </span>
      </div>
    </div>
  );
}

async function safeError(cause: unknown): Promise<WebsiteErrorResponse> {
  const { safeWebsiteError } = await import("../server/errors.server");
  return safeWebsiteError(cause);
}
