# Player page

A Clasher opens any tracked player and sees their name, tag, current trophies, the Legend day card, the Daily Legend log of battles, the Season summary, and a Refresh button that asks for fresh data.

## Sub-features

- `player-open` opens the page from a direct link, search, or a leaderboard row.
- `player-canonical` sends a lowercase tag (`%232pp`) to the uppercase address (`%232PP`).
- `player-profile` shows the name heading, tag and `Current trophies`.
- `player-log` shows the `Daily Legend log` heading and the day's battles.
- `player-season` shows the `Season summary` region with `Hit rate`, `Defenses` and `Per defense`.
- `player-refresh` asks for fresh data with the `Refresh` button.
- `player-narrow` fits a 375-pixel-wide phone screen without sideways scrolling.

## How to get to it (user POV)

- Open `/players/%232PP` directly.
- Search `#2PP` from the header search (see [Search and leaderboards](./search-and-leaderboards.md)) and choose the result.
- Choose a player's name on the home page or tracked leaderboard.

## Driving it with Playwright

Preconditions:

- Doctor passes; `/players/%232PP` contains `Synthetic Clasher 001`.

- **Open.** `page.goto("/players/%232pp")`. The URL ends `/players/%232PP` and the heading `Synthetic Clasher 001` is visible.
- **Profile.** `page.getByText("#2PP", { exact: true })` and `page.getByText("Current trophies", { exact: true })` are visible.
- **Log.** `page.getByRole("heading", { name: "Daily Legend log", exact: true })` is visible.
- **Season summary.** In `page.getByRole("region", { name: "Season summary" })`, `Hit rate` text, the row header `Defenses` and `Per defense` text are visible.
- **Refresh.** Click `page.getByRole("button", { name: "Refresh", exact: true })`. The POST to `/resources/players/%232PP/refresh` answers `202`, the button reads `Refreshing…` and is disabled, and the region `Player refresh` holds the screen-reader-only status `Refreshing…`; nothing else on the page moves. Wait up to 30 seconds for that status to read `Updated.` and the button `Refresh` again; the time in `.player-updated` (its `datetime` attribute) is then the same or later than before. Screenshot while refreshing and after.
- **Phone width.** `page.setViewportSize({ width: 375, height: 812 })`, reload, then `document.documentElement.scrollWidth - document.documentElement.clientWidth` is `0`.
- **Proof.** Screenshot and `page.locator("main").ariaSnapshot()` after Open and after Refresh, plus the phone screenshot.

## Gotchas

- The page refreshes itself once on load when its data is old, so a manual Refresh right after load can be refused as too soon. A refusal shows an `alert`; that is correct behavior, not a failure. Say which one you saw.
- Refresh spends a per-visitor allowance. Do not click it in a loop.
- Battles and trophies change while collection runs. Do not assert exact numbers.
- Past Seasons and old Legend days (`?season=`, `?day=`) need history the fresh stack does not have. A recipe that needs them reports a missing precondition.
