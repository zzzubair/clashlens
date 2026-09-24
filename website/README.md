# Clash Lens website

This directory contains the self-hosted TypeScript SSR website. It talks to
the private Python API through one server-only client boundary.

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

`check:browser-assets` fails if server-only client or secret markers enter the
browser bundle. End-to-end tests start the root development stack and exercise
the real Python application against loopback-only Clash, archive, Google, and
Discord fixtures. They never call Google, Discord, Supercell, production data,
or cloud storage.

## Runtime interface

The root deployment starts and recovers the website as part of `./ops up`.
The website has no database, collector, worker, archive, or admin secret. It
connects to the private API over the pod's loopback interface and publishes
only the configured host and port.

The application can use Google OpenID Connect when the deployment supplies
the login settings and protected secret files. Local issuer overrides are for
tests only; production requires an exact HTTPS public origin. See
`app.env.example` and [`docs/deployment.md`](../docs/deployment.md) for the
operator configuration.

## Preview on Rogue

`https://preview.clashlens.net` runs the current UI with real Google and Discord
sign-in against the saved preview database. The main `clashlens.net` site still
serves the coming-soon page. Google and Discord use the same application
credentials, with additional `/auth/google/callback` and
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

The HTTPS preview serves a production build from
`/home/zubair/development/clashlens-preview/https/build-20260922-provider-copy`.
The previous `https/build-20260922-public-profile` remains available for rollback.
Future updates need a
fresh build directory and a restart of only the preview website service after
its build mount is updated; never replace a build while it is serving requests.
Start the dependent preview proxy and tunnel again after that restart if needed.
The existing Tailscale preview on port 5173 still watches source changes.
Do not run the main stack's `ops up` to update this preview.

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

Usernames are fixed after signup. Both the website action and private API reject
rename attempts; display-name edits still work. The form directs username-change
requests to support. Search accepts Clash Lens usernames (with or without `@`),
display names and linked Clash of Clans player names. Results expose only public
names, usernames and linked-account counts, never saved players or private groups.

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
