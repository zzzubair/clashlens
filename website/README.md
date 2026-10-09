# Clash Lens website

This directory contains the self-hosted TypeScript SSR website. It talks to
the private Python API through one server-only client boundary.

The Live Leaderboard's Find your rank search accepts player names and tags.
A tag typed with `#` opens its row directly when that tag is on the board, or
links to that player's page when it is not; other searches show up to 20
tracked players whose name matches, or whose tag matches without the `#`, with
their whole-board ranks and trophies and a prompt to narrow larger results.
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
Known real players reuse their eligibility result on visits. Failed checks and
tags reported as not found offer a retry with the existing 30-second minimum
interval. Starting a check shares Refresh's six-per-minute allowance and unchanged
address handling. With JavaScript enabled, the page checks progress once a second
for up to 60 seconds, then explains that the check may still be running. A result
that arrives after that limit still replaces the waiting message. The Check
progress link also works without JavaScript. A tracked player whose newest
profile was rejected is not waiting, so the page explains why there are no
current results and rereads saved data once a minute while visible instead of
once a second. For a Legend I profile with Season ID 0 and no Legend battle
this Season it shows that profile's name, tag, clan and trophies, says the
player hasn't played a Legend League battle this Season, and shows no daily
log. A rejected newest profile is explained even when older results were
accepted; those stay as saved history, never as current trophies, until a
lookup shows an accepted profile again. Season 0
trophies stay out of search, the Live board and groups.

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
pages show the eligibility explanation or lookup error and any saved history.
Saved daily history combines available season, recent and current-day records
without duplicate dates. Records with unconfirmed season membership show Date
only instead of a season day number. This adds no weekly recheck or clan discovery
and does not enable production discovery. The [product map](../docs/product-status.md)
tracks the remaining launch work.

On a tracked player's current page only two things are live: the current
trophies next to the name and the Daily Legend log. Everything else uses
finished Legend days, the ones that ended at a 05:00 UTC Reset. The page has
no Trophy trend section; [`PlayerTrends.tsx`](app/components/PlayerTrends.tsx)
keeps that calculation for a later dashboard card. The player page explains
itself with titles, labels and short counts rather than help paragraphs.

The Daily Legend log shows, for each day, starting trophies, attacks, defenses,
trophy change, end of day and Reset rank. A finished day's end of day is its
start plus its trophy change plus any weekly or Season reset at its closing
Reset, which is the next day's start; it carries the day's Verified, Calculated
or Uncertain dot, and a next-day start that disagrees makes it Uncertain. A day
without its own change uses the next day's start. Under Defenses, a day with an
automatic defense loss at Reset shows it, such as -32 automatic loss, so
attacks minus defenses minus that loss equals the trophy change. A weekly or
Season reset is not part of the trophy change and shows under it, such as +80
weekly reset.
Today's end of day and Reset rank read After Reset.

Each Season shows one Season summary box. A past Season's box leads with its
official in-game final rank, highlighted as the standout number, and final
trophies. The current Season's box leads with the Clash Lens rank at the
latest Reset this Season, shown as Not ranked yet when that Reset's board has
no rank for the player, and trophies at the latest Reset, the end of day of
the day that Reset ended, with that day's dot. Before the Season's first
finished day, trophies read No finished day yet. Below that: hit rate, the
percentage of all attacks that got three stars; attacks and defenses by 3, 2,
1 and 0 stars; and trophies per day on offense and defense, per attack and per
defense. A note names stars of unknown battles only when there are some.
The Season trophy change and total trophies gained are not shown, because the
change is just ending trophies minus 5,000.

The current Season's box counts only finished Legend days, never the day in
progress, and can show This Season, Last 7 days or Last 14 days: the last 7 or
14 finished days, never days before the current Season. Early in a Season the
option says how many finished days so far, such as Last 7 days (2 so far), and
the box's one short line gives the dates and finished days saved. Counts,
stars, hit rate and averages all use every recorded battle of those days:
offense per day is trophies gained from attacks divided by the saved finished
days, battles or not, and defense per day is trophies lost on defense the same
way; per attack and per defense divide by those days' recorded battles.
Automatic Reset losses are not part of defense averages. Empty samples show
Unavailable for rates and averages. Partial history and conflicting reports
are flagged; retained past-Season totals cannot fill missing battle details. A
past Season's box uses its saved summary, with per-day averages over every
recorded day, and when coverage is partial it says how many of the 28 days are
recorded. A Season known only from in-game history shows just the rank, which
reads Not published yet until the game publishes it, and trophies.

