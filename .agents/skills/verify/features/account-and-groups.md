# Sign-in, accounts and groups

A Clasher signs in with Google or Discord, picks a username, links their own Clash player by its in-game API token, and saves players into private groups to compare them.

## Sub-features

- `signin-google` signs in through the fake Google.
- `signin-discord` signs in through the fake Discord.
- `account-setup` creates the account with a username and display name.
- `account-link` links a player with its API token and shows it on the public profile.
- `groups-first` makes a first group from a player page's `Add to group`.
- `groups-compare` opens `Compare players` for a group, with a `14 days` view.
- `account-logout` signs out with `Log out`.

## How to get to it (user POV)

- Click the header's sign-in link, or open `/login`, then `Continue with Google` or `Continue with Discord`.
- Open `/account/verify-player` from the account pages, or `Link another account` on a Crew invite.
- On a player page, use `Add to group`, which opens `/account/groups/add/<tag>`; or open `/account/groups`.

## Driving it with Playwright

Preconditions:

- Doctor passes. A fresh browser context per Clasher.
- Pick an unused fake identity per run, e.g. `fixture-google-subject-4001`, by rewriting `login_hint` on `**/authorize?*` (see `website/tests/e2e/account-isolation.spec.ts`).

- **Sign in.** `page.goto("/login")`, click `page.getByRole("link", { name: "Continue with Google" })`. The URL becomes `/account/setup` for a new identity.
- **Set up.** Fill `page.getByLabel("Username")` with `lensverify` and `page.getByLabel("Display name")` with `Lens Verify`, click `Create account`. The URL becomes `/users/lensverify` and the heading `Clash players` is visible.
- **Link a player.** `page.goto("/account/verify-player")`, fill `Player tag` with `#2PP` and `API token` (`exact: true`) with `VERIFY-2PP`, click `Link player`, retrying for up to 30 seconds. The URL ends `?linked=%232PP` and the line `Synthetic Clasher 001 linked.` (or `#2PP linked.` before its name is saved) is shown.
- **Second view.** In a new, signed-out context, open `/users/lensverify`. The heading `Lens Verify` and `#2PP` are visible.
- **Group.** Open `/account/groups/add/%232PP`, fill `Group name` with `War plan`, submit. `/account/groups` lists a group headed `War plan`, and its list `Players in War plan` contains `#2PP`.
- **Compare.** Click `Compare players`, then `14 days`. The URL contains `days=14`.
- **Log out.** Click `Log out`. The header shows the sign-in link again and `/account` sends you to `/login`.
- **Database view.** Count the saved group with `psql` (see `../SKILL.md` Drive). Save the output.
- **Proof.** Screenshot and accessibility snapshot after each step, the signed-out profile, and the `psql` output.

## Gotchas

- A username or identity is taken for the life of the stack. Launch starts empty, so reuse is only a problem within one run.
- A player can be linked to only one Clash Lens account. Use a different tag per account in the same run.
- An account holds at most 10 groups of 20 players. A full group says so and cannot be chosen; that is correct behavior.
- The fake game's verify call is rate-limited; one failed `Link player` click is not a failure.
