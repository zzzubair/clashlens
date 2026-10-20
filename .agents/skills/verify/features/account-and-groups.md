# Sign-in, accounts and Watchlists

A Clasher signs in with Google or Discord, picks a username, links their own Clash player by its in-game API token, and saves players into private Watchlists to compare them.

## Sub-features

- `signin-google` signs in through the fake Google.
- `signin-discord` signs in through the fake Discord.
- `account-setup` creates the account with a username and display name, then asks to link a player.
- `account-link` links a player with its API token and shows it on the public profile.
- `groups-first` makes a first watchlist from a player page's `Add to watchlist`.
- `groups-compare` opens `Compare players` for a watchlist, with a `14 days` view.
- `account-logout` signs out with `Log out`.

## How to get to it (user POV)

- Click the header's sign-in link, or open `/login`, then `Sign in with Google` or `Sign in with Discord`. A sign-in no account uses yet shows `No account found` with `Create account with Google` (or Discord). `/signup` signs up directly.
- Open `/account/verify-player` from the account pages, or `Link another player` on a Crew invite.
- On a player page, use `Add to watchlist`, which opens `/account/watchlists/add/<tag>`; or open `/account/watchlists`. Old `/account/groups...` links redirect there.

## Driving it with Playwright

Preconditions:

- Doctor passes. A fresh browser context per Clasher.
- Pick an unused fake identity per run, e.g. `fixture-google-subject-4001`, by rewriting `login_hint` on `**/authorize?*` (see `website/tests/e2e/account-isolation.spec.ts`).

- **Sign in.** `page.goto("/login")`, click `page.getByRole("link", { name: "Sign in with Google" })`. For a new identity the URL becomes `/login?none=google`; click `Create account with Google` and the URL becomes `/account/setup`.
- **Set up.** Fill `page.getByLabel("Username")` with `lensverify` and `page.getByLabel("Display name")` with `Lens Verify`, click `Create account`. The URL becomes `/account/verify-player`; open `/account` and the URL becomes `/users/lensverify` with the heading `Clash players`.
- **Link a player.** `page.goto("/account/verify-player")`, fill `Player tag` with `#2PP` and `API token` (`exact: true`) with `VERIFY-2PP`, click `Link player`, retrying for up to 30 seconds. The URL ends `?linked=%232PP` and the line `Synthetic Clasher 001 linked.` (or `#2PP linked.` before its name is saved) is shown.
- **Second view.** In a new, signed-out context, open `/users/lensverify`. The heading `Lens Verify` and `#2PP` are visible.
- **Watchlist.** Open `/account/watchlists/add/%232PP`, fill `Watchlist name` with `War plan`, submit. `/account/watchlists` lists a watchlist headed `War plan`, and its list `Players in War plan` contains `#2PP`.
- **Compare.** Click `Compare players`, then `14 days`. The URL contains `days=14`.
- **Log out.** Click `Log out`. The header shows `Sign in` again and `/account` shows `Your account` with `Create account` and `Sign in` (status 401).
- **Database view.** Count the saved watchlist (table `account_groups`) with `psql` (see `../SKILL.md` Drive). Save the output.
- **Proof.** Screenshot and accessibility snapshot after each step, the signed-out profile, and the `psql` output.

## Gotchas

- A username or identity is taken for the life of the stack. Launch starts empty, so reuse is only a problem within one run.
- A player can be linked to only one Clash Lens account. Use a different tag per account in the same run.
- An account holds at most 10 watchlists of 20 players. A full watchlist says so and cannot be chosen; that is correct behavior.
- The fake game's verify call is rate-limited; one failed `Link player` click is not a failure.