With JavaScript enabled, changing the period uses battle details already loaded with
the player page and makes no request or additional database read. It adds no stored data; the
[saved-history limits](../docs/history-retention.md) still apply. The calculations
are in [`battle-statistics.ts`](app/lib/battle-statistics.ts).

The separate Older history table, closed until the visitor opens it, shows
saved finishes from January 2025 up to the Season that ended on 7 September
2026, newest first, with three columns:
Season ended, Global rank and Final trophies. Later Seasons are not listed.
The rank is highlighted as the standout number.
28-day Seasons are dated by their closing Reset; older calendar-month results
keep their month label. Saved in-game Legend I history wins for each Season,
even when a trophy count or rank is missing: those cells say "Not recorded"
instead of borrowing ClashKing's values. ClashKing fills Seasons with no saved
in-game row and gets one linked credit line. This table needs JavaScript and
loads after the rest of the page; absent or unavailable history hides only the
table. It is separate from the Seasons links that open Clash Lens's saved days.

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

The page says "Updated." only when the current Refresh reports complete. A
started Refresh that fails, becomes unavailable or can't be checked says it
couldn't refresh and that saved results are shown; a refused request shows only
the refusal reason. Each Refresh gets one minute from when it is submitted; after
that the page stops checking, says so, and ignores any later answer.

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

## Preview on Rogue

### Refresh address boundary

The intended production path is Cloudflare Tunnel → a local proxy → the website.
`npm start` and the website image run `server.ts`, which passes the real socket
peer to React Router. Manual Refresh and lookups share six requests per address
per minute; automatic profile refreshes get a separate three per minute in this
website process. Cookies, form fields, URL parameters and forwarding chains do
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
  `X-Forwarded-For`, `X-Real-IP` and `True-Client-IP`, and pin the public host.
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
tag, clan, current trophies, Live Leaderboard rank ("Unranked" when off the
board) and today's net so far with attacks and defenses done. The one profile
read returns every card. An account without current results also shows its
player page's explanation, with any saved trophies and today's battles from its
last accepted profile (for example after leaving Legend I), its rank whenever
it is on the board, and "Unknown" or "Not available yet" where nothing valid is
saved.

Usernames are fixed after signup. Both the website action and private API reject
rename attempts; display-name edits still work. The form directs username-change
requests to support. Account setup preserves names typed before the page's
JavaScript loads and validates the current form values when submitted.
Search accepts Clash Lens usernames (with or without `@`),
display names and linked Clash of Clans player names. Results expose only public
names, usernames and linked-account counts, never saved players or private groups.
Clash Lens profiles in suggestions and results have a tinted background and a
"Clash Lens profile" label so they are not mistaken for game players.
Every page except home, including error pages, has the same search behind a
header icon. Enter on an exact player tag opens that player; other
text shows suggestions in place without leaving the page.

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

Headings and big numbers use Lilita One; everything else uses Nunito, as decided
in [`../brand/README.md`](../brand/README.md). Both are WOFF2 files as published
by Google Fonts, split into Latin and Latin Extended so a browser only downloads
the part a page needs: 12 KB for Lilita One and 75 KB for Nunito, which is one
variable file covering weights 400 to 900. Other scripts fall back to the system
font. Their SIL Open Font Licences are in `public/fonts/OFL-LilitaOne.txt` and
`public/fonts/OFL-Nunito.txt`. The fonts are served by the site itself and add no
application or build dependency.

## Game art and brand icons

The browser tab icon, home-screen icons and web app icons use the Clash Lens CL
mark from [`../assets/`](../assets/). `public/favicon.ico` holds 32, 16 and 48
pixel images of `mark-cl-block.svg`, in that order. `public/apple-touch-icon.png`
and `public/apple-touch-icon-precomposed.png` are 180 × 180,
`public/apple-touch-icon-120x120-precomposed.png` is 120 × 120, and
`public/icon-192.png` and `public/icon-512.png` are listed in
`public/site.webmanifest`; all of these are `mark-cl-block-square.svg`, which fills
the whole square so phones can round the corners themselves. They were rendered
from the SVGs with headless Chrome. `public/images/og-clashlens.png` is the
1200 × 630 link preview image used by player pages, built from the wordmark.
All are served by the existing static-file handler, with no API calls or database
queries.

`public/images/legend-league.webp` is the Legend I tier badge from the official
API (`leaguetiers/326/s5Y12RDRg7tgznd2RwU9kgLbedC5Not4peiHfOaWfJo.png`, as saved
in a profile response on October 2, 2026), resized to 160 pixels high. It is used under the Supercell Fan Content Policy,
which the site footer links to.

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
