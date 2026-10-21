# Dashboard

A signed-in Clasher with a linked player opens their dashboard, picks the ready-made layout or an empty one on the first visit, and sees cards such as the Legend day, Bases you attacked, Armies on defense and a goal. They can move, remove and add cards, and the layout stays.

## Sub-features

- `dash-welcome` offers `Use our layout` or an empty layout on the first visit.
- `dash-cards` shows the `Legend day`, `Bases you attacked` and `Armies on defense` cards.
- `dash-customise` opens `Customise dashboard`, adds a card from `Add a card`, and saves with `Done`.
- `dash-goal` sets a goal from `Goal settings`.
- `dash-bases-settings` changes the `Bases you attacked` settings, such as `3 days`.

## How to get to it (user POV)

- Open `/` (the `Dashboard` tab) after signing in and linking a player. The old `/dashboard` redirects there.

## Driving it with Playwright

Preconditions:

- Doctor passes. One signed-in context with an account set up, which has linked `#Q00VJ` (see [Sign-in, accounts and groups](./account-and-groups.md)).

- **First visit.** Open `/`, retrying for up to 60 seconds until the dialog `Your dashboard` appears (it waits for the player to be tracked). Click the button matching `/Use our layout/`. The dialog closes.
- **Cards.** The regions `Legend day`, `Bases you attacked` and `Armies on defense` (each `exact: true`) are visible.
- **Customise.** Click `Customise dashboard`, add a card from the `Add a card` dialog, click `Done`. `Your own layout` is shown.
- **Stays saved.** Reload `/`. The added card is still there and the welcome dialog does not reappear.
- **Proof.** Screenshot and accessibility snapshot after the first visit, after Done, and after the reload.

## Gotchas

- The welcome dialog appears only once the linked player is tracked; poll, do not fail on the first load.
- Legend day numbers need collected battles. Right after ready the cards can show their empty states; that is correct for a fresh stack, not a failure.
- `Customise dashboard` opens only after the first layout choice has saved.
