# Clash Lens verification map

What a Clasher can do on the website, and how to prove each part works on a stack started by the `verify` skill. Read this index, then use the matching feature file as the recipe.

## Starting state

- A fresh stack from `../SKILL.md` Launch: empty database, 200 fake Clashers, collection running against the fake Clash API.
- `#2PP` is `Synthetic Clasher 001` and is collected before the stack reports ready. Other fake tags the browser tests use: `#Q00UU`, `#Q00UV`, `#Q00V0`, `#Q00V2`, `#Q00V8` (crews), `#Q00VJ` (dashboard), `#Q00VP` (overlay).
- Doctor passes and its source fingerprint matches your working tree.
- Never drive a stack this run did not start.

## How to drive

- Throwaway Playwright scripts from `../SKILL.md` Drive, base URL `http://127.0.0.1:5173`.
- Find elements by role and accessible name (`getByRole`, `getByLabel`), as in `website/tests/e2e/`. No CSS positions.
- Each browser context is a new visitor. A signed-in recipe signs in inside its own context.
- Collection keeps running, so trophy numbers move. Assert labels, links and relationships, not exact values, unless a recipe says otherwise.
- The fake game's verify call accepts only a few calls a second. Retry `Link player` for up to 30 seconds.

## Proof and reporting

- A screenshot and accessibility snapshot after each action that matters, numbered in order.
- Zero page errors (`page-errors.txt` empty).
- Anything saved gets a second, read-only view: a reload, a signed-out view, or a `psql` count.
- Name the feature file and entry point with each piece of evidence.
- An entry point you could not reach is reported as not verified, with the command you ran and what was missing. Do not count it as verified through a different entry point.

## Feature file shape

Each file has an H1 title, one paragraph on what the Clasher sees, then four H2 sections in this order: `Sub-features`, `How to get to it (user POV)`, `Driving it with Playwright`, `Gotchas`.

## Features

- [Player page](./player-page.md): a player's profile, Legend day card, Daily Legend log, Season summary and Refresh.
- [Search and leaderboards](./search-and-leaderboards.md): header search, home page tables, the tracked leaderboard and Find your rank.
- [Sign-in, accounts and groups](./account-and-groups.md): Google and Discord sign-in, account setup, linking a player, groups.
- [Crews](./crews.md): making a Crew, inviting, joining, and the signed-out view.
- [Dashboard](./dashboard.md): first-visit layout choice, cards, customising and the goal.

Not mapped yet: Analytics Armies (`/analytics/armies`), OBS overlay (`/account/obs-overlay`, `/o/<link>`), Discord alerts (`/account/discord-alerts`), blog (`/blog`), and the Discord bot's `/stats`, which is not a website surface.
