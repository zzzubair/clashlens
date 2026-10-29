# Clash Lens website

This directory contains the self-hosted TypeScript SSR website. It talks to
the private Python API through one server-only client boundary.

The Live Leaderboard's Find your rank search accepts player names and tags.
A tag typed with `#` opens its row directly when that tag is on the board, or
links to that player's page when it is not; other searches show up to 20
tracked players whose name matches, or whose tag matches without the `#`, with
their whole-board ranks and trophies, noting when only the first 20 are shown.
Selecting a player loads their current page, highlights the row and scrolls it
into view, adding up to 5 players from the next or previous page when the row
sits at a page edge. The page is located again when
opened, so movement since the search cannot strand the player on an old page.
Search uses the same ordering and not-found exclusions as the board. It never
starts tracking an unknown player. [Validation and screenshots](../docs/find-my-rank-validation.md)
include measurements at 13,000 players.

Exact-tag search opens the player page and starts an unknown tag's first check
without a button or sign-in. The existing interactive collector retains the
profile, battle log and league history; existing profile processing activates
eligible Legend I players. Progress waits for processed evidence. Official
not-found, temporary failure and uncertain eligibility have distinct messages.
Known real players reuse their eligibility result on visits. Failed checks offer
a retry with the existing 30-second minimum interval. A tag reported as not found
offers a search box holding that tag instead, so a typo can be fixed; sending the
same tag back asks the game again, with the same minimum interval. Starting a check shares Refresh's six-per-minute allowance and unchanged
address handling. With JavaScript enabled, the page checks progress once a second
for up to 60 seconds, then explains that the check may still be running. A result
that arrives after that limit still replaces the waiting message. The Check
progress link also works without JavaScript. A tracked player whose newest
profile was rejected is not waiting, so the page explains why there are no
current results and rereads saved data once a minute while visible instead of
once a second. A Legend I profile at 5,000 with no Legend battle this Season,
accepted or naming Season ID 0, whatever Season it names, needs no explanation: the
player has not played yet, so their normal page shows them at 5,000 with a
daily log of 0/0 days from 5,000 to 5,000, and every board ranks them at
5,000. Another Legend I profile with Season ID 0 and no Legend battle this
Season shows that profile's name, tag, clan and trophies and says the player
hasn't played a Legend League battle this Season. A rejected newest profile
is explained even when older results were accepted; those stay as saved
history, never as current trophies, until a lookup shows an accepted profile
again. Season 0 trophies stay out of search and groups.

Player profiles reread their own data in the background: these progress checks
and after Refresh, including when a phone wakes mid-wait. When one of those rereads
cannot reach the website, because the phone is offline, the connection drops
mid-answer or a proxy answers with its own error page, the profile keeps showing
what it had and a later reread tries again; only a profile not yet shown can fail
with the error screen. Refresh and search show their own unavailable notice
instead. Nothing is copied aside for this: the header shows signed out until the
next reread reaches the website, and account pages still show the error screen.
The full route list ships with each page, so browsing never needs an extra
route lookup that could fail on its own.

Name results include active players or players with recorded history. Empty
day records marked `player_not_eligible` do not count as history unless they
contain battles. A full current profile and Refresh appear only when the displayed
player response itself confirms active tracking, including after Refresh. Other
pages show the eligibility explanation or lookup error and any saved history. A
player outside Legend I, or not confirmed in it, is named by its newest saved
profile, with that profile's league when known, and shows no current Season
unless it has a saved Legend day this Season.
Saved daily history combines available season, recent and current-day records
without duplicate dates. Records with unconfirmed season membership show Date
only instead of a season day number. This adds no weekly recheck or clan discovery
and does not enable production discovery. The [product map](../docs/product-status.md)
tracks the remaining launch work.

