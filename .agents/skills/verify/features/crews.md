# Crews

A Clasher makes an invite-only Crew from their linked Legend League accounts, shares the invite link, and other Clashers join with some or all of their accounts. Anyone, signed out too, can look inside a Crew and its boards without seeing who owns it.

## Sub-features

- `crew-create` makes a Crew with a name, a number of places, and chosen accounts.
- `crew-invite` shows the invite link and how many places are open.
- `crew-join` joins from the invite with chosen accounts.
- `crew-link-from-invite` links another account from the invite and returns to it.
- `crew-public` shows the Crew and its live board to a signed-out visitor with no owner names and no invite code.
- `crew-manage` lets the owner change places (`Save size`) and remove an account (`Kick`).

## How to get to it (user POV)

- Open `/crews`, then `/crews/new`.
- Open an invite link `/crews/join/<code>`.
- Choose a Crew's name on a member player's page.

## Driving it with Playwright

Preconditions:

- Doctor passes. Two browser contexts, each signed in as a different fake identity with an account set up (see [Sign-in, accounts and groups](./account-and-groups.md)).
- The owner has linked `#Q00UU` and `#Q00UV`; the joiner has linked `#Q00V0` and `#Q00V2`.

- **Create.** As the owner, `page.goto("/crews/new")`, fill `Crew name` with `Night Owls` and `Places` (`exact: true`) with `10`, tick each account's checkbox (the `label` containing the tag), click `Create crew`. The URL becomes `/crews/<id>?invite=1` and the dialog `Invite to Night Owls` shows a `/crews/join/...` link and `8 places open`.
- **Join.** As the joiner, open the invite path. The heading `Night Owls` is visible. Tick both accounts and click `Join with 2 players`. The URL is the Crew page and `4 of 10 places` is shown.
- **Signed-out view.** In a new context, open the Crew page. The heading `Night Owls` (level 1), `Joining is by invite only.`, and the region `Players (4)` with all four tags are visible. `Invite`, `Members` and `Edit` are absent. The page HTML contains neither username nor the invite code.
- **Crew board.** Open `<crew page>/boards/live`. The heading `Live leaderboard` (level 1) is visible.
- **Proof.** Screenshot and accessibility snapshot after each step, plus the saved HTML check's result.

## Gotchas

- A player's page lists its Crew only once that read beats the page's other reads; poll for up to 30 seconds.
- Each tag can belong to one Clash Lens account. Use the tags above once per run.
- Account checkboxes are found through the `label` that contains the tag, not by position.
