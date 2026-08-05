import { Link, useLoaderData, type LoaderFunctionArgs } from "react-router";

import { ErrorNotice } from "../components/ErrorNotice";
import { CoverageSummary } from "../components/CoverageSummary";
import { FreshnessText, Provenance } from "../components/Provenance";
import { StateBadge } from "../components/StateBadge";
import { canonicalPlayerPath } from "../lib/player-tag";
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
  return (
    <main className="page-shell">
      <section className="hero" aria-labelledby="home-title">
        <p className="eyebrow">Evidence-led Legend I tracking</p>
        <h1 id="home-title">Tracked Players</h1>
        <p className="lede">
          Live tracked players from the saved Python data service. This cohort is not a
          claim of full Legend I coverage.
        </p>
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
              defaultValue={data.query}
              placeholder="#2PP or Nova"
              autoComplete="off"
            />
            <button type="submit">Search</button>
          </div>
          <p className="form-help">
            Exact valid tags open a canonical player page. Other text searches known Clash
            Lens players only.
          </p>
        </form>
        {data.search ? <SearchResults search={data.search} /> : null}
      </section>

      {data.error ? <ErrorNotice error={data.error} /> : null}

      <section className="data-section" aria-labelledby="live-leaderboard-title">
        <div className="section-heading">
          <div>
            <p className="eyebrow">Live view</p>
            <h2 id="live-leaderboard-title">Live tracked players</h2>
          </div>
          <Link className="button secondary" to="/leaderboards/tracked">
            Full live leaderboard
          </Link>
        </div>
        {data.leaderboard ? (
          <>
            <p className="section-note">
              Showing the first {data.leaderboard.entries.length} of{" "}
              {data.leaderboard.totalTracked} actively tracked players. Older observations
              remain ordered and are marked.
            </p>
            <Provenance provenance={data.leaderboard.provenance} />
            <CoverageSummary coverage={data.leaderboard.coverage} />
            <section className="quality-panel" aria-labelledby="fixture-quality-title">
              <h3 id="fixture-quality-title">Fixture data-quality states</h3>
              <p>
                These explicit states remain distinct in the Python-owned response
                contract.
              </p>
              <ul className="state-list">
                {data.leaderboard.qualityStates.map((state) => (
                  <li key={state}>
                    <StateBadge state={state} />
                  </li>
                ))}
              </ul>
            </section>
            <LeaderboardTable entries={data.leaderboard.entries} />
          </>
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
        <FreshnessText freshness={result.freshness} />
        <StateBadge state={result.state} />
      </div>
    </div>
  );
}

function LeaderboardTable({ entries }: { entries: TrackedPlayerEntry[] }) {
  return (
    <div className="table-wrap">
      <table aria-label="Live tracked players" className="data-table">
        <caption className="sr-only">First 25 live tracked Legend I players</caption>
        <thead>
          <tr>
            <th scope="col">Tracked position</th>
            <th scope="col">Official rank</th>
            <th scope="col">Player</th>
            <th scope="col">Clan</th>
            <th scope="col">Trophies</th>
            <th scope="col">Observation</th>
            <th scope="col">State</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((entry) => (
            <tr key={entry.tag} data-testid="tracked-player-row">
              <td>{entry.rank}</td>
              <td>{entry.officialRank ?? "Not supplied"}</td>
              <th scope="row">
                <Link className="player-name" to={canonicalPlayerPath(entry.tag)}>
                  {entry.name}
                </Link>
                <span className="player-tag">{entry.tag}</span>
              </th>
              <td>{entry.clan}</td>
              <td>{entry.trophies.toLocaleString()}</td>
              <td>
                <FreshnessText freshness={entry.freshness} />
              </td>
              <td>
                <StateBadge state={entry.state} />
                <small className="table-detail">Confidence: {entry.confidence}</small>
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
