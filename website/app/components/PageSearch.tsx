import { useEffect, useRef, type ReactNode } from "react";
import { Form, useNavigation } from "react-router";

import { moveFocusWithArrows } from "./ArrowKeys";
import { SearchIcon, SearchSuggestions, usePlayerSuggestions } from "./PlayerSearch";

/**
 * The big player search on the front page and the search page. Suggestions
 * appear as you type; Enter opens the full results at /search. `results`
 * shows under the box while the box still holds the searched words.
 */
export function PageSearch({
  query = "",
  results,
}: {
  query?: string;
  results?: ReactNode;
}) {
  const suggestions = usePlayerSuggestions(query);
  const { setQuery } = suggestions;
  const searching = useNavigation().state !== "idle";

  useEffect(() => {
    setQuery(query);
  }, [query, setQuery]);

  // The browser can show the page and accept typing before its scripts start.
  // Once they take over, pick up anything typed then, or it sits in the box
  // with no suggestions.
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const typed = inputRef.current?.value ?? query;
    if (typed !== query) suggestions.change(typed);
  }, []);

  return (
    <div className="player-search-panel">
      <Form
        method="get"
        action="/search"
        role="search"
        className="search-form"
        onSubmit={suggestions.dismiss}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            suggestions.dismiss();
            inputRef.current?.focus();
          } else {
            moveFocusWithArrows(event, ".search-suggestion", inputRef.current);
          }
        }}
        onBlur={(event) => {
          if (!event.currentTarget.contains(event.relatedTarget)) suggestions.dismiss();
        }}
      >
        <label className="sr-only" htmlFor="player-search">
          Search players and Clash Lens profiles
        </label>
        <div className="search-controls">
          <SearchIcon className="search-field-icon" />
          <input
            ref={inputRef}
            id="player-search"
            name="q"
            type="search"
            value={suggestions.query}
            placeholder="Name, @username or #tag"
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
          Suggestions appear below as you type. Press the Down arrow or Tab to reach them,
          or Escape to dismiss.
        </span>
        {suggestions.open ? (
          <SearchSuggestions
            id="player-search-suggestions"
            data={suggestions.data}
            loading={suggestions.loading}
          />
        ) : null}
      </Form>
      {results && suggestions.query === query ? results : null}
    </div>
  );
}