On a tracked player's current page only two things are live: the current
trophies next to the name and the Daily Legend log. Everything else uses
finished Legend days, the ones that ended at a 05:00 UTC Reset. The page has
no Trophy trend section; for the unused
[`PlayerTrends.tsx`](app/components/PlayerTrends.tsx) see open item 6 in the
[dashboard design](../docs/dashboard.md). The player page explains
itself with titles, labels and short counts rather than help paragraphs.
The player's name in its heading opens their profile in Clash of Clans. The
heading first shows the saved clan name as plain text, then the clan the game
reports right now, linked to its profile, streams in to replace it; when the
game gives no answer the saved name stays, without a link
([live clan reads](../docs/operating.md#clash-api-keys)).

The Daily Legend log shows, for each day, starting trophies, attacks, defenses,
trophy change, end of day and Reset rank. A finished day's end of day is the
next day's start, from any saved record of that day, even one the log leaves
out, such as a day after the player left Legend I; it carries the day's Verified, Calculated or Uncertain dot.
Only the latest finished day, with no next day saved yet, uses its start plus
its trophy change plus any weekly or Season reset at its closing Reset; an
older day without the next day's start shows Unavailable. When that sum
disagrees with the next day's start, the end shows the next day's start and
the whole day is Uncertain, its badges and explanation both, which says so,
such as Battles add up to 5,050; next day started at 5,992, or, with a
reset, Battles add up to 4,968, which the weekly reset takes to 5,000; next
day started at 4,968. Trophies at last Reset follows the same rule. Under Defenses, a day with an
automatic defense loss at Reset shows it, such as -32 automatic loss, so
attacks minus defenses minus that loss equals the trophy change. A weekly or
Season reset is not part of the trophy change and shows under it, such as +80
weekly reset.
Today's end of day and Reset rank read After Reset, and the battles it has
not saved yet are counted in one line, such as 7 attacks not recorded yet,
where a finished day's say 7 attacks not recorded. Today's day sits first in
the log on every screen and starts folded. On a phone an opened day shows its
attacks or its defenses, picked with an Attacks | Defenses switch that carries
each side's total; wider screens show both side by side.

Each Season shows one Season summary box. A past Season's box leads with its
official in-game final rank, highlighted as the standout number, and final
trophies. The current Season's box leads with the Clash Lens rank at the
latest Reset this Season and trophies at that Reset, the end of day of the
day it ended, with that day's dot. Until that Reset's board ranks the player,
as between the Reset and the board deadline, both come from the Reset before
it; when that Reset has no rank either, or was last Season's, the rank reads
Not ranked yet. Before the Season's first finished day, trophies read No
finished day yet. Below that: hit rate, the
percentage of all attacks that got three stars; attacks and defenses by 3, 2,
1 and 0 stars; and trophies per day on offense and defense, per attack and per
defense. A note names stars of unknown battles only when there are some.
The Season trophy change and total trophies gained are not shown, because the
change is just ending trophies minus 5,000.

The current Season's box counts only finished Legend days, never the day in
progress; its one short line gives the dates and finished days saved. The box's
Season menu, in the same bold style as the Analytics filters, lists the current
Season and each past Season with Clash Lens days and opens the chosen one. Counts,
stars, hit rate and averages all use every recorded battle of those days.
Offense per day is trophies gained from attacks divided by the days with at
least one recorded attack, and defense per day is trophies lost on defense
divided by the days with at least one recorded defense, so shielded or unplayed
days don't pull them down; per attack and per defense divide by the recorded battles.
Automatic Reset losses are not part of defense averages. Empty samples show
Unavailable for rates and averages. Partial history and conflicting reports
are flagged; retained past-Season totals cannot fill missing battle details. A
past Season's box uses its saved summary, with per-day averages over the
recorded days with a battle on that side, and when coverage is partial it says how many of the 28 days are
recorded. A Season known only from in-game history shows just the rank, which
reads Not published yet until the game publishes it, and trophies.

The box adds no stored data; the
[saved-history limits](../docs/history-retention.md) still apply. The calculations
are in [`battle-statistics.ts`](app/lib/battle-statistics.ts).

The separate Older history table, closed until the visitor opens it, shows
saved finishes from January 2025 up to the Season that ended on 7 September
2026, newest first, with three columns:
Season, Global rank and Final trophies. Later Seasons are not listed.
The rank is highlighted as the standout number.
28-day Seasons are named by their first and last Legend days, such as
13 Jul – 9 Aug 2026, and older calendar-month results keep their month label. Saved in-game Legend I history wins for each Season,
even when a trophy count or rank is missing: those cells say "Not recorded"
instead of borrowing ClashKing's values. ClashKing fills Seasons with no saved
in-game row and gets one linked credit line. This table needs JavaScript and
loads after the rest of the page; absent or unavailable history hides only the
table. It is separate from the Season menu that opens Clash Lens's saved days.

Opening a tracked player's profile with JavaScript enabled automatically submits
the existing Refresh request once when the server reports its saved check is
more than 60 seconds old. Saved data stays visible while the existing Refresh
flow runs, including if the request is refused. Manual Refresh and
browser-reload Refresh still work.

Automatic refreshes have their own allowance of three attempts per visitor per
minute. Once spent, further automatic attempts quietly keep the saved profile and
its check time on screen. They never spend the six-per-minute allowance shared by
manual Refresh, browser-reload Refresh and new-player lookups. Both counters use
the same trusted visitor address, each retains at most 10,000 addresses, and both
reset when the website process restarts.

Three automatic checks normally fetch six responses per minute, averaging 0.1
request starts per second per visitor. The interactive key defaults to 25 starts
per second, with one reserved for verification, leaving 24 for collection. This
small automatic allowance limits incidental browsing traffic while keeping that
shared key protection, the 30-second per-player cooldown and reuse of active
refresh work unchanged. It is a per-visitor bound, not a guarantee of spare
capacity across all visitors; the shared key limit still controls aggregate load.

When any Refresh reports complete, the page reloads its data immediately and
again about 3 and 8 seconds later. The player carried by the completed Refresh
stays on screen only while its profile check is newer than the reloaded data, so
battles processed after the profile still appear. Known limitation: complete
means both API responses were saved, not that the worker has processed them, so
processing that takes longer than about 8 seconds appears only after the next
Refresh or page load.

A Refresh shows in place: while it runs, the Refresh button reads Refreshing…
and can't be pressed, and nothing on the page moves. Only screen readers hear
"Refreshing…" and, when the current Refresh reports complete, "Updated.". A
started Refresh that fails, becomes unavailable or can't be checked says it
couldn't refresh and that saved results are shown; a refused request shows only
the refusal reason. Each Refresh gets one minute from when it is submitted; after
that the page stops checking, says so, and ignores any later answer.

The Dashboard is `/` for signed-in users while
`CLASHLENS_DASHBOARD_ENABLED=true`; the old `/dashboard` address redirects
there. Everyone else, and everyone while the switch is off, gets the front
page at `/`: the big search, then, only while the switch is on, a locked
example dashboard with Create account and Sign in
([`DashboardTeaser.tsx`](app/components/DashboardTeaser.tsx)), then the top 10
now with a link to all rankings ([`FrontPage.tsx`](app/components/FrontPage.tsx), see
[deployment](../docs/deployment.md)). The top bar's tabs are Dashboard,
Notebook (signed in, with the Notebook on), Rankings, Analytics, Crews,
Watchlists and Blog. Below 64rem, and on touch screens, they move to a bar at
the bottom of the screen: Home (Dashboard when signed in with the Dashboard
on), Rankings, Analytics, Crews and More, which holds Watchlists, Blog and Join
Discord. Signed in with the Notebook on, Notebook takes Crews' place in the bar
and Crews moves into More; the top bar keeps the logo, search,
the theme switch and Sign in or the account menu. A signed-out visitor who
opens a page that needs an account sees, in its place, what the page is for and
separate Create account and Sign in buttons that both come back to it
([`NeedsAccount.tsx`](app/components/NeedsAccount.tsx)). A sign-in with Google
or Discord that has no account yet says "No account found" and asks before
making one; a new account chooses a username, links a player, then goes on to
where the Clasher was heading. "Not you? Use a different sign-in" on Choose your
username logs that sign-in out and starts the sign-up again. The card designs
are in the [dashboard design](../docs/dashboard.md).

Signed-in users get one page: account tabs for their linked players, then a
divider with "⚙ Customise dashboard" and a
3-column grid of Small (1 column), Medium (2) and Large (full row) cards; empty
grid slots stay empty. The cards follow the selected tab, and one layout serves
every account. An account outside Legend I gets the Legend I only message
instead of cards and Customise; one not tracked yet gets the player page's
status message; with no linked player the page shows how to link one. On a
phone every card is full width, the Customise bar stays at the bottom and
dialogs open as bottom sheets.

All 13 cards are built. Each card reads saved player pages, tracked
leaderboards and groups through the private API; a player page is read once
per load however many cards use it, and a card whose reads fail says it could
not be loaded without taking the other cards down. Switching account shows its
tab at once and each card fills in as its data arrives; a save keeps the cards
shown and reads again only those it changes or adds
([`dashboard.tsx`](app/routes/dashboard.tsx)). A page left open past the
Reset hides the day's numbers behind a new-day notice until it reloads.
While the Notebook is on, Armies on defense says "Base: Ring ›" for the base
the selected account runs today, opening its page; with none it says "Which
base? ›" and opens `/notebook?change=<tag>`, the Notebook with that account's
Change sheet open.

Bases you attacked rates each base Hard / Average / Easy in the mode its
settings choose. Held % compares the base's held share today with today's held
share among tracked Legend players at similar trophies, whose day started
within 20 trophies of the base's Reset trophies, widened until that holds 200
defenses but never past 300 either side, and at least 50 defenses (Average is
within 15 points). Without that comparison a base with 3+ defenses shows "–"
and "no comparison yet". Trophies, the default, compares the trophies it gave
up a defense today with what the same Legends at similar trophies lost a
defense (Average is within `BASE_TROPHY_BAND` trophies a defense) and shows the
total it lost today beside theirs over as many defenses, such as "lost −185 ·
avg −201 · in 6 defenses". Under 3 defenses the rating is too early, but
Trophies still shows the total so far beside theirs. Its ‹ Day 9 · today ›
button reads an earlier Legend day of this Season through
`/resources/players/{tag}/bases?day=N`
(`GET /v1/players/{tag}/bases?day=N` on the private API, signed-in users only,
behind the same switch), compared with every player's saved defenses that day,
and keeps each day it read until the page closes or another account tab is
chosen. It reads only the day where clicking stops for 0.4 s, and waits up to
15 s, as the private API reads one earlier day at a time.

The first visit offers "Use our layout" or "Build my own". Customise moves
(drag, or ↑ ↓), removes and adds cards, has Reset to default, and Done saves;
card settings and the watchlist picked on the Watchlist card save at once. A user is on
the default layout (`DEFAULT_LAYOUT` in
[`dashboard.ts`](app/lib/dashboard.ts)), which follows each release, or on
their own. Settings are saved in the account's existing 4,096-byte preferences
box, so there is no database change. The Legend clock uses the device's time
zone.

Legend day and Bases you attacked share one read of `GET /v1/players/{tag}/today`
on the private API, for the selected account only. Its board of every
tracked player's trophies and today's attack and defense counts is read in the
background at most once a minute and shared by every dashboard, so loading
the page never waits for it. Until today's first read finishes, or once the
newest is 10 minutes old, the Next Reset range and base comparisons are left
out and the page asks again up to 3 times, each waiting up to 4 seconds for
that read ([`api_dashboard.py`](../python/src/clashlens/api_dashboard.py)).
Legend day's projected place is asked for the same way while it is worked
out ([dashboard data](../docs/dashboard-data.md)).
Opponents' defenses come from their own published daily logs, so an untracked
opponent shows only your hit. Until the account's own log for today is saved,
Bases you attacked says so and Legend day shows – for its attack and defense
counts, rather than 0.

The army cards read `GET /v1/players/{tag}/armies?lens=defense|offense&days=N`
on the private API, through `/resources/players/{tag}/armies`, which their day
switches call (signed-in users only, behind the same switch). A finished day
counts only when its army records are complete (for Armies on offense, also
counted by the worker) and the player's newest saved daily log for it is
complete, and a finished Season's records are deleted 7 days after it ends, so
early in a Season, or for a player tracked only lately, a long window covers
fewer days and the card says how many have army data. A defense's army type is the attacker's army.
Armies on offense shows your hit rate with each army type, up to 3 and then
Other armies, for 3 days, 7 days or this Season's finished days (`days=season`), beside every tracked Legend player's hit rate
with that army in grey, from the worker's saved attack counts for each day
(about 35,000 rows a day), kept in the API's memory until the day is counted
again, so only a day not yet seen is read. Armies on defense also takes a
Custom 1–28 days and shows your held % and average trophies lost against
each army, each followed in grey by every tracked Legend player's against
that army over the same days, from the worker's saved defense counts for
each day (defenders' own reports, about 88,000 rows a day), kept in the
API's memory the same way. A defense whose attacking army could not be read
counts only in All defenses. Until the worker has counted every one of the
days the grey numbers show –.

The Notebook at `/notebook` stays hidden unless `CLASHLENS_NOTEBOOK_ENABLED=true`,
and needs sign-in. A base stands on its own with one base ID; a notebook only
points to it, so removing one from a notebook keeps the base and its results.
The page is this Season's: each verified account's base today, its record on
that run, and a strip of the Season's 28 Legend days with a block per run of
one base. Change picks the base from today or tomorrow, and Days lists every
day so far to fix which base was up then; a finished Season is locked. Each
starts from one account and only changes that account, and an account runs one
base a day. Bases can be set only on an account's Legend days (its weekly
league record says Legend I). Below, Bases lists this Season's bases with
search, a This Season or Retired filter with counts, and three sorts. A base
running when a Season ends carries over by itself; the rest stay on their
Season's page, opened from the Season picker (`/notebook?season=<id>`), where a
base's page brings it back. A base retires by hand or 30 days after its last
counted day or when it was added or brought back. A base's page at
`/notebook/bases/<base ID>` shows how it held (defenses not 3-starred) from
everyone who ran it over Last 3 days (the 3 newest counted Legend days), This
Season (the default) or All: its stars and their shares, how often each army
type 3-starred it, the same split by the defender's Reset trophies (under
5,400, 5,400–5,799, 5,800+), and Who ran it, one row per account per Legend day,
newest first with today so far, each opening that day on the player's page.
Who ran it lists only accounts of people in that notebook: yours, or on a
Crew's page its members'. Then each of your accounts with the days it ran,
days with no data, its note and link. Only its maker edits its
name, link and note. Every change saves at once through `/notebook/save` and
offers Undo, except the note. Records come from the worker's defense counts, so
a finished day counts after 07:00 UTC.

Each Crew has a Notebook too, at `/crews/<crew id>/bases`, linked as Bases from
the Crew's page and seen only by its members. It lists bases, with no account
rows: owners, admins and Uploaders add bases and retire or remove them (an
Uploader only the ones they made or added), and any member adds one to their
own notebook with Add to my notebook, where it stays if they leave. From a base
in your own notebook, Add to Crew puts it in a Crew where you can add bases.
One base keeps one record wherever it is; on a Crew's base page its owner,
admins and Uploaders also see how many Clashers copied it into their own
notebook. While the Notebook is on, only a
Crew's owner and admins make invite links, and they make or remove Uploaders
from Members.

The Notebook opens on Tomorrow, at `/notebook/tomorrow`, one tap from Your
bases (`/notebook`). For one of your accounts (a tab each) it shows about what
it will have at the Reset (live trophies plus the middle of the 200 imagined
finishes behind Projected place, or 5,000 before a Season's Day 1), how far
from that defenses count (at least ±40, wider where players are spread out,
0.6× on Mondays and 1.25× at weekends), and the armies that attacked players
there in the last 7 counted days. Every base you can reach, from your notebook
and every Crew you're in, is ranked by holds of 8: each army's share of those
attacks times the base's chance to hold it, added up, times 8, from defenses
weighted by how close the defender started and how recent they are, with 10
pretend defenses per army at the average base's rate. A base needs 24 defenses
near you (looking twice as wide if needed) to be ranked; fewer is an early
guess, and a base with no results is Not run yet. Run it from tomorrow picks it
for that account from tomorrow, adding it to your notebook first if needed.
The worker saves the armies by Reset trophies for the last 28 counted days in
`notebook_meta_counts` (migration 0135).

Crews at `/crews` stay hidden unless `CLASHLENS_CREWS_ENABLED=true`. Signed-in users
see their crews (at most 5) and can create one at `/crews/new` with a name, a
number of places and which linked accounts join. A crew's page at
`/crews/<id>` shows six boards: Live leaderboard, Top players, Top attackers,
Best defenders, Worst defenders and Highest streaks, the last four switched
between Today, Last 7 days and Season (the default). Best and Worst defenders
share one card with a Best | Worst switch. On a phone a row of tabs (Live,
Reset, Attack, Defense, Streaks) shows one board at a time. Each card shows its top 5
and links to the full board at `/crews/<id>/boards/<board>`, which also lists
who is not on it and why. Anyone, signed in or not, can open a crew's page and
its boards; below the boards it lists every account in the crew by name and
tag. Visitors never see which Clash Lens accounts are in it, its invite links,
or Invite, Members and Edit, and a player's page names the crews that player
is in, linking to each. Invite on a crew's page makes or reuses the clasher's
48-hour link and shows it with Copy and Share; a new crew opens with it. The
link, `/crews/join/<code>`, shows the crew to anyone; signed out it says an
account is needed, with separate Create account and Sign in buttons that come
back to it. Signed in, it shows which of the clasher's linked accounts can join,
and "Link another player" goes through
`/account/verify-player?return=/crews/join/<code>` and comes back. A link that
was turned off or ran out names its crew; any other says it doesn't work. Members at
`/crews/<id>/members` lets clashers add or remove their own accounts and leave,
the owner make or remove admins, and the owner or an admin kick accounts. Edit
crew at `/crews/<id>/settings` renames, resizes (never below the places in
use), turns off links, hands over and deletes; members only see that the owner
and admins change settings.

The Blog at `/blog` lists posts newest first, shows each at `/blog/<slug>` and
publishes an RSS feed at `/blog/rss.xml`. Posts are not in this repository: the
website reads them from a copy of the private blog repo on the server, named by
`CLASHLENS_BLOG_DIR`. [`blog/README.md`](blog/README.md) describes what a post
can contain; raw HTML in a post is removed.

## Requirements and setup

- Node.js 24 LTS and npm with the committed `package-lock.json`; and
- rootless Podman for the full local stack and browser tests.

```sh
cd website
npm ci
```

Start the product from the repository root:

```sh
./dev up
```

Production mounts `CLASHLENS_PYTHON_HMAC_SECRET_FILE` instead of using an
environment secret. It must contain one unpadded base64url value for exactly
32 bytes, with at most one final LF.

## Checks

```sh
npm run build:verify
npm run typecheck
npm run lint
npm run format
npm run test:unit
npm run test:e2e
```

Tag-lookup verification on September 30, 2026: all 377 website unit tests,
type checking, lint, formatting and the production build passed. The isolated
Rogue database run passed 47 Python checks across `test_player_lookup_postgres`,
`test_api_db_public_ops` and `test_collector_db_postgres`. After the browser run
exposed empty noneligible day records appearing in name search, the corrected
lookup tests passed all 16 cases and `tests/e2e/player.spec.ts` passed all seven
cases against 200 synthetic players. These cover automatic anonymous tracking,
progress, not-found, uncertain eligibility, non-Legend pages and name exclusion,
temporary failure, retries and the no-JavaScript path. A Chrome check of the
non-Legend page at 375 pixels found no horizontal overflow or console errors.

Local commands used Node 22 and locked Python 3.12; the browser fixture stack
used the repository's Node 24 container. No real Clash requests or production
changes were made. The full website browser suite, real providers, weekly
rechecks, clan discovery and production load were not checked by this task.

`check:browser-assets` fails if server-only client or secret markers enter the
browser bundle. End-to-end tests start the root development stack and exercise
the real Python application against loopback-only Clash, archive, Google, and
Discord fixtures. They never call Google, Discord, Supercell, production data,
or cloud storage.

When the browser tests start their own stack, Playwright waits for `dev` to print
`Clash Lens is ready at`, after the first player page and both login fixtures are
ready. The website health page alone does not prove those are ready. Playwright
sends SIGTERM on shutdown so `dev` can remove that stack and its disposable
database, archive, and spool volumes. With `CLASHLENS_E2E_EXTERNAL_STACK=1`, the
caller owns stack startup and cleanup instead.

## Runtime interface

The root deployment starts and recovers the website as part of `./ops up`.
The website has no database, collector, worker, archive, or admin secret. It
connects to the private API over the pod's loopback interface and publishes
only the configured host and port.

The application can use Google OpenID Connect when the deployment supplies
the login settings and protected secret files. Local issuer overrides are for
tests only; production requires an exact HTTPS public origin. See
[`docs/deployment.md`](../docs/deployment.md#production-configuration) for the
operator configuration.

### Search engines and the live address

The website answers by the request's host. While `CLASHLENS_PUBLIC_ORIGIN` is
anything but `https://clashlens.net`, such as the preview, every response
carries `X-Robots-Tag: noindex, nofollow` and `/robots.txt` disallows
everything. Once it is `https://clashlens.net`:

- Host `clashlens.net` may be indexed: no such header, and `/robots.txt`
  allows the public pages and names `/sitemap.xml`.
- Host `preview.clashlens.net` gets a permanent (301) redirect to the same
  path and query on `https://clashlens.net`.
- Any other host, such as a loopback health check, keeps the header.

`/sitemap.xml` lists the front page, Rankings, Analytics, Blog, Privacy and
every published post, at the public origin. Each of those pages has a title,
a description and a canonical address. An unknown address shows the normal
top bar with "Page not found", status 404 and a `noindex` tag.

## Preview on Rogue

### Refresh address boundary

The intended production path is Cloudflare Tunnel → a local proxy → the website.
`npm start` and the website image run `server.ts`, which passes the real socket
peer to React Router. Manual Refresh and lookups share six requests per address
per minute; automatic profile refreshes get a separate three per minute, player
verification six, player-page live clan reads six (past that the page shows the
saved clan), and the search page and header search share sixty, all in this
website process. The private API separately lets each account start six player
verifications per minute. Cookies, form fields, URL parameters and forwarding chains do
not choose that address. Restarting the process resets its in-memory counters.
Without a valid connection address, Refresh returns `503 service_unavailable`
without requesting collection.

In the website itself, `CLASHLENS_TRUSTED_PROXY_IP` defaults to empty, meaning
no header is trusted. Set it to exactly the local proxy's socket address **as
observed inside the website container**, not a subnet or a number of hops.
IPv4-mapped IPv6 is normalized. Only Cloudflare's `CF-Connecting-IP` header supplies the visitor
address. Missing, duplicate, chained or malformed addresses fall back to the
socket peer. `CLASHLENS_TRUST_PROXY` is obsolete and ignored. The root `ops`
uses the [fixed production pod address](../docs/deployment.md#production-configuration),
which is where every connection to the loopback-published website port appears
from, and trusts that address when `app.env` leaves the setting unset. An empty
value trusts nothing.

When upgrading, set `CLASHLENS_TRUSTED_PROXY_IP=10.89.14.2` or remove the line
instead of keeping an old `10.89.*` pod address. `./ops up` refuses stale pod
addresses before stopping any service. If an older example left the setting
empty, make the same update to enable per-visitor Refresh allowances; an
explicit empty value is preserved.

Before starting production with proxy trust, the operator must:

- Restrict the local proxy to the Cloudflare Tunnel connection, and restrict the
  website to that proxy. Never trust an address shared with untrusted connections.
- Forward exactly Cloudflare's `CF-Connecting-IP`, for example nginx
  `proxy_set_header CF-Connecting-IP $http_cf_connecting_ip;`. Strip `Forwarded`,
  `X-Forwarded-For`, `X-Real-IP` and `True-Client-IP`. Pass the visitor's host
  only for `clashlens.net` and `preview.clashlens.net`, and refuse any other.
  Those headers never supply the Refresh identity.
- Verify the actual socket peer and header replacement through the deployed
  tunnel. Do not enable transforms or Workers that rewrite the visitor address.

Cloudflare documents the visitor-header behavior in its
[HTTP header reference](https://developers.cloudflare.com/fundamentals/reference/http-headers/).
The production tunnel/proxy configuration is outside this repository and still
needs that deployment verification. These changes do not alter it. The plain
`react-router dev`/`react-router-serve` adapters do not supply socket context;
Refresh returns that 503 response there. Use the built website with `npm start`
or `./dev up`.

### September 25 preview deployment

This section records the earlier separate preview. For current production
operation, see [Operating Clash Lens on rogue](../docs/operating.md).

On September 25, `https://preview.clashlens.net` was configured for real Google
and Discord sign-in against the saved preview database; completed-provider
proof is listed below. The main `clashlens.net` site served the coming-soon
page. Google and Discord used the same application credentials, with
additional `/auth/google/callback` and
`/auth/discord/callback` redirects on the preview origin.

The preview has a separate session-signing key. Credentials stay under
`/srv/clashlens-secrets` on Rogue and mount read-only into the website container.
Its environment file is
`/home/zubair/development/clashlens-preview/https/website.env`.
`CLASHLENS_ARMY_PREVIEW=true` enables the explicitly labelled saved battle
snapshot in a production build; leave it unset for the main deployment.

Five user services run the preview:

- `clashlens-preview-website`: built website on `127.0.0.1:15173`.
- `clashlens-preview-proxy`: HTTP gateway on `127.0.0.1:15174`; pins the public
  host and replaces forwarded client addresses with Cloudflare's visitor address.
- `clashlens-preview-tunnel`: Cloudflare connection for the preview hostname;
  readiness is available at `http://127.0.0.1:15175/ready` on Rogue.
- `clashlens-preview-api`: private API on the existing preview pod's port 8000,
  published only at `127.0.0.1:18000` on Rogue. Keeps the saved preview database.
- `clashlens-preview-api-egress`: private Squid connection gateway inside that
  same pod; permits only HTTPS connections to `api.clashofclans.com:443`.

Their definitions are in `/home/zubair/.config/containers/systemd/` on Rogue.
The backend services depend on the existing preview pod and database, which
must be running first. Their environment is in `https/api.env`, and their
source snapshot is in `https/api-src` under the preview directory. The old
`clashlens-browse-dates-api` container is retained stopped for rollback.
The preview collector remains stopped. Player ownership verification uses the
existing protected interactive API key; the gateway keeps encrypted requests
on Rogue's already-allowlisted outbound connection. No player token is retained.
Container request logging is disabled to avoid retaining sign-in callback codes.
Check service status and the website's `/healthz` endpoint for availability.

[PR #137](https://github.com/zzzubair/clashlens/pull/137) records the September 25
preview deployment of merged `8107609`, built at
`/home/zubair/development/clashlens-preview/https/build-20260925-performance-8107609`.
Its API source is `https/api-src-20260925-performance-8107609`; four changed
Python files match the merge, while five existing preview-specific files were
preserved. The preview API is therefore not claimed to be identical to main.

The preview's "Recent real battles" army view remains a fixed September 22
capture, not a growing live dataset. #137 records improved downloads and selected
query/render costs, along with remaining 390-to-980-pixel overflow in the opened
army breakdown, heavy-view stalls and untested physical devices/Safari/Firefox.
Carry these into #127/#130; do not treat preview checks as full-population proof.

Setup checks (22 September 2026): production build and browser-secret scan
passed; 93 login/account tests passed. Twelve page checks across phone, tablet
and desktop sizes found no page overflow or JavaScript errors. Provider-start
redirects and secure cookies passed for both providers; a real Google sign-in
created an account and loaded its account data. Discord completion, provider
linking and physical iPad/Safari testing remain to be verified. Army tests now
cover automatic filter changes and sortable table headings in the current UI.

Player-verification setup exposed a real response-format mismatch: Supercell
returns `tag`, `token` and `status`, while the client expected only `status`.
The client now checks the echoed tag/token against the request and removes
them before classification. All 31 verification tests pass, including five
new regression cases. A live deliberately-invalid token was correctly rejected
through the restricted gateway. The user then linked two real players; a read-only
database check confirmed both links on the `sloothy` account.
Python lint passed with the locked environment during checkpoint validation.
The earlier login accessibility scan reported one `region`
warning (content outside a page landmark), outside this setup's scope.

Account and public-profile update (22 September 2026): saved players and groups
have their own navigation tabs. The account link and `/account` open the user's
public `/users/:username` profile, including after successful player linking.
Only the owner sees edit, sign-in connection and account-linking controls.
The linking form includes the in-game API token instructions.
Each linked account on a profile is one card that opens its player page: name,
tag, clan, current trophies, Live Leaderboard rank (when off the board, the
league its newest saved profile names, or "Unranked" when none is known) and
today's net so far with attacks and defenses done. The one profile
read returns every card. An account without current results also shows its
player page's explanation, with any saved trophies and today's battles from its
last accepted profile, its rank whenever it is on the board, and "Unknown" or
"Not available yet" where nothing valid is saved. An account outside Legend I,
or not confirmed in it, shows its newest saved profile's trophies instead, as
its player page does.

Usernames are fixed after signup. Both the website action and private API reject
rename attempts; display-name edits still work. The form directs username-change
requests to support. Account setup preserves names typed before the page's
JavaScript loads and validates the current form values when submitted.
Search accepts Clash Lens usernames (with or without `@`),
display names and linked Clash of Clans player names. Results expose only public
names, usernames and linked-account counts, never private groups.
Clash Lens profiles in suggestions and results have a tinted background and a
"Clash Lens profile" label so they are not mistaken for game players.
Every page except `/search`, including error pages, has the same search behind a
header icon (or `/`). Enter on an exact player tag opens that player; other
text opens the full results at `/search?q=`.

Two bounded official profile requests fetched the names of `sloothy`'s linked
players. The normal collector code retained both raw responses in the preview
spool and the existing worker processed them. No collection loop was started.
Linked-account names now use the latest parsed identity even when the player's
Legend season/tier evidence is not publishable. That evidence remains classified
as conflicting; this change does not publish those profiles' Legend statistics.

Validation: 121 website tests and 16 distinct database/API tests passed, including
blocked rename attempts, allowed display-name edits and public-search privacy.
Type checking, targeted JavaScript/Python lint and the browser-asset build check
passed. Live search by `sloothy`, `@sloothy`, `Sloothyy` and `Sloothy 安` found the
public profile; clicking a suggestion opened its two linked accounts. Four live
profile widths and 16 isolated owner/profile-edit layout checks had no page
overflow. Owner controls were absent in the anonymous live browser. Signed-in
navigation/edit controls were checked with isolated render data; a real signed-in
browser roundtrip after this update and physical iPad/Safari remain untested.

Sign-in connections now explain that Google and Discord can open the same account,
with a visible reminder to keep at least one connection. The disabled final-unlink
button is associated with that explanation for assistive technology. Existing
server protection is unchanged. All 13 provider database/API tests passed,
including concurrent unlink attempts, alongside 58 account-route tests, type
checking, targeted lint and the production build. Twenty-four isolated layout
checks covered Google only, Discord only and both linked, at four widths in both
themes. A real Discord linking roundtrip remains untested.

The main deployment's configured verification gateway was not running during
this setup. Before enabling its real traffic, configure that gateway and deploy
the response fix there too. The preview and main databases currently maintain
separate rate-limit records for the shared interactive API key; review that
before running both environments with continuous real traffic.

## Container check

Daily logs prefer the recorded starting trophy count. A calculated value requires
complete battle coverage or all eight attacks and eight defenses, with matching
totals and timestamps. Incomplete evidence stays unavailable. Calculations from
the next day's start also require consecutive days within the same season.

The local fake-service stack enables `CLASHLENS_ARMY_PREVIEW` for browser tests of
the captured army data. `saved=1` selects the backend's saved publications instead.
The capture is a bounded generated JSON file, excluded from automatic formatting.

Build the separate website image with:

```sh
podman build --file Containerfile --tag clashlens-website:prototype .
```

The root repository `Containerfile` builds the Python asyncio collector; this
`website/Containerfile` builds the Node application.

## Font assets

Words use Inter and scoreboard numbers use Barlow Condensed (semi-bold and bold),
as decided in [`../brand/README.md`](../brand/README.md). Both are Latin-only WOFF2
files as published by Google Fonts and already shipped for the OBS overlay, so the
redesign added no font files: Inter is one variable file covering weights 400 to
900, and the two Barlow Condensed weights are one file each. Other scripts fall
back to the system font. The OBS overlay keeps Lilita One and Nunito (split into
Latin and Latin Extended) for its own look, and its other font choices are Inter
(Clean), Barlow Condensed (Esports), Rajdhani (Tech) and Bungee with Rubik for
smaller text (Arcade). Every font's SIL Open Font Licence is in the matching
`public/fonts/OFL-*.txt` file. The fonts are served by the site itself and add no
application or build dependency.

## Game art and brand icons

The browser tab icon, home-screen icons and web app icons use the Clash Lens CL
mark from [`../assets/`](../assets/). `public/favicon.ico` holds 32, 16 and 48
pixel images of `mark-cl-block.svg`, in that order. `public/apple-touch-icon.png`
and `public/apple-touch-icon-precomposed.png` are 180 × 180,
`public/apple-touch-icon-120x120-precomposed.png` is 120 × 120, and
`public/icon-192.png` and `public/icon-512.png` are listed in
`public/site.webmanifest`; all of these are `mark-cl-block-square.svg`, the CL
mark on a sky blue square, which fills the whole square so phones can round the
corners themselves. The manifest's Android crop-to-shape ("maskable") icon is
`public/icon-maskable-512.png`, from `mark-cl-block-square-maskable.svg`, whose
letters fit inside the centre circle Android never crops. They were rendered
from the SVGs with headless Chrome, each drawn at its exact size, with no
transparency. `public/images/og-clashlens.png` is the
1200 × 630 link preview image used by player pages, built from the wordmark.
`public/images/mark-cl-block.svg`, an unchanged copy of the CL mark, is the OBS
overlay's Powered by clashlens.net mark. All are served by the existing
static-file handler, with no API calls or database queries.

`public/images/legend-league.webp` is the Legend I tier badge from the official
API (`leaguetiers/326/s5Y12RDRg7tgznd2RwU9kgLbedC5Not4peiHfOaWfJo.png`, as saved
in a profile response on October 2, 2026), resized to 160 pixels high. It is used under the Supercell Fan Content Policy,
which the site footer links to.

`app/units/` holds official unit art from the Supercell Fan Kit
(fankit.supercell.com, checked on October 13, 2026), also used under the Fan
Content Policy: 159 images, 64 × 64 pixels, 348 KB in all, each named after the
unit's saved id (`troop:23` is `troop-23.webp`). The build gives them
fingerprinted names under `/assets/`, cached for a year. Tapping an attack or defense
on a player page opens it: the opponent's tag, a link to their Legend log, Copy
army, and that battle's army drawn from its Copy army code, using the unit names the player response sends once
per page. Units the Fan Kit has no art for, such as Noble Iron, show their name
instead.

The footer uses the exact notice in the policy's "Insert disclaimers" section,
checked on October 2, 2026. The URL is visible and linked. The root README uses
the same notice. Its browser test checks the complete rendered text, link, and
320-pixel layout in light and dark themes; the existing 12.48-pixel footer text
and wrapping remain unchanged.

Shared orange accent colours use `--cl-accent` and `--cl-accent-contrast` in
`theme.css` and the dark overrides in `appearance.css`. Both now hold the
brand's link and accent text colour for each theme, listed in
[`../brand/README.md`](../brand/README.md). All four values and every use were
preserved when renaming the former blue-named settings.

For that rename, full-page Chrome screenshots before and after changing only
the colour names showed zero changed pixels on both Home in light mode
(375 × 876) and Sign in in dark mode (375 × 812). These used the local website
with the data service unavailable and sign-in disabled. The final notice was also visually checked at 320 pixels in both themes, with
no horizontal overflow. All 496 unit tests, the footer browser test, type
checking, lint, formatting and `build:verify` passed on Node 22.23.2. Node 24,
the full browser suite, populated player pages and real providers were not
checked in this task. Nothing was deployed.
