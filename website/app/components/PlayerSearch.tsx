import { useEffect, useRef, useState } from "react";
import { Link, useFetcher, useNavigate } from "react-router";

import { canonicalPlayerPath, normalizePlayerTag } from "../lib/player-tag";
import { MAX_SEARCH_QUERY_LENGTH } from "../lib/validation";
import type { PlayerSearchLoaderData } from "../routes/player-search";

const SEARCH_DEBOUNCE_MS = 180;

/**
 * Typed text plus the suggestions fetched for it. Nothing is requested until
 * the user types, and only after a short pause, so idle search boxes cost no
 * requests.
 */
export function usePlayerSuggestions(initialQuery = "") {
  const fetcher = useFetcher<PlayerSearchLoaderData>();
  const [query, setQuery] = useState(initialQuery);
  const [requestedQuery, setRequestedQuery] = useState("");
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (timer.current !== null) clearTimeout(timer.current);
    },
    [],
  );

  const normalizedQuery = query.trim().toLocaleLowerCase();
  const requestMatchesInput =
    requestedQuery.toLocaleLowerCase() === normalizedQuery && normalizedQuery !== "";
  const data =
    requestMatchesInput && fetcher.data?.query.toLocaleLowerCase() === normalizedQuery
      ? fetcher.data
      : undefined;
  const open =
    requestMatchesInput &&
    (fetcher.state !== "idle" || data?.search != null || !!data?.error);

  function dismiss() {
    if (timer.current !== null) clearTimeout(timer.current);
    setRequestedQuery("");
  }

  function request(value: string, delay: number) {
    if (timer.current !== null) clearTimeout(timer.current);

    const trimmed = value.trim();
    if (trimmed === "" || value.length > MAX_SEARCH_QUERY_LENGTH) {
      setRequestedQuery("");
      return;
    }

    timer.current = setTimeout(() => {
      setRequestedQuery(trimmed);
      void fetcher.load(`/resources/players/search?q=${encodeURIComponent(trimmed)}`);
    }, delay);
  }

  function change(value: string) {
    setQuery(value);
    request(value, SEARCH_DEBOUNCE_MS);
  }

  return {
    query,
    setQuery,
    change,
    searchNow: () => request(query, 0),
    dismiss,
    open,
    data,
    loading: fetcher.state !== "idle",
  };
}

export function SearchSuggestions({
  id,
  data,
  loading,
}: {
  id: string;
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
      id={id}
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
                className="search-suggestion search-suggestion-profile"
                data-testid="search-suggestion"
                to={`/users/${encodeURIComponent(user.username)}`}
              >
                <span className="search-suggestion-player">
                  <strong>{user.displayName}</strong>
                  <small>@{user.username}</small>
                  <span className="profile-badge">Clash Lens profile</span>
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
                  {result.clan} ·{" "}
                  {result.trophies === null
                    ? "Waiting for Season reset"
                    : result.trophies.toLocaleString()}
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

const searchIcon = (
  <svg
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
);

/**
 * Compact search for every page but home: a quiet icon that opens a small
 * panel under the header (a full-width sheet on phones). Escape closes it.
 */
export function HeaderSearch() {
  const [expanded, setExpanded] = useState(false);
  const suggestions = usePlayerSuggestions();
  const toggleRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const navigate = useNavigate();

  useEffect(() => {
    if (expanded) inputRef.current?.focus();
  }, [expanded]);

  useEffect(() => {
    if (!expanded) return;
    function handlePointerDown(event: PointerEvent) {
      const target = event.target as Node;
      if (panelRef.current?.contains(target) || toggleRef.current?.contains(target)) {
        return;
      }
      collapse(false);
    }
    document.addEventListener("pointerdown", handlePointerDown);
    return () => document.removeEventListener("pointerdown", handlePointerDown);
  }, [expanded]);

  function collapse(returnFocus: boolean) {
    suggestions.dismiss();
    suggestions.setQuery("");
    setExpanded(false);
    if (returnFocus) toggleRef.current?.focus();
  }

  return (
    <div className="header-search">
      <button
        ref={toggleRef}
        type="button"
        className="header-search-toggle"
        aria-label="Search players"
        aria-expanded={expanded}
        aria-controls={expanded ? "header-search-panel" : undefined}
        title="Search players"
        onClick={() => (expanded ? collapse(false) : setExpanded(true))}
      >
        {searchIcon}
      </button>
      {expanded ? (
        <div
          ref={panelRef}
          id="header-search-panel"
          className="header-search-panel"
          onKeyDown={(event) => {
            if (event.key === "Escape") {
              event.preventDefault();
              collapse(true);
            }
          }}
          onBlur={(event) => {
            if (
              event.relatedTarget !== null &&
              !event.currentTarget.contains(event.relatedTarget) &&
              event.relatedTarget !== toggleRef.current
            ) {
              collapse(false);
            }
          }}
        >
          <form
            role="search"
            aria-label="Players and profiles"
            className="search-form"
            onSubmit={(event) => {
              event.preventDefault();
              const tag = normalizePlayerTag(suggestions.query);
              if (tag) {
                collapse(false);
                void navigate(canonicalPlayerPath(tag));
              } else {
                suggestions.searchNow();
              }
            }}
          >
            <label className="sr-only" htmlFor="header-search-input">
              Search players and Clash Lens profiles
            </label>
            <div className="search-controls">
              <input
                ref={inputRef}
                id="header-search-input"
                name="q"
                type="search"
                value={suggestions.query}
                placeholder="Search player, @username or #tag"
                autoComplete="off"
                autoCapitalize="none"
                enterKeyHint="search"
                aria-controls={suggestions.open ? "header-search-suggestions" : undefined}
                aria-describedby="header-search-help"
                onChange={(event) => suggestions.change(event.currentTarget.value)}
              />
              <button type="submit" aria-label="Search">
                {searchIcon}
              </button>
            </div>
            <span className="sr-only" id="header-search-help">
              Suggestions appear below as you type. Press Tab to reach them, or Escape to
              close the search.
            </span>
            {suggestions.open ? (
              <SearchSuggestions
                id="header-search-suggestions"
                data={suggestions.data}
                loading={suggestions.loading}
              />
            ) : null}
          </form>
          <button
            type="button"
            className="header-search-close"
            onClick={() => collapse(true)}
          >
            Close
          </button>
        </div>
      ) : null}
    </div>
  );
}
