# Search and leaderboards

A Clasher finds players by name or tag from the header search, sees the front page's top 10, and pages through the tracked leaderboard, where Find your rank jumps to a player.

## Sub-features

- `search-header` opens the header search and suggests players and Clash Lens profiles while typing.
- `search-submit` runs a full search on `/search?q=` and lists results.
- `front-top` shows `Top 10 now` (table `Latest saved standings`) on the signed-out front page at `/`.
- `board-pages` pages through `/leaderboards/tracked?view=live`.
- `board-find` finds a player's rank with the `Find your rank` search box, and `Reset` clears it.
- `board-out-of-range` handles a page past the end.

## How to get to it (user POV)

- Click `Search players` in the header on any page except `/search`.
- Open `/` signed out and use the big search, the `Rankings` button and the `Full rankings` link.
- Open `/leaderboards/tracked?view=live&page=1`.

## Driving it with Playwright

Preconditions:

- Doctor passes. Leaderboard rows appear only after the collector's first sweep of all 200 fake Clashers; allow up to a minute after ready.

- **Header search.** On `/blog`, click `page.getByRole("button", { name: "Search players" })`, fill `page.getByRole("searchbox", { name: "Search players and Clash Lens profiles" })` with `Synthetic Clasher 001`. The region `Player and profile search suggestions` lists `Synthetic Clasher 001`.
- **Search by tag.** Fill the same searchbox with `#2PP` and press Enter. The page shows the heading `Synthetic Clasher 001`.
- **Front page.** On `/` signed out, the heading `Top 10 now` and `page.getByRole("table", { name: "Latest saved standings" })` are visible and contain `#2PP`.
- **Full rankings.** Click the link `Full rankings`. The URL starts `/leaderboards/tracked`.
- **Find your rank.** On `/leaderboards/tracked?view=live&page=1`, fill `page.getByRole("searchbox", { name: "Find your rank" })` with `2pp`, click `page.getByRole("button", { name: "Search", exact: true })`. The `Live leaderboard table` region shows `#2PP`. Click the link `Reset`; the query is gone from the URL.
- **No match.** Search `NoSuchClasherForThisTest`. The page says nothing matched and shows no error.
- **Past the end.** Open `/leaderboards/tracked?view=live&page=999999`. The page offers `Go to page 1` and shows no error.
- **Proof.** Screenshot and accessibility snapshot after each step above.

## Gotchas

- Suggestions wait for typing to pause. Wait for the suggestions region, not a fixed delay.
- Right after ready, only `#2PP` may have a profile. Run search and board recipes a minute later, or poll until the table has rows.
- The `Search` button name collides with `Search players`; use `exact: true`.
