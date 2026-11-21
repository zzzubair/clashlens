import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router";

import {
  canonicalPlayerPath,
  noMatchHint,
  possiblePlayerTag,
  searchedPlayerTag,
} from "../lib/player-tag";
import { MAX_SEARCH_QUERY_LENGTH } from "../lib/validation";
import type { PlayerSearchLoaderData } from "../lib/player-search-data";
import { moveFocusWithArrows } from "./ArrowKeys";
import { TrophyMark } from "./LeaderboardShared";

const SEARCH_DEBOUNCE_MS = 180;
export const SEARCH_TIMEOUT_MS = 10_000;

/**
 * Asks for suggestions for `query`. A failed, refused or slower than
 * `SEARCH_TIMEOUT_MS` request answers as Search unavailable; one cancelled
 * through `signal` answers null.
 */
export async function loadPlayerSuggestions(
  query: string,
  signal: AbortSignal,
  timeoutMs = SEARCH_TIMEOUT_MS,
): Promise<PlayerSearchLoaderData | null> {
  const controller = new AbortController();
  const cancel = () => controller.abort();
  signal.addEventListener("abort", cancel);
  const timeout = setTimeout(cancel, timeoutMs);
  try {
    const response = await fetch(
      `/resources/players/search?q=${encodeURIComponent(query)}`,
      { headers: { Accept: "application/json" }, signal: controller.signal },
    );
    return (await response.json()) as PlayerSearchLoaderData;
  } catch {
    if (signal.aborted) return null;
    return {
      query,
      search: null,
      error: { error: { code: "unavailable", message: "Search did not answer." } },
    };
  } finally {
    clearTimeout(timeout);
    signal.removeEventListener("abort", cancel);
  }
}

/**
 * Typed text plus the suggestions fetched for it. Nothing is requested until
 * the user types, and only after a short pause, so idle search boxes cost no
 * requests. Typing again, dismissing or leaving the page cancels the request
 * still running.
 */
export function usePlayerSuggestions(initialQuery = "") {
  const [query, setQuery] = useState(initialQuery);
  const [requestedQuery, setRequestedQuery] = useState("");
  const [answer, setAnswer] = useState<PlayerSearchLoaderData | undefined>(undefined);
  const [loading, setLoading] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const running = useRef<AbortController | null>(null);

  useEffect(
    () => () => {
      if (timer.current !== null) clearTimeout(timer.current);
      running.current?.abort();
    },
    [],
  );

  const { state, data } = suggestionsView(query, requestedQuery, {
    state: loading ? "loading" : "idle",
    data: answer,
  });

  function cancel() {
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = null;
    running.current?.abort();
    running.current = null;
    setLoading(false);
  }

  function dismiss() {
    cancel();
    setRequestedQuery("");
  }

  function start(trimmed: string) {
    const controller = new AbortController();
    running.current = controller;
    setRequestedQuery(trimmed);
    setLoading(true);
    void loadPlayerSuggestions(trimmed, controller.signal).then((result) => {
      if (running.current !== controller || result === null) return;
      running.current = null;
      setAnswer(result);
      setLoading(false);
    });
  }

  function request(value: string, delay: number) {
    cancel();

    const trimmed = value.trim();
    if (trimmed === "" || value.length > MAX_SEARCH_QUERY_LENGTH) {
      setRequestedQuery("");
      return;
    }

    if (delay === 0) start(trimmed);
    else timer.current = setTimeout(() => start(trimmed), delay);
  }

  function change(value: string) {
    setQuery(value);
    request(value, SEARCH_DEBOUNCE_MS);
  }

  function retry() {
    request(query, 0);
  }

  return {
    query,
    setQuery,
    change,
    dismiss,
    retry,
    open: state !== "closed",
    data,
    loading,
  };
}

/**
 * The suggestion list under a search box:
 *
 * - `closed`: nothing typed, dismissed, too long, or the typed text is still
 *   in its 0.18-second pause, so the last request was for other text.
 * - `searching`: the request for the typed text is running.
 * - `shown`: its answer is back: results, none found, or Search unavailable.
 *
 * Events: typing (which waits out the pause, then sends one request for the
 * trimmed text), the answer, and dismissing or choosing a result. An answer
 * for other text never shows. A failed search shows as unavailable until the
 * text changes. `usePlayerSuggestions` owns the typed and requested text.
 */
export type SuggestionsState = "closed" | "searching" | "shown";

export function suggestionsView(
  query: string,
  requestedQuery: string,
  fetcher: { state: string; data?: PlayerSearchLoaderData },
): { state: SuggestionsState; data: PlayerSearchLoaderData | undefined } {
  const normalizedQuery = query.trim().toLocaleLowerCase();
  if (requestedQuery.toLocaleLowerCase() !== normalizedQuery || normalizedQuery === "") {
    return { state: "closed", data: undefined };
  }
  const data =
    fetcher.data?.query.toLocaleLowerCase() === normalizedQuery
      ? fetcher.data
      : undefined;
  if (fetcher.state !== "idle") return { state: "searching", data };
  return { state: data?.search != null || !!data?.error ? "shown" : "closed", data };
}

export function SearchSuggestions({
  id,
  data,
  loading,
  onRetry,
  onSelect,
}: {
  id: string;
  data: PlayerSearchLoaderData | undefined;
  loading: boolean;
  onRetry?: () => void;
  onSelect?: () => void;
}) {
  const search = data?.search;
  const results = search?.results.slice(0, 5) ?? [];
  const users = search?.users.slice(0, 3) ?? [];
  const candidateTag = data ? (search?.exactTag ?? possiblePlayerTag(data.query)) : null;
  const unknownExactTag =
    candidateTag && !results.some((result) => result.tag === candidateTag)
      ? candidateTag
      : null;

  return (
    <div
      id={id}
      className="search-dropdown"
      role="region"
      aria-label="Player and profile search suggestions"
      aria-live="polite"
      aria-busy={loading}
      // Safari never focuses a tapped or clicked link, so the search box would
      // blur with nowhere to go and close this list before the tap lands. Keep
      // focus in the search box instead; Tab still reaches the links.
      onMouseDown={(event) => event.preventDefault()}
    >
      {loading && !search ? <p className="search-dropdown-status">Searching…</p> : null}
      {data?.error ? (
        <p className="search-dropdown-status">
          Search unavailable.{" "}
          {onRetry ? (
            <button
              type="button"
              className="button button-secondary"
              disabled={loading}
              onClick={onRetry}
            >
              {loading ? "Trying again…" : "Try again"}
            </button>
          ) : null}
        </p>
      ) : null}
      {!loading &&
      search &&
      results.length === 0 &&
      users.length === 0 &&
      !unknownExactTag ? (
        <p className="search-dropdown-status">
          No players or profiles found. {noMatchHint(search.query)}
        </p>
      ) : null}
      {results.length > 0 || users.length > 0 || unknownExactTag ? (
        <ul className="search-dropdown-list">
          {users.map((user) => (
            <li key={`user:${user.username}`}>
              <Link
                className="search-suggestion search-suggestion-profile"
                data-testid="search-suggestion"
                to={`/users/${encodeURIComponent(user.username)}`}
                onClick={onSelect}
              >
                <span className="search-suggestion-player">
                  <strong>
                    <bdi>
                      <Matched text={user.displayName} query={search?.query} />
                    </bdi>
                  </strong>
                  <small>
                    @<Matched text={user.username} query={search?.query} /> ·{" "}
                    {user.linkedPlayerCount} linked{" "}
                    {user.linkedPlayerCount === 1 ? "account" : "accounts"}
                  </small>
                </span>
                <span className="profile-badge">Clash Lens profile</span>
              </Link>
            </li>
          ))}
          {results.map((result) => (
            <li key={result.tag}>
              <Link
                className="search-suggestion"
                data-testid="search-suggestion"
                to={canonicalPlayerPath(result.tag)}
                onClick={onSelect}
              >
                <span className="search-suggestion-player">
                  <strong>
                    <bdi>
                      <Matched text={result.name} query={search?.query} />
                    </bdi>
                  </strong>
                  <small>
                    <Matched text={result.tag} query={search?.query} />
                    {result.clan ? (
                      <>
                        {" · "}
                        <bdi>{result.clan}</bdi>
                      </>
                    ) : null}
                  </small>
                </span>
                <span className="search-suggestion-meta">
                  {result.trophies === null ? (
                    "Waiting for Season reset"
                  ) : (
                    <>
                      <TrophyMark />
                      {result.trophies.toLocaleString()}
                    </>
                  )}
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
                onClick={onSelect}
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

/** `text` with the first run of the typed letters marked, ignoring case. */
function Matched({ text, query }: { text: string; query: string | undefined }) {
  const typed = query?.trim().replace(/^@/, "") ?? "";
  const lower = text.toLocaleLowerCase();
  // Some letters change length when lowered; leave those names unmarked.
  const start =
    typed === "" || lower.length !== text.length
      ? -1
      : lower.indexOf(typed.toLocaleLowerCase());
  if (start < 0) return text;
  const end = start + typed.length;
  return (
    <>
      {text.slice(0, start)}
      <mark className="search-match">{text.slice(start, end)}</mark>
      {text.slice(end)}
    </>
  );
}

/** The magnifier on every search box. */
export function SearchIcon({ className }: { className?: string }) {
  return (
    <svg
      className={className}
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
}

/**
 * Compact search for every page but /search: a quiet icon that opens a small
 * panel under the header (a full-width sheet on phones). "/" opens it from
 * anywhere except while typing in another field; Escape closes it.
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

  // The panel grows out of the search button, wherever the header puts it:
  // above the panel on a wide screen, under the phone's sheet. Measured from
  // layout, not the box on screen, which the opening animation shrinks.
  useLayoutEffect(() => {
    const panel = panelRef.current;
    const toggle = toggleRef.current;
    const parent = panel?.offsetParent;
    if (!expanded || !panel || !toggle || !parent) return;
    const button = toggle.getBoundingClientRect();
    const box = parent.getBoundingClientRect();
    const x = button.left + button.width / 2 - box.left - panel.offsetLeft;
    const y = Math.max(0, button.top + button.height / 2 - box.top - panel.offsetTop);
    panel.style.transformOrigin = `${x}px ${y}px`;
  }, [expanded]);

  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key !== "/" || event.defaultPrevented) return;
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      const target = event.target;
      if (
        target instanceof HTMLElement &&
        (target.isContentEditable || target.closest("input, textarea, select"))
      ) {
        return;
      }
      event.preventDefault();
      if (inputRef.current) inputRef.current.focus();
      else setExpanded(true);
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, []);

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
        aria-keyshortcuts="/"
        title="Search players (press /)"
        onClick={() => (expanded ? collapse(false) : setExpanded(true))}
      >
        <SearchIcon />
        {/* Shown beside the icon on wide screens with a mouse; the button's
            label already names it for screen readers. */}
        <span className="header-search-label" aria-hidden="true">
          Search
        </span>
        <kbd className="header-search-key" aria-hidden="true">
          /
        </kbd>
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
            } else {
              moveFocusWithArrows(event, ".search-suggestion", inputRef.current);
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
              const tag = searchedPlayerTag(suggestions.query);
              const words = suggestions.query.trim();
              if (tag || words !== "") {
                collapse(false);
                void navigate(
                  tag
                    ? canonicalPlayerPath(tag)
                    : `/search?${new URLSearchParams({ q: words })}`,
                );
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
                placeholder="Name, @username or #tag"
                autoComplete="off"
                autoCapitalize="none"
                enterKeyHint="search"
                aria-controls={suggestions.open ? "header-search-suggestions" : undefined}
                aria-describedby="header-search-help"
                onChange={(event) => suggestions.change(event.currentTarget.value)}
              />
              <button type="submit" aria-label="Search">
                <SearchIcon />
              </button>
            </div>
            <span className="sr-only" id="header-search-help">
              Suggestions appear below as you type. Press the Down arrow or Tab to reach
              them, or Escape to close the search.
            </span>
            {suggestions.open ? (
              <SearchSuggestions
                id="header-search-suggestions"
                data={suggestions.data}
                loading={suggestions.loading}
                onRetry={suggestions.retry}
                onSelect={() => collapse(false)}
              />
            ) : null}
            <button
              type="button"
              className="header-search-close"
              onClick={() => collapse(true)}
            >
              <kbd aria-hidden="true">Esc</kbd>
              Close
            </button>
          </form>
        </div>
      ) : null}
    </div>
  );
}
