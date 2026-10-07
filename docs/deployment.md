# Fedora operation

For daily health checks, responses to each alert and the restore entry point,
see [Operating Clash Lens on rogue](operating.md).

## Launch order agreed on September 27

Use [the go-live runbook](go-live.md) for approval points, exact manual-import
commands, simultaneous alerts and tracking startup, warm-up and safe stop.

The [product map](product-status.md) and [#140](https://github.com/zzzubair/clashlens/issues/140)
separate collection readiness from public website launch. Tracking must be
complete before the October 5 05:00 UTC season boundary, targeting a September 30
start, with 12,500 live players within a total known pool of 22,157 supplied
tags. Check known-player eligibility weekly at the Monday transition,
automatically from the October 12 Reset, reusing fresh live profiles and that
week's completed checks. Add any new list before tracking starts and the
October 5 list during collection, with one identity per tag. New tags need
their first check. Starting a cold import at 05:00 is not proof of complete
Day 1 data.

Before real traffic: prove backups/restores, delivered private Discord alerts,
repeated imports, accuracy and capacity for 12,500 live players plus weekly
known-player checks. #128 owns the bounded
real rehearsal; its resulting cost/storage evidence must be accepted before
ongoing collection. The complete public website and clan discovery need not
delay starting from supplied lists. Public release separately needs the real
domain/logins, full feature checks, invited testing and #131 handover.

Current commands below describe implemented behavior. The old bootstrap command
caps at 20,000 tags and rejects duplicate input and later new imports. Launch
lists will use the [verified manual operator procedure](manual-list-import-validation.md),
which preserves existing identities and queues checks through existing functions.
No reusable import feature is needed. Live import remains pending. Weekly
scheduling and reuse are implemented behind a
[switch](collector-polling.md#weekly-eligibility-switch); production
verification remains pending. For trial sizes and commands, see
[local development](../README.md#local-development).
For production key allocation and expansion, see
[Clash API keys](operating.md#clash-api-keys). #128 must measure the actual active
workload alongside the 22,157-tag known pool and weekly checks. Raising the live-player
trial limit or adding keys is not required merely because the known pool is
larger. Keep existing safeguards.

## Existing service lifecycle

`./ops` runs Clash Lens as rootless Podman containers managed by the user's
systemd service manager. The tracked files under `deploy/quadlet/` are
Quadlets: Podman turns them into ordinary system services. PostgreSQL, the
collector, private API, worker, and website are owned by one
`clashlens.target`, so they start together after reboot and stop together.

Stopping the pod applies one time limit to every container still running,
replacing each container's own limit. `clashlens.pod` sets that limit to
PostgreSQL's 85 seconds and gives the pod service 90 seconds before systemd
gives up, so a busy database finishes its shutdown checkpoint and last change-log
upload. Podman's default pod limit is 10 seconds.

On reboot, the host's system manager also limits how long the service
account's whole user service manager may take to stop. Fedora's
`user@.service` sets 60 seconds, which can still cut PostgreSQL short. The
services stop in order: the collector (up to 45 seconds), then the worker (its
60-second lease plus 15, so up to 75 seconds), then PostgreSQL (up to 90
seconds). That adds up to 210 seconds, so the host limit is 240 seconds with a
30-second margin. If `CLASHLENS_WORKER_LEASE_SECONDS` is raised, raise this
limit by the same amount. As the service account, run:

```sh
sudo mkdir -p /etc/systemd/system/user@$(id -u).service.d
printf '[Service]\nTimeoutStopSec=240\n' | sudo tee /etc/systemd/system/user@$(id -u).service.d/clashlens-stop.conf
sudo systemctl daemon-reload
systemctl show user@$(id -u).service --property=TimeoutStopUSec
```

The last command should print `TimeoutStopUSec=4min`.

Building and running are separate operations. `up` never builds or pulls an
image. `build` records the exact image IDs and a fingerprint of every source,
migration, and unit input; `up` refuses to mix those images with changed init
files. A new build is only staged: operator and recovery commands keep using
the last successfully started release until `up` promotes the new one.

Before it stops anything, `up` checks the release's images and secret files,
writes its settings and unit files into a scratch folder in the state
directory and deletes it again, so a value they reject stops `up` while the
old release runs with its own files. It then checks that the release
includes every migration the running database has already applied (otherwise
it refuses with `the database has migrations N that this release lacks`), and
tries the release's pending migrations on the running database in one
transaction that is rolled back, refusing with `a pending migration failed
when tried on the running database; nothing was stopped or changed` if one
fails. The trial waits at most 5 seconds for each lock, but once it has one it
holds it until the rollback, so a migration that rewrites a large table slows
the old release for as long as it takes. A migration that builds an index
concurrently cannot run inside a transaction and is only run for real. Only
after services stop does `up` write the settings, secrets and unit files in
place and apply the migrations, as before. A database that is not running yet
gets only the release check, after it starts. If `up` fails after it has
stopped services, it
leaves them stopped, records the deploy as failed and starts the alert
schedule again, so the alert check sends `A deploy failed and left Clash Lens
stopped` within a minute and retries delivery every minute; the next `up` that
succeeds clears it.
Each successful `up` keeps the release it replaced in `previous-release.env`
in the state directory, with that release's images and source revision.

To roll back, do not reverse a migration. Revert the code change on `main`
while keeping every migration file, then `./ops build` and `./ops up` that
revert. The previous release can read the database only if no migration since
it changed a table it reads; check the migrations listed between the two
revisions before choosing this. A worker-only or API-only change deploys with
`./ops up --keep-collector`, which restarts only the worker, API and website
and leaves the collector, database, pod and network running, as described
below.

## Clean Fedora fixture

Install Git and rootless Podman, clone this repository as the service account,
then run:

```sh
sudo dnf install git podman
./ops build --fixture
./ops up --fixture
./ops status
```

The fixture is explicit. It uses the same application images, PostgreSQL 18,
migrations, role separation, and initialization as production, with local
Clash, archive, Google, and Discord substitutes. It makes no official Clash
request and needs no cloud or OAuth credential. Its published endpoints are
loopback only: the website at `http://127.0.0.1:15174`, collector health at
`http://127.0.0.1:18081`, and login providers at ports 8011 and 8012.

`up` enables systemd lingering for the service account, using `sudo loginctl`
when needed, so rootless services run without an interactive login and return
after reboot. On a host where sudo is restricted, an administrator can run
`loginctl enable-linger SERVICE_ACCOUNT` first.

Check the actual reboot state after the machine returns:

```sh
./ops status
curl --fail http://127.0.0.1:15174/players/%232PP
```

To run the existing browser suite against this stack, build its existing check
image and set `CLASHLENS_E2E_ORIGIN=http://127.0.0.1:15174`. No separate test
harness is needed.

Stopping keeps PostgreSQL, spool, and fixture archive data, and disables the
target so it stays stopped after later reboots:

```sh
./ops down
./ops status
```

## Production configuration

The settings template is not in this public repository. It is kept private on
the server, next to the production settings, at
`~/.config/clashlens/issue92/app.env.example`. Copy it to `app.env` in the
checkout, replace every `CHANGE_ME`, and make it private:

```sh
cp ~/.config/clashlens/issue92/app.env.example app.env
chmod 600 app.env
```

Before starting production, follow the website's
[Refresh address boundary](../website/README.md#refresh-address-boundary) for
default proxy trust, upgrades from older settings, and required restrictions on
incoming connections.

Production uses the private network `clashlens-private` with address range
`10.89.14.0/24` and fixed pod address `10.89.14.2`. Fixture mode leaves both for
Podman to choose. `./ops up` refuses an existing network with a different range
before stopping any service. If it reports that mismatch, run `./ops down`,
remove only the network with `podman network rm clashlens-private`, then run
`./ops up` again. This keeps the PostgreSQL volume, spool and archive. Python 3
must be available on the host when a non-empty trusted proxy address is
configured, so `up` can validate it before changing services.

Create the configured spool as a dedicated directory owned by the service
account with mode 700. Keep it outside the account's home, checkout, ops state
and unit directories, and secret directory; `up` refuses overlapping paths
before stopping the current stack. Under
`CLASHLENS_API_KEY_HOST_DIR`, prepare the
[private key files](operating.md#clash-api-keys), plus the mode-600 HMAC file named
by `CLASHLENS_HMAC_SECRET_FILE`. `./ops` transfers their values into Podman's
private secret store. API keys are supplied to the collector as an environment
secret because its current CLI accepts `label=value` key pools; they are not
written to a unit, environment file, or process argument. The API receives
only the interactive key as a mounted file.

Set `CLASHLENS_OFFICIAL_API_PROXY_URL` to the fixed-address relay's HTTP origin
on Tailscale, reachable from inside the pod. Use an origin without credentials,
a path, query or fragment. `ops` supplies this setting to
both collection and private player-token verification. The standalone collector
defaults to direct access when this setting is empty; `--official-proxy-url`
overrides it. Explicit proxy settings ignore ambient `NO_PROXY`, and a relay
failure never falls back to a direct connection. Token verification retains its
existing requirement for a proxy outside local tests.

The archive credentials have separate duties. The collector credential creates
immutable raw responses and may read back the marker or one exact object to
prove a write; the worker credential can only read. Neither runtime credential
may list, overwrite, delete, or broadly browse archive objects.
The database also has separate collector, worker, and API roles, plus a
[raw-response cleanup](#raw-response-cleanup) role. The admin database URL
exists only as a short-lived Podman secret during fixture bootstrap or while an
operator explicitly handles a failed item.
Every container in the pod shares `127.0.0.1`, so the database image starts
PostgreSQL with [`deploy/postgres/pg_hba.conf`](../deploy/postgres/pg_hba.conf):
every network connection, the administrator's included, needs that role's
password. Only the database container's own Unix socket, which `./ops`, health
checks and backups use, skips the password. That socket lives in
`/var/run/postgresql` on the database container's own filesystem; pod
containers share the network, not files, and no unit or `./ops` command mounts
that directory into any other container. The cluster's own `pg_hba.conf` is
ignored, so a new or restored cluster cannot fall back to its password-free
defaults. `./ops up` stops if the running database reports any other
`hba_file`.

### Paris fixed-address relay

`clashlens-egress-paris` is a Scaleway DEV1-S in `fr-par-1`: two processor cores,
2 GiB memory, a 10 GB local boot disk and a 200 Mbps connection. Its server ID
is `11bee593-3b31-40c0-80b1-28e24614c9ec`. The reserved public IPv4 is
**163.172.188.40**, allocation `38ccd5b5-6185-46f9-8312-cd1720688832`.
Scaleway reports the allocation as non-dynamic; keep it allocated when rebuilding
the server. This address is what every
[configured Clash API key](operating.md#clash-api-keys) must allow.

The September 30 quote totals **EUR 10.56/month before tax** at 730 hours:
EUR 6.55 compute, EUR 3.65 IPv4 and EUR 0.36 for the 10 GB local disk. This fits
the approved EUR 11–15/month budget. Outgoing instance
traffic is included. At 150 requests/second, response sizes of 10, 25 or 50 KB
mean approximately 4.9, 12.3 or 24.6 TB/month with a 25% traffic allowance.
These are sizing assumptions, not measured successful response sizes.
See [instance pricing](https://www.scaleway.com/en/pricing/virtual-instances/)
and [the June 2026 IPv4 price update](https://www.scaleway.com/en/blog/a-transparent-update-on-scaleway-pricing/).
The [public product catalog](https://www.scaleway.com/en/developers/api/product-catalog/public-catalog)
quotes local disk product `/instance/volume/l_ssd/fr-par-1` at EUR 0.000049/GB/hour.

The private relay address is **100.122.10.22**, named
`clashlens-egress-paris.tail54c4a2.ts.net`, with tailnet tag `tag:clashlens-egress`.
The server runs Ubuntu 24.04, Docker and Tailscale. The relay code is in
`/opt/clashlens/egress-proxy`; generated configuration is in
`/root/.local/share/clashlens-egress-proxy`. `deploy/egress-proxy/deploy.sh`
runs Tinyproxy without root privileges, with a read-only filesystem, a 128 MiB
memory limit, 128 processes and threads, at most 96 connections, and warning
logs capped at three 10 MB files. It holds no API keys or response archive.
On 8 October 2026 it held 48 connections against a limit of 50 then, so a
slower API could have run it out.

The 96 connections are shared out:

| Caller | At most | Why |
| --- | ---: | --- |
| Collector | 64 | Six per key: up to nine regular keys and the interactive key. It refuses to start with settings above 64. Eight regular keys use 54. |
| Player verification in the API | 20 | Its calls run on the API's worker threads: 20 on rogue's 16 processor threads. |
| Operator commands (`probe`, `recover-discord`) | 1 each | Run by hand, one request at a time. |

That leaves at least 10 spare while a connection closes and another opens.
`python/tests/test_collector_proxy.py` sends this full load at once, 54
collector and 20 verification connections, through a stand-in relay that
enforces `MaxClients` from the configuration `deploy.sh` writes. The stand-in
is not Tinyproxy, so Tinyproxy's own process and memory limits are not tested.
Tinyproxy runs a thread per connection, hence 128 processes and threads.
A changed limit takes effect only when `deploy.sh up` is run again on the relay
(below).

The collector's `/metrics` shows its side. `clashlens_collector_api_connection_limit`
is the most connections it keeps open, so the most relay tunnels it holds, and
`clashlens_collector_api_requests_in_flight` how many of them a request is using.
`clashlens_collector_relay_tunnels_open` counts the tunnels it holds now: those
a request is using and idle ones kept open for the next request. A tunnel
counts from when its connection is fully set up (connected to the relay, the
relay's tunnel accepted and the encrypted handshake with the official API
done) until the collector closes it; an idle one the relay has closed stops
counting when next checked.
`clashlens_collector_relay_requests_total` counts requests sent through the
relay; `clashlens_collector_relay_admission_failures_total` those the relay
refused, closed unanswered as it does at its connection limit, or could not be
reached for, and `clashlens_collector_relay_timeouts_total`
those that ran out of time, including ones queued at a full relay. A
connection to the relay that runs out of time counts in both. Timeouts divided
by requests, using growth between two reads, is the timeout share.
`clashlens_collector_relay_reachable` is 1 after a request got through the relay
and 0 after one could not reach it; it appears after the first request.
In `clashlens_collector_requests_total`, the same failures show as
`outcome="proxy_failure"`, `outcome="proxy_timeout"` (the relay connection ran
out of time) and `outcome="timeout"` per endpoint.

The API's `/operatorz` shows player verification's side under
`player_verification_relay`: `tunnels_open` (each verification opens its own
tunnel, counted from when its connection is fully set up in the same way until
the verification call returns), `requests`, `timeouts`, `admission_failures` and
`reachable` (`null` before the first verification). Counts on both sides reset
when the process restarts.

Both tunnel counts approximate the relay's own connection count. The relay
counts a connection from when it is accepted, so while a connection is being
set up, or one that stalls during setup is waiting to time out, the relay holds
a slot these counts do not show yet.

The filter allows only `CONNECT api.clashofclans.com:443`, an encrypted tunnel
whose API certificate the caller still checks. Ordinary HTTP requests, other
hosts and other ports are rejected. The proxy binds only its private Tailscale
address on port 3128 and accepts only rogue's Tailscale address `100.115.149.49`.
The cloud firewall denies inbound traffic except UDP 41641 for Tailscale and
the restricted SSH administration rule. Password login is disabled. Tailscale's
host firewall chain accepts private-interface traffic before UFW rules;
Tinyproxy's client allowlist enforces rogue-only proxy access. A request from
another tailnet machine was rejected with proxy status 403.

The collector keeps its existing reusable connection pool, request-start limits,
certificate checks, request deadlines and cancellation behavior through the
relay. A failed connection does not quarantine a key. A genuine API 401/403
still does. An outage makes requests fail or time out; the existing stopped
tracker alert takes about 10–11 minutes without successful fetches. A relay
does not remove the dependency on rogue's home connection.

For recovery, connect from rogue with
`ssh -o HostKeyAlias=163.172.188.40 ubuntu@100.122.10.22`. The alias checks
the same SSH host key already verified at the public address. On the relay, inspect `systemctl status tailscaled docker`, `sudo docker logs
clashlens-egress-proxy`, and `sudo ss -lntp`. Restart only the relay container
after diagnosing it. Docker and Tailscale start on boot, and the container has
`unless-stopped` restart behavior. If the machine must be rebuilt, retain the
IPv4 allocation above, attach it to the replacement in the same zone, install
Docker and Tailscale, authorize the new Tailscale machine, and copy this checkout's
`deploy/egress-proxy` directory to `/opt/clashlens/egress-proxy`. Then run:

```sh
sudo env PROXY_LISTEN_IP=100.122.10.22 PROXY_CLIENT_IP=100.115.149.49 \
  /opt/clashlens/egress-proxy/deploy.sh up
```

For a replacement, substitute its actual Tailscale address and repeat the connectivity,
destination-filter and latency checks before changing callers. Never release
the public IPv4 as part of routine recovery. A replacement private address
requires updating both callers through `CLASHLENS_OFFICIAL_API_PROXY_URL`.

#### Measured relay behavior

On September 30, 2026, after rogue's temporary network outage ended, paired
HTTP/1.1 requests from rogue measured the following times in milliseconds.
The warm rows exclude the first connection; each run reused one connection for
all 300 measured requests. The cold runs opened 100 connections each and disabled
TLS session reuse. Median is the middle sample; p95 and p99 are the times within
which 95% and 99% of samples finished, using nearest-rank percentiles.

| Path | Samples | Mean ms | Median ms | p95 ms | p99 ms |
| --- | ---: | ---: | ---: | ---: | ---: |
| Direct, reused connection | 300 | 80.837 | 80.767 | 82.694 | 87.690 |
| Paris relay, reused connection | 300 | 93.590 | 93.492 | 95.504 | 99.727 |
| Direct, new connection | 100 | 242.892 | 240.870 | 246.028 | 324.346 |
| Paris relay, new connection | 100 | 348.790 | 378.425 | 387.203 | 388.355 |

The warm mean added **12.753 ms**, inside the proposed 60 ms allowance. New
connections added **105.898 ms** on average, so retaining connection reuse is
essential. Runs started at 19:56:26 UTC for warm requests and 19:57:27 for cold
requests, paced at five starts/second per path. All 802 requests completed
without network errors and returned the expected unauthenticated HTTP 403 with
a 59-byte `Missing authorization` response. Cold totals include connection and
encryption setup; they do not meet the 200 ms warm full-response target.

Separate 30-second runs used 24 warmed client slots with a shared ceiling of
120 request starts/second. The direct path completed 3,530 requests, 117.67
starts/second, with mean 80.645 ms and p95/p99 82.692/89.913 ms. The relay
completed 3,552 requests, 118.40 starts/second, with mean 92.935 ms and p95/p99
95.139/99.033 ms. All responses had the same expected 403 and 59-byte body;
there were no transport failures. Each path also made 24 warm-up requests.
The relay's observed container use during that run was 3.527 MiB of its 64 MiB
limit and 2.48% CPU, with 24 established proxy connections. These are one-time
resource samples, not long-term maxima.

A separate relay-to-rogue transfer sent 2 GiB in **81.842 seconds**, averaging
**209.91 Mbps** over Tailscale and SSH with compression disabled. An earlier
768 MiB transfer took 22.756 seconds, 283.12 Mbps, showing a short burst above
the advertised rate. Budget against the advertised 200 Mbps rather than the
burst result. This checks the private network's capacity, not transfer of real
API response bodies through Tinyproxy. Both transfers generated bytes from
`/dev/zero` and discarded them on rogue, without writing large files.

Rogue's `tailscale ping` and active peer address confirmed direct traffic to
`163.172.188.40:41641`, with 10–14 ms round-trip delay. The DERP fallback path
was not forced or benchmarked. Actual private-address binding, rejection of
another tailnet client, rejection of other destinations/ports and plain HTTP,
and automatic recovery of the proxy and Tailscale after reboot were verified.
The reserved public IPv4 remained unchanged across both provisioning reboots.

The load check measures transport of small unauthenticated responses, using
Python's standard HTTPS client with CONNECT tunneling and certificate checking;
it is not a successful collector workload. The collector's own connection reuse,
certificate rejection, proxy-down behavior, request pacing, timeout and
cancellation are covered by local tests with a real HTTPS origin and CONNECT
relay. Successful profile/battle response sizes, provider processing time,
day-long loss, full collection/storage load, Reset gaps and outage alerts remain
unverified. Do not treat the unauthenticated rate as proof of launch capacity.

#### Production switch-over, separate approval required

1. Confirm `tailscale ping clashlens-egress-paris` from rogue reports a direct
   connection. A result using DERP, Tailscale's public fallback relay, needs a
   new latency/throughput check. Verify an unauthenticated request through
   `http://100.122.10.22:3128` returns the official API's missing-authorization
   response with certificate checks enabled.
2. Add **163.172.188.40/32** to each regular key and the interactive
   key in the Clash developer portal. If this requires replacement key values,
   put them in the existing private key files. Do not print them or put
   them in a command line, this document or the relay.
3. Deploy the reviewed release to rogue through the existing release procedure.
   Set `CLASHLENS_OFFICIAL_API_PROXY_URL=http://100.122.10.22:3128` in its
   private `app.env`, then run `./ops build` and `./ops up`. This is the production
   restart step and has not been authorized or performed by the relay task.
4. Confirm successful collection and private token verification, healthy keys,
   and the relay's direct connection mode from inside the running pod's network.
   Measure full successful profile/battle responses under the intended workload:
   aim for at most 60 ms extra relay delay and 200 ms mean request time. Check
   per-player gaps across Reset separately. Unauthenticated probes do not prove
   these behaviors.
5. If checks fail, stop the switch and diagnose it. A return to direct collection
   requires a working home address on the keys and a separately agreed route
   for token verification; removing the proxy setting alone is not a recovery
   plan. Never allow an automatic direct fallback to an unapproved address.

### Production archive: Scaleway Object Storage

The raw-response archive is the Scaleway bucket `clashlens-raw-evidence` in
`nl-ams` (Standard Multi-AZ, private, versioning off). Three Scaleway IAM
applications each hold one API key:

- `clashlens-archive-collector` — IAM grants object read/write; the bucket
  policy narrows it to `GetObject` plus `PutObject` only when the
  `If-None-Match` conditional-create header is present, so it can never
  overwrite. Its key pair goes in `app.env` as `CLASHLENS_ARCHIVE_*`.
- `clashlens-archive-worker` — object read only. Its key pair goes in
  `app.env` as `CLASHLENS_WORKER_ARCHIVE_*`.
- `clashlens-archive-operator` — object delete only, for
  [raw-response cleanup](#raw-response-cleanup). Keep its key pair outside
  `app.env`, the checkout and the generated unit environment, as described there.

The bucket policy is an allowlist: anything not granted there is denied for
the scoped credentials, which is what makes list, delete, and unconditional
writes fail for the runtime identities. Bucket configuration changes (policy
updates, versioning checks) need the account's own credential; while the
policy is attached, even it cannot read bucket configuration, so remove the
policy, make the change, and re-apply it.

The marker object at `clashlens/archive-instance.json` pins the archive
identity. `CLASHLENS_ARCHIVE_MARKER_HASH` is the lowercase SHA-256 of its
exact bytes; `up` records the whole contract in `archive_instances` and
refuses to start against a changed one.

IAM API keys expire one year after creation (organization policy). Rotate
each pair by creating a new key on the same application, updating `app.env`,
running `up`, and deleting the old key once the stack is healthy.

Keep `CLASHLENS_GLOBAL_RANKINGS_ENABLED=false` until real collection is
approved; with it off and no tracked players, the collector makes no
official API calls at all.

Build from the checkout to be released, review the resulting commit, then run
the already-built release:

```sh
./ops build
./ops up
./ops status
```

The website and collector health endpoints bind to `127.0.0.1`; PostgreSQL and
the private API have no host port. Production discovery is on unless
`CLASHLENS_PLAYER_DISCOVERY_ENABLED=false`; see
[collector polling](collector-polling.md) for its limit and request cost.

`up` first checks configuration and existing resources without stopping services.
Once those checks pass, it disables and stops the whole target, except as described in
[when up restarts the collector](#when-up-restarts-the-collector).
It then starts PostgreSQL by itself, applies every missing numbered migration in order,
verifies the fixed archive contract, rotates the admin and runtime-role
passwords through standard input, and only then enables the application target.
A failed migration leaves application services disabled for the next reboot.
Re-running `up` applies only migrations
whose recorded version is absent.

### When up restarts the collector

`up` leaves the collector running when nothing it runs on changed, because a
restart empties its scheduling memory, such as the Season 0 wait in
[collector polling](collector-polling.md). The collector cannot outlive the
database, pod or network, so these four keep running, or restart, together.
They keep running only when all of these hold since the last successful `up`:

- the collector and PostgreSQL images have the same file layers and run
  settings (build labels such as the commit are ignored);
- their rendered service files, apart from the image line, their settings
  files, the pod, network and database volume files, and the migration files
  are unchanged;
- the contents of their secrets are unchanged, compared by hash and never
  printed;
- all four are running and the collector and PostgreSQL containers are healthy.

The collector's image holds all of `python/src`, so a release that changes
only worker or API code still has a different collector image, and a plain
`./ops up` restarts all four on any Python change. The narrow path for a
worker-only or API-only change is the manual `./ops up --keep-collector`,
which the person deploying chooses: it keeps the running collector image
instead of the new one. Every other condition above must still hold, so a
release that also changes the collector's or PostgreSQL's settings, service
files or secrets, the PostgreSQL image, or any migration still restarts all
four, on the new collector image. `up` cannot tell collector code from worker
code, so use it only when the release changes no collector code; the next
`up` without it starts the new collector image. Choosing the collector image
automatically is a recorded follow-up. `--keep-collector` and
`--restart-collector` cannot be combined.

Otherwise, or with `./ops up --restart-collector`, `up` restarts all four as
before. It prints which happened and why, for example `Restarting the collector
with the database, pod and network: changed collector settings.` The API,
worker, website, fixture services and timers always restart. Files are compared
whole, so even a comment-only edit restarts the four; that is deliberate, because
such edits are rare and a needless restart is safe. When the four keep running, an
`up` that fails after comparing them, for example because the website is unhealthy, stops only
the services it restarted and leaves the four running. While they keep running,
the active release records their running images, so their image
revision label can name an older commit than the release. The record of what
they run with is `kept-services.env` in the state directory; it holds only
hashes.

## Blog posts

The website reads blog posts from a copy of the private repo
`zzzubair/clashlens-blog` on the server. A timer pulls that repo every five
minutes with a read-only deploy key, `./ops` mounts the copy into the website
container read-only at `/blog`, and the website rereads it at most once a
minute. Publishing a post is a merge to the blog repo's `main`; it shows up
within about six minutes. The format is in
[`website/blog/README.md`](../website/blog/README.md).

Run these once, as the service account, from the checkout:

1. Make a key that can only read the blog repo, and register it as a
   read-only deploy key (approve the `gh` step from an account that owns the
   repo):

   ```sh
   ssh-keygen -t ed25519 -N '' -C clashlens-blog-sync -f ~/.ssh/clashlens-blog
   gh repo deploy-key add ~/.ssh/clashlens-blog.pub -R zzzubair/clashlens-blog \
     --title "rogue blog sync"
   ```

2. Clone the repo outside the checkout and make it readable by the website
   container's user:

   ```sh
   GIT_SSH_COMMAND='ssh -i ~/.ssh/clashlens-blog -o IdentitiesOnly=yes' \
     git clone --branch main git@github.com:zzzubair/clashlens-blog.git ~/clashlens-blog
   git -C ~/clashlens-blog config core.sshCommand \
     'ssh -i ~/.ssh/clashlens-blog -o IdentitiesOnly=yes'
   chmod -R a+rX ~/clashlens-blog
   ```

3. Pull every five minutes. Save as
   `~/.config/systemd/user/clashlens-blog-sync.service`:

   ```ini
   [Unit]
   Description=Pull Clash Lens blog posts

   [Service]
   Type=oneshot
   UMask=0022
   ExecStart=/usr/bin/git -C %h/clashlens-blog pull --ff-only --quiet
   ```

   and as `~/.config/systemd/user/clashlens-blog-sync.timer`:

   ```ini
   [Unit]
   Description=Pull Clash Lens blog posts every five minutes

   [Timer]
   OnBootSec=1min
   OnUnitActiveSec=5min

   [Install]
   WantedBy=timers.target
   ```

   then start it:

   ```sh
   systemctl --user daemon-reload
   systemctl --user enable --now clashlens-blog-sync.timer
   ```

4. Find the owner's sign-in, which is the only one that sees drafts. Replace
   `<username>` with the owner's Clash Lens username:

   ```sh
   podman exec --user postgres clashlens-postgres psql -X -At -d clashlens -c \
     "SELECT i.provider || ':' || i.provider_subject
        FROM account_provider_identities AS i
        JOIN clash_lens_accounts AS a ON a.id = i.account_id
       WHERE a.normalized_username = lower('<username>')"
   ```

5. Add both settings to `app.env`, using the full path of the copy and one or
   more of the lines printed above, comma-separated:

   ```sh
   CLASHLENS_BLOG_DIR=/home/<service-account>/clashlens-blog
   CLASHLENS_BLOG_OWNER=google:<subject>
   ```

6. Run `./ops up`. It restarts the website with the folder mounted.

Check it: `curl -s http://127.0.0.1:3000/blog/rss.xml` lists published posts
only, and `systemctl --user list-timers clashlens-blog-sync.timer` shows the
next pull. A failed pull leaves the last good copy in place; see it with
`journalctl --user -u clashlens-blog-sync.service`. A post that fails to parse
is left off the site, and `./ops logs website` says why. Leaving
`CLASHLENS_BLOG_DIR` unset keeps the blog empty; leaving `CLASHLENS_BLOG_OWNER`
unset hides every draft from everyone.

## PostgreSQL backups and recovery

Production builds add WAL-G v3.0.9 to the existing PostgreSQL 18 Alpine image.
The source archive is SHA-256 checked and compiled without libc dependencies.
This avoids changing the database's text ordering by switching to Debian.
Fixture builds still use the unmodified PostgreSQL image.

Backups are opt-in. After approving deployment, set these in private `app.env`:

```ini
CLASHLENS_BACKUP_ENABLED=true
CLASHLENS_BACKUP_ENDPOINT=https://ACCOUNT_ID.eu.r2.cloudflarestorage.com
CLASHLENS_BACKUP_PREFIX=s3://clashlens-pg-backup/rogue-pg18
CLASHLENS_BACKUP_CREDENTIAL_FILE=/srv/clashlens-secrets/clashlens-backup-r2.env
```

Use a fresh cluster prefix for a new database or a major PostgreSQL upgrade.
Never share it with a scratch database. WAL-G owns `basebackups_005/` and
`wal_005/` below that prefix. Leave bucket lifecycle deletion disabled.

The credential file contains only `AWS_ACCESS_KEY_ID=...` and
`AWS_SECRET_ACCESS_KEY=...`, without quotes. It must belong to the service
account with mode 600. `ops` parses it without executing it and mounts the
resulting JSON as a Podman secret readable only by PostgreSQL's OS user.
The collector, worker and website receive no backup credentials. Keep the
separate read-only recovery key outside runtime units, and keep an additional
protected copy off rogue so losing the host does not also lose recovery access.
Rotate credentials by replacing the file, running the approved `ops up`,
verifying backup and restore, then revoking the old key in Cloudflare.

The timer starts a full backup every 12 hours, at 00:00 and 12:00 UTC, well
away from the 04:00–07:00 UTC Reset work. Missed calendar runs are
caught up; restarting the stack does not add another full backup. Run
`./ops backup` once during the approved initial rollout. PostgreSQL continuously uploads
its change log, called WAL, with `archive_timeout=300`. A restore can target any
point in the last ten days, chosen on 2026-10-08 so a weekly tournament's
promotions and demotions leave about three spare days to notice and fix a
problem. A successful full backup
then prunes backups older than the newest backup completed before ten days and
one hour ago. It retains that backup, all newer backups and WAL, and, when that
backup is a WAL-G delta backup (only the pages changed since an earlier backup),
the full backup its chain starts from (`wal-g delete before FIND_FULL`). Only
full and delta backup names are accepted. Delta backups are not switched on.
Until such a backup exists, pruning deletes nothing. Extra manual backups cannot
shorten the recovery window. The one-hour margin covers scheduling and backup
duration; right after each prune this keeps about 21–22 full backups and about
10.5–11 days of WAL.

```sh
./ops backup                  # upload now, then apply age-based retention
./ops backup-status           # remote backup freshness and pending WAL health
./ops backup-prune            # deletion preview only
./ops backup-prune --apply    # apply the same retention rule after approval
./ops logs backup --since today
```

The existing operation lock prevents concurrent backup, deployment and cleanup.
A scheduled backup waits for an in-progress operation; a manually started backup
and backup pruning still fail immediately on contention. `down` stops the timer
and backup service. A failed upload never runs pruning. Backup commands reject
changes covered by the active release fingerprint before accessing backup
credentials or remote storage.
Release fingerprints use byte-ordered filenames so terminal and scheduled-service
language settings cannot make unchanged code appear different. Rebuild and deploy
after upgrading this check; do not edit a saved fingerprint or bypass the guard.
`backup-status` exits unsuccessfully for a failed service, inactive timer,
missing/unreachable remote backups, a newest full backup older than 24 hours, disabled
archiving, or completed WAL files waiting over ten minutes. No WAL activity during
an idle period is not itself failure. It also prints how many GB of WAL a restore
to now would replay after the newest backup, the part of a restore that grows
between backups; it does not fail on that number. `./ops alert-check` alerts on this command
and disk space: PostgreSQL retains unarchived WAL locally during a storage outage
and that queue is not capped by `max_wal_size`. Never delete unarchived WAL to
free space.

### Disk writes and change-log volume

On 2026-10-02 production wrote 14 MB/s from PostgreSQL and 18 MB/s from the
collector. The change log was 3.1 MB/s (271 GB/day, 127 GB/day after upload
compression); 87% of it was whole 8 KB page copies. Data checksums make
PostgreSQL copy each page into the change log the first time it changes after a
checkpoint, and the old 1 GB `max_wal_size` forced a checkpoint every three
minutes. The 128 MB page cache also wrote 8.6 MB/s of table pages as it evicted
them.

The PostgreSQL unit now sets `shared_buffers=2GB` (inside the 6 GB memory cap;
128 MB for fake-service runs; `./ops` refuses a `CLASHLENS_POSTGRES_MEMORY`
below twice the cache), `checkpoint_timeout=10min`, `max_wal_size=2GB` and `wal_compression=zstd`.
Commit flushing, full-page writes, checksums and archiving are unchanged. A
70-minute page-by-page replay of production's change log predicts 225 instead of
393 page copies per second; zstd shrinks each copy to about 37%. Expect roughly
1 MB/s of change log (about 85 GB/day) and 60–70 GB/day of backup uploads.
Crash recovery replays the change log written since the last checkpoint.
`max_wal_size` is a soft limit that heavy load or stalled archiving can exceed,
so 2 GB is the usual size, not a ceiling: on rogue 845 MB took 90 seconds plus
14 seconds to save, so 2 GB takes about four minutes. From 30 Sep to 8 Oct
2026 the most written between two of its 3,008 checkpoints was 1.1 GB, so a
crash just before a checkpoint finishes replays up to about 2.2 GB. A stopped
replay only starts again from the beginning, so no fixed time limit stops one:
the unit waits for the database as long as it takes (`TimeoutStartSec=infinity`),
and the regular health check, which kills the container after twelve failures
in a row, starts only once a startup check (`HealthStartupCmd`) finds the
database ready. Every 10 seconds until then, the startup check reads which
16 MB change-log file PostgreSQL's startup process is replaying, by its full
24-character name in that process's name, and stops the
database (an immediate shutdown, which systemd restarts) only when that file
has not changed for 10 minutes; at the measured speed a file takes about two
seconds. If it cannot see a replay at all, it waits rather than stop one.
`./ops up` waits with the unit. The alert check warns once the database has
been starting for five minutes; `./ops logs postgres` shows the replay's
progress.

The collector remembers, in memory, the used fields it last committed for each
player and endpoint. An ordinary response that matches them is recorded in the
database without being saved to the spool, so most of the roughly 97% unchanged
responses no longer reach it. Each saved response cost about 147 KiB of disk
writes, mostly the forced flushes that make it crash-safe. The unchanged check
holds no lock, so no other response waits behind it. Any other response (the
first per player and endpoint after a restart, a changed one, a reset or
work-bound one) is saved to the spool before its own database work, exactly as
before; so is a matching one the database does not accept as unchanged, or
whose check fails or is cancelled, or finds a row it needs held (see
[collector polling](collector-polling.md#spool-archive-and-rate-enforcement)).
No response waits on the database while
holding the shared lock, so a later response is saved before it waits for an
earlier one's commit; saved responses still commit in saved order. Restart
recovery finishes any already committed saved response first, then replays the
rest in the order they were received. A hard crash can therefore
lose only an unchanged sighting's seen time, poll count, and the sighting time
and retirement deadline it would have extended; no raw response or other kept
data is lost, and the next poll records the sighting again. The poll count can
also count one poll twice when an unchanged sighting commits but the
confirmation is lost and the response is then saved and recorded again; this is
an accepted trade-off. The memory record
holds one entry per player and endpoint polled since the collector started:
about 26,500 entries and 9 MB for 13,263 players.

### Restore into a separate database

Use the pinned backup image from the release manifest and the read-only key.
Create a temporary Podman secret containing the same JSON settings as the backup
configuration, substituting only the read-only credentials. Do not print it.
Use a new empty named volume, a unique container name, no production pod and no
published port. Run WAL-G as OS user `postgres`, mounting the secret at
`/run/secrets/walg.json` with uid/gid 70 and mode 0400.

1. Run `wal-g --config /run/secrets/walg.json backup-list --detail --json`.
   Choose the newest backup whose **finish time precedes the desired recovery
   time**; a delta backup fetches its chain itself. `LATEST` is unsuitable when
   recovering an older point. Note its `start_lsn`.
2. Run `wal-g --config /run/secrets/walg.json backup-fetch /var/lib/postgresql/data/pgdata BACKUP_NAME`
   against the new volume, timing it: that is the download time. Create
   `recovery.signal` in that directory.
3. Start PostgreSQL with that volume and `PGDATA`, the read-only secret,
   `hba_file=/etc/clashlens/pg_hba.conf`, `archive_mode=off`, an empty
   `archive_command`,
   `restore_command=wal-g --config /run/secrets/walg.json wal-fetch %f %p`,
   `recovery_target_time=CHOSEN_UTC_TIME`, and `recovery_target_action=pause`.
4. Confirm `SHOW hba_file` returns `/etc/clashlens/pg_hba.conf`, and both
   `pg_is_in_recovery()` and `pg_is_wal_replay_paused()` are true,
   and the log says recovery reached the chosen time. Merely accepting queries
   does not prove the target was reached. The time from starting PostgreSQL to
   that log line is the replay time; `pg_last_wal_replay_lsn()` minus the
   backup's `start_lsn` is the WAL it replayed, and the two give the replay rate
   that decides how much WAL fits in the restore target. Compare expected
   accounts, saved-player links, player history, battle links and army
   summaries. Read every retained raw object referenced by the sample and
   verify its hash, timing it as the raw-check time. Missing required evidence
   means the restore failed. Do not promote this scratch database into production.
5. Repeat for the ten-day-old boundary, using a full backup from before it.
   If the restore could take more than two days while production keeps
   running, first run `systemctl --user stop clashlens-archive-retention.timer`.
   Repeat affected checks after the season history and cleanup work in #140
   changes stored data or maintenance.

Expected timings, from the 8 October 2026 real-size rehearsal (2 processor
cores at the lowest scheduling priority, beside live production): fetching a
15.55 GB compressed full backup took 268 seconds, about 58 MB/s, and replay ran
at about 51 GB of change log an hour. With a full backup every 12 hours, a
restore replays at most about 12 hours of change log after its full backup:
about 53 GB before upload compression (about 45 GB as stored, from 105 GB/day
before compression and 89.36 GB/day stored), so about 1 hour of replay plus
5–8 minutes to fetch a 16–27 GB compressed full backup, and about half that on
average. The rehearsal did not measure
an unthrottled replay or the raw-object check time.

### Targets, cost and remaining rollout checks

The intended maximum data loss is **5 minutes plus upload delay**, not a hard
five-minute guarantee. A provider outage can exceed it. The worst-case restore
time target is about **1 hour** at today's change-log rate, with a full backup
every 12 hours, chosen by the owner on 2026-10-08 over daily full backups with
a 2-hour worst case (see
[expected timings](#restore-into-a-separate-database)).
Ten-day recovery starts only after ten days of uninterrupted archived history.

On 8 October 2026 R2 held 707.87 GB of WAL, uploaded at 89.36 GB/day after
compression, and the newest full backup was 15.55 GB compressed (37.47 GB
before compression); the 64.77 GB database implies about 26.8 GB per full.
Keeping 21–22 fulls of 16–27 GB (about 330–590 GB) plus 10.5–11 days of WAL
(about 940–985 GB) models about **1.27–1.57 TB**, about **$19–24/month** at R2
Standard's $0.015/GB-month, against 1.25–1.88 TB of WAL alone with weekly
fulls. Each extra full adds its size, so the cost grows with the database. The
owner accepted roughly 3–6 GBP/month of added storage for the 12-hour
schedule. This is a model, not
measured production growth. Manual backups add up to another full backup each
until they age out; failed uploads can leave partial objects requiring separately
reviewed cleanup. Real traffic must be measured before accepting the €60 total
monthly envelope. Do not silently let failed pruning or WAL uploads accumulate.

The raw-response retention rule, recovery protection and projected cost belong in
[raw expiry](history-retention.md#implemented-raw-expiry-and-required-recovery-protection)
and the enablement prerequisites belong in
[raw-response cleanup](#raw-response-cleanup).

Validation on 2026-09-19 used a separate PostgreSQL cluster with all 34 migrations
and synthetic records, under R2 prefix `validation-20260919`. A full backup took
4.49 seconds, fetching it took 3.46 seconds, and recovery reached the selected
recent point at 22:53:20 UTC, about 17 seconds after fetch began. The recent
target was 22:53:03.020339 UTC; it recovered display name `Recovered recent point`
and 5,080 trophies, excluding later changes to the name and 5,120 trophies.
An earlier restore to 22:52:18 UTC recovered `Before backup` and 5,040 trophies.
Both retained the saved-player link, battle link and army summary. The read-only
R2 key fetched the backup and WAL; direct write/delete attempts were denied.
These tiny-database timings do not establish production recovery time or worst-case
data loss. The earlier target was minutes old, **not seven days old**.

The backup sorting fix #134 was merged and deployed; its scheduled service was
also invoked successfully, as recorded in #122. The September 27 03:00 UTC backup
fired naturally, as recorded in [#140](https://github.com/zzzubair/clashlens/issues/140).
The 8 October rehearsal measured restore time at production size by restoring
a point 4.4 days old and reading its player count; it did not read raw objects.
Before closing #122, measure data loss,
verify restored raw references across protected expiry, restore a genuine
ten-day-old point, and verify host reboot. Earlier small restores and service
checks do not prove those remaining gates. Keep real collection disabled until
backups and #124 alerts are proven. This documentation update does not deploy
or close #122.

## Raw-response cleanup

The scheduled cleanup deletes old raw responses from the Scaleway archive under
the [raw-expiry rule and recovery protection](history-retention.md#implemented-raw-expiry-and-required-recovery-protection).
It is off by default. Store the operator key pair as two service-owned mode-600
one-line files beside the Clash API keys:
`clashlens-archive-operator-access-key` and
`clashlens-archive-operator-secret-key` in `CLASHLENS_API_KEY_HOST_DIR`. Then set
`CLASHLENS_ARCHIVE_RETENTION_DB_PASSWORD` (32–128 URL-safe characters, like the
other role passwords) and `CLASHLENS_ARCHIVE_RETENTION` in `app.env`:

- `off`: no timer, `up` removes the operator secrets from Podman, and the
  cleanup database role cannot log in.
- `preview`: no timer; `./ops archive-prune` previews one batch on request. It
  changes nothing.
- `apply`: the timer marks and deletes.

`up` copies the operator keys and the cleanup role's database address into
Podman secrets that only the cleanup container mounts. The database secret,
`clashlens-archive-operator-database-url`, logs in as
`clashlens_archive_retention`, not the administrator. That role can read only
the archive identity, the stored-response list, upload states, each
observation's stored-response location and each job's status; it can change
only a stored response's deletion state, and it locks observations and jobs
through one fixed database function. These secrets remain after `down`; the
next production `up` with `CLASHLENS_ARCHIVE_RETENTION=off` removes them. The
collector, worker, API and website never receive them. Each run starts a short-lived container with the
same spool as the collector, processes one batch of up to 1,000 deletions and
1,000 markings, and prints one JSON report. In `apply` the timer starts five
minutes after `up` and runs again 30 seconds after each batch finishes.
Scheduled runs do not take the shared operation lock, so they never delay
deployment or backups; `up` and `down` stop the timer first. A manual run takes
the lock like other operator commands.

```sh
./ops archive-prune              # preview now
./ops archive-prune --apply      # one batch now; only when set to apply
./ops logs archive-retention --since today
```

Enable it in two approved steps:

1. Set `preview`, run `./ops up`, then `./ops archive-prune`. Put its per-batch
   counts and bytes for responses to delete and mark in the deployment report.
2. After that report is approved, set `apply` and run `./ops up`. The owner
   approved `apply` on 2026-10-08, after the real-size restore rehearsal above,
   with the 12-day recovery hold.

Throughput is unmeasured. At an assumed 50 ms per deletion, deleting 1,000
objects takes 50 seconds. With the 30-second gap, that is a theoretical
1.08 million deletions a day before marking, database waits and container startup,
against about 553,000 new objects a day on October 2. After switching on deletion,
total `deleted_objects` across a full day and compare it with new arrivals.
If cleanup cannot keep up, the backlog and the bill keep growing.

A run with any failed object exits unsuccessfully, which marks the service
failed. Logs record its report and each failed object's location and error type.
The next run retries.

## Finished-job cleanup

Every production `up` installs `clashlens-history-retention.timer`, which
deletes processing jobs 48 hours after they finish; nothing in `app.env` turns
it on or off. `up` also stores `clashlens-history-operator-database-url`, a
Podman secret only the cleanup container mounts. It logs in as
`clashlens_history_retention` (migration 0053) with a random password that
`up` replaces each time; that role can only run the one deletion function.
Fixture stacks get no timer, and the role cannot log in there. Checks and
failure handling are in [operating](operating.md#finished-job-cleanup-failed).

## Discord bot

`./ops up` installs the Discord bot's unit in every mode but starts it only in
production with `CLASHLENS_DISCORD_BOT=on` and the mode-600 token file
`clashlens-discord-bot.token` under `CLASHLENS_API_KEY_HOST_DIR`. The
[Discord bot](discord-bot.md) page has the go-live checklist.

## Status and logs

```sh
./ops status
./ops logs
./ops logs collector
./ops logs discord-bot
./ops logs postgres --since today
./ops logs worker -f
./ops queue-status
```

`status` fails when the target or any required system service is stopped, or when
any required container is absent, stopped, or unhealthy. Logs come from the user
journal, which includes both container output and systemd lifecycle failures
without printing configuration files.

Podman checks each container every 30 seconds and kills it after six failed
checks in a row, about three minutes. The collector's check is `/livez` on
port 8081. It fails only when the collector needs a restart: one of its three
main loops (player checks, queued requests, spool cleanup) has not come
round for 20 minutes, its spool or a saved-response handoff failed, or every
regular or interactive key is quarantined. It never waits on the database, a
spool lock or a thread. A database call made by the loop itself, or by work it
started, holds that verdict off for up to 20 minutes from when the call began,
so a slow database slows collection without getting the collector killed; on
7 Oct 2026 it was killed 13 times for that. Another loop's call never holds it
off: until 8 Oct 2026 any running call did, and player checks keep one running
almost all the time, so a stuck Reset or upload loop could pass for ever. Every
collector statement also stops after 60 seconds (5 minutes for the Reset
sweep's), five times the slowest seen from 1 to 8 Oct 2026, so no database wait
lasts indefinitely; a stopped statement is retried, then its loop stops and the
collector restarts. The collector restarts its uploads process by itself when
that exits or stops reporting, so a stuck upload never restarts collection; a
spool read failure there still fails `/livez` as `spool_io_failure`. The port opens
before startup recovery, which answers `starting`, and failures in the first
five minutes are ignored; the five-minute start limit still applies. `/readyz`
still reports the database, spool capacity and keys for a person to read.

The worker's check is `clashlens.cli ready`. Each worker thread and the
maintenance timer write when they last came round, and the check fails when one
of them has not for 20 minutes, whether or not the others keep going, or for an
hour for the thread that runs board builds and for the timer. Only a progress
file that cannot be read falls back to its own age: not written for 20 minutes. The longest finished job
from 1 to 8 Oct 2026 took 465 seconds, or 844 seconds for an army build.

PostgreSQL logs statements slower than 5 seconds, waits for a lock longer than
1 second, and every automatic vacuum and statistics refresh, without query
values. On 6 Oct 2026 its log was 3.3 MB; at the 2,100 automatic runs a day
seen since 18 Sep, these add roughly 2–3 MB a day, more on a slow day.
`./ops` turns the slow-statement log off for its password changes.

## Private Discord alerts

`./ops alert-check` checks the eighteen [alert conditions](alerts.md) and posts
changes to the private operator channel through an incoming webhook. Create the
service-owned mode-600 file
`/srv/clashlens-secrets/clashlens-discord-alert-webhook` separately.
Its default directory follows `CLASHLENS_API_KEY_HOST_DIR`; an optional
`CLASHLENS_DISCORD_ALERT_WEBHOOK_FILE` overrides the full path. Store only the URL
in that file. Never put it in `app.env`, command arguments, logs or documentation.
A missing, unreadable, wrongly owned or non-600 file fails the check loudly.

A subsequently approved production `./ops up` installs `clashlens-alert.service`
and `clashlens-alert.timer`. The enabled `clashlens.target` starts the timer on
reboot, using the same installation path as the backup timer. It checks every
minute with `Persistent=true`, so a missed calendar check runs after reboot.
Both units use `PartOf=clashlens.target`. The service has no dependency on a
healthy collector or API, so failed processes cannot prevent the check starting.
Fixture mode does not install or start Discord alerts. `./ops down` records an
intentional stop and stops the timer; manual checks also stay quiet until a
successful `./ops up`. Checks also stay quiet while `up` starts the stack,
unless it fails after stopping services: then it starts the timer again and
the checks report the failed deploy.
Time deliberately stopped does not count as a fetch gap.

### Alert conditions

The [alert conditions](alerts.md) page lists each check's thresholds,
recovery rules and delivery behaviour.

### Outside availability check

The Paris relay checks `https://preview.clashlens.net/` and its `/healthz`
every minute, from outside rogue, and posts to the same Discord channel when
either has failed every check for two minutes, then again when both have
answered every check for 15 minutes.
A check fails on a timeout after 10 seconds, a redirect or any non-2xx answer.
An intentional `./ops down` also alerts here. It runs the same
[`alerts.py`](../python/src/clashlens/alerts.py) with `--uptime`, using the
Python 3 the relay already has. Its state is one incident record under
`/var/lib/clashlens-uptime`, under 1 KiB.

Installed on 2026-10-03 as the system user `clashlens-uptime`, with the
webhook URL copied from rogue to `/etc/clashlens-uptime/discord-webhook`,
owned by that user with mode 600. To install or update it, copy `alerts.py`
to `/opt/clashlens/uptime/alerts.py`, mode 644, owned by root, and these two
units to `/etc/systemd/system/`, then run
`systemctl daemon-reload && systemctl enable --now clashlens-uptime.timer`.

`clashlens-uptime.service`:

```ini
[Unit]
Description=Clash Lens outside availability check
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
User=clashlens-uptime
StateDirectory=clashlens-uptime
StateDirectoryMode=0700
ExecStart=/usr/bin/python3 /opt/clashlens/uptime/alerts.py --uptime /var/lib/clashlens-uptime /etc/clashlens-uptime/discord-webhook https://preview.clashlens.net/ https://preview.clashlens.net/healthz
TimeoutStartSec=2min
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
```

`clashlens-uptime.timer`:

```ini
[Unit]
Description=Run the Clash Lens outside availability check every minute

[Timer]
OnCalendar=*-*-* *:*:30
AccuracySec=1s

[Install]
WantedBy=timers.target
```

Check it with `systemctl status clashlens-uptime.timer` and
`journalctl -u clashlens-uptime.service --since today` on the relay. While the
site is down each run exits unsuccessfully and logs one fixed line. The relay
cannot alert if the relay itself or Discord is down.

## Failed work

While processing a claimed job, the worker handles database deadlocks, where
jobs wait on each other's locks, and serialization failures, where PostgreSQL
rejects conflicting concurrent writes. These conflicts do not stop other jobs.
The worker runs the same claimed job up to three times without consuming another
queue attempt. If all three runs conflict, it records `database_deadlock` for
either kind of conflict; the normal attempt limit decides whether the job
retries or fails.

If that failure write also conflicts, the worker keeps the lease and tries to
restore an unused attempt before returning `retrying`. Queue maintenance can
then recover the job after the lease expires. Restoration can succeed after
expiry while the job is still leased to the same owner with the same claim
token. A new claim replaces that token, and queue maintenance clears it, so
restoration cannot change a job another worker or maintenance has taken; the
expired worker returns `lease_lost` instead.

When PostgreSQL refuses a job's writes, through a trigger's check, a
constraint or an invalid value, the worker records `database_rejected` with
PostgreSQL's one-line reason, and the normal attempt limit decides whether the
job retries or fails. Other jobs keep running. If recording that failure is
refused, conflicts or times out too, the worker leaves the lease to expire and
queue maintenance retries the job, or fails it on its last attempt. A lost
database connection still stops the worker so systemd restarts it.

When a job waits 30 seconds for a free connection from the worker's shared
pool and gets none, only that job stops. The worker gives its attempt back and
logs it as `retrying` with `database_pool_timeout`, and queue maintenance
requeues it after its lease expires. If no connection is free for giving the
attempt back either, the lease still expires and maintenance retries the job,
or fails it on its last attempt. Other jobs keep running.

The running worker cancels any single database statement after 15 minutes,
including time spent waiting for a lock, set by
`WORKER_STATEMENT_TIMEOUT_SECONDS` in [`db.py`](../python/src/clashlens/db.py).
Its slowest statements from Oct 1 20:04 to Oct 3 took 56 seconds. Cancelling
rolls the job's transaction back and gives its attempt back, so queue
maintenance requeues it after its lease expires even on its last attempt. The
worker logs it as `retrying` with `database_timeout`. A job that hits the
deadline every time keeps being retried and raises the one-hour processing
alert. A cancelled statement outside a job, such as queue maintenance, stops
the worker, which restarts. Operator commands have no deadline.

A battle log with new or changed armies waits at most 250 milliseconds for the
Reset publication locks of its days (`RESET_LOCK_WAIT` in
[`db.py`](../python/src/clashlens/db.py)). If a
slow publication holds one, its whole transaction rolls back, so it stops
holding its battles and the collector's response, and the worker gives its
attempt back and logs it as `retrying` with `database_lock_busy`. A daily
calculation, from a job or the late-battle sweep, waits the same 250
milliseconds for any lock, such as each day's Reset or another calculation of
the same player and day, so one that spans several days does not keep earlier
Resets locked while a later one is busy; the late-battle sweep retries such a
player at its next run. Any other job
that hits a short lock-wait limit is handled the same way. If a database time
limit ends the worker's whole session mid-job
(`idle_in_transaction_session_timeout` or `transaction_timeout`), the worker
first reads the job's saved attempt: a result that committed is kept and
reported as such; otherwise the attempt is given back and logged as `retrying`
with `database_session_timeout`. The pool opens a new connection in place of
the closed one. Giving an attempt back waits at most one second for the job's
row; if that runs out, the lease expires and maintenance retries the job, or
fails it on its last attempt.

While a Reset's board waits for more than 100 members, its Reset readings,
battle logs and day results only share that Reset's publication lock, so they
run side by side; creating, freezing or correcting the board, and the last 100
member results, take it alone (`lock_boundary_members` in
[`boundary.py`](../python/src/clashlens/boundary.py)). On 2026-10-06 they took
it one at a time, about 13 a second, and the board waited until 06:15. Reset
readings, results for the Legend day that just ended, and the board's snapshot
and analytics builds are queued at priority 300 instead of 100
(`PYTHON_RESET_PRIORITY` in [`db.py`](../python/src/clashlens/db.py)). A claim
adds 10 for each minute live work has waited, while Reset-priority work keeps
its fixed score, so by waiting time they go first unless live work has waited
20 minutes, however long they have waited themselves. Each worker thread
alternates (`RESET_FIRST_CLAIM_EVERY` in
[`worker.py`](../python/src/clashlens/worker.py)): every other job it claims
takes Reset-priority work first, and the rest take other due work first, with
backfill still last. So while both wait, the board's Reset work and live work,
such as new responses and the new day's results, each get at least half of the
jobs every thread claims, however recently the live work arrived. On
2026-10-08 the rest still went by waiting time, so live responses that arrived
after 05:09 waited 21 minutes behind the Reset backlog. Build claims always
take Reset-priority work first, so the board's build and checks never wait
behind an army build. Among live work, a claim looks only at the 32 jobs that
became due first, so a retried live job, due again from its retry time, can
wait behind older live work. On 2026-10-07 the collector's outage delayed about 19,000
Reset readings by 35 minutes; while they also earned the waiting bonus, no live
reading was processed until they were all done, 40 minutes later, and live
pages fell up to 59 minutes behind. Operator batches
(`republish-current-season --first-logs`, `--day-1`, `--overlap-gap`, `--mismatch` and `--sign-up`)
are queued at backfill priority, 25, which a worker thread only runs when no
higher-priority work that thread can claim is due; a thread that does not process saved
responses can run one while responses still wait. On a thread's Reset-first
turn, a claim from the newest-job plan takes its planned job only if, in the
same database statement, no Reset-priority work it could take is waiting: due,
waiting on its saved response, or with an expired lease. If there is any, the
same claim uses that order instead. On the other turn it takes its planned
job. Asking for one particular job by number still takes that job.
The board maintenance pass waits at most 50 milliseconds for a Reset's lock
and otherwise tries again on its next pass.

Production runs `CLASHLENS_WORKER_PROCESSES` worker processes, 1 unless set,
each with its own queue maintenance. If maintenance in another worker process
reaches an expired job on its last allowed attempt before restoration
succeeds, it still fails the job as `lease_expired_max_attempts`. See
[`ObservationProcessor._process_claim`](../python/src/clashlens/worker.py) and
the recovery cases in
[`test_claim_jobs_postgres.py`](../python/tests/test_claim_jobs_postgres.py).

Listing and previewing are the default; a retry needs both one exact item and
`--apply`:

```sh
./ops failed-items --limit 20
./ops failed-items --work-id 123
./ops failed-items --work-id 123 --apply
./ops failed-items --upload-hash SHA256
./ops failed-items --upload-hash SHA256 --apply
./ops failed-items --accept-job-id 123 --reason 'profile shape cannot be parsed'
./ops failed-items --accept-job-id 123 --reason 'profile shape cannot be parsed' --apply
./ops failed-items --replay-job-id 123 --replay-job-id 124 --reason 'parser v3 replay' --apply
```

This command starts an ephemeral copy of the pinned Python image with an
init-only database secret, then removes the secret. The long-running worker
never receives this operator-only retry authority. Repair configuration or
authentication before restarting the collector and retrying archive
configuration failures. Archive checksum or catalogue contradictions return
`archive_integrity_repair_required` and are never requeued automatically.
Failed profile and battle-log observation processing is replayed only through
the audited `clashlens_request_python_replay_v2` database function:
`--replay-job-id` (repeatable, with a `--reason` of 8 to 500 characters) asks
it for each failed job's saved response under that job's own parser and rules,
switching the admin login to the `clashlens_replay_request` role for that one
transaction, and queues all the jobs or none. The host-only
`deploy/replay-request --observation-id ID --reason REASON` wrapper calls the
same function, with `--parser-version` set to the failed job's parser
(`supercell-battle-parser-v3` is accepted for battle logs only).
League-history, global, and derived processing failures require
investigation. A failed processing job that cannot be repaired can be
accepted with `--accept-job-id` and a `--reason` of 8 to 500 characters: it
keeps its failed state and evidence, the acceptance records `ops:` and the host
account, the time and the reason, and the failed-work alerts stop counting it.
A job can be accepted once; nothing undoes an acceptance. Transport failures
are evidence governed by the normal work policy and are not manually requeued.

## Worker processes

In `app.env`, `CLASHLENS_WORKER_PROCESSES` (1 or 2, default 1) sets how many
worker processes run in the worker container, `CLASHLENS_WORKER_CONCURRENCY` (default
12) each process's threads, `CLASHLENS_WORKER_RESPONSE_LANES` (default about
two thirds of them, 8 of 12) how many of them process only responses, and `CLASHLENS_WORKER_DATABASE_POOL_SIZE`
(default 12, at most 16) each process's connections. All processes share the container's
memory limit (`CLASHLENS_WORKER_MEMORY`, 4 GB by default) and CPU limit. The
worker refuses to start with more than 16 connections a process or 38 in
all; see [the database connection budget](architecture.md#structured-data-and-evidence)
for how that fits with the other processes.

The setup proposed on 8 October 2026 for a 05:30 board with fresh live pages
is 2 processes of 16 threads, 12 for responses, and 16 connections each, 38
database connections in all. Production keeps one worker process until both
of these are true:

- The owner has decided to run the second worker process.
- A `./dev` trial of this setup has shown the combined resources and recovery:
  both processes together under 3 GB of memory with one build running, swap
  barely used, at most 38 worker database connections, and an interrupted
  20,000-job backlog finished with every job done once.

Only then, outside 04:00-07:00 UTC, turn it on by setting in `app.env`:

```sh
CLASHLENS_WORKER_PROCESSES=2
CLASHLENS_WORKER_CONCURRENCY=16
CLASHLENS_WORKER_RESPONSE_LANES=12
CLASHLENS_WORKER_DATABASE_POOL_SIZE=16
```

then run `./ops up`. It restarts the worker, API and website, and leaves the
collector and database running when their images and settings are unchanged.
To undo it, remove those four lines and run `./ops up` again.

Memory is expected, not yet measured, to stay well inside the 4 GB limit. At
09:32 UTC on 8 October 2026 one process used 325 MB, with a peak of 887 MB in
the 26 minutes since it started; earlier peaks were near 1 GB. A population
build is the biggest use, and only one runs at a time across processes, so
two processes are expected to peak at about 1.3 to 1.7 GB, adding roughly 0.3
to 0.8 GB on the host, which then had 9.0 GB available.

At the first Reset with two processes, check:

- `./ops logs worker`: no `worker_process` lines, which mean a process exited
  and the container restarted both. Each `worker_health` line belongs to one
  process; the change in its `python_process_observation` and
  `python_reconcile_ranked_day` stage counts between lines is its responses
  and daily results per minute. Their `queue.kinds` show the overdue backlog
  of each kind of work and how long the oldest has waited.
- The board's first frozen publication time against 05:25 for the frozen
  inputs and 05:30 for the published board.
- `systemctl --user show clashlens-worker.service -p MemoryCurrent -p
  MemoryPeak`, plus `free -m` showing at least 2 GB available and `vmstat 1 5`
  showing little swapping.
- At most 38 database connections for `clashlens_python_worker` in
  `pg_stat_activity`.

## Support recovery

The agreed support entry point is private tickets in the Clash Lens Discord
community, using an existing ticket bot. Public discussion/feedback and the
private operator-alert channel are separate. Provider, permissions, transcript
retention, identifiers and cost remain setup work in
[#138](https://github.com/zzzubair/clashlens/issues/138). Website support links
must work without a Clash Lens login. A ticket is not ownership proof; tokens
must use the existing protected verification flow, never ticket messages.

The existing restricted host wrapper remains supported. Configure its entry
point as `DEPLOY_SCRIPT=/srv/clashlens/ops`; the internal
`support-recovery-exec` command enters the existing private API container and
is unavailable in fixture mode. The root-owned wrapper still validates the
sudo caller and prompts for the current in-game token without echo.

`deploy/support-transfer` and `deploy/replay-request` continue to connect
through their narrow PostgreSQL service roles and do not use `./ops`.

## Data and launch boundary

`./ops down` removes no retained data. PostgreSQL uses a named volume; the
spool and fixture archive use explicit persistent paths. The 16 GiB spool cap
bounds temporary raw-response storage. Database and remote archive retention
remain governed by the product rules and are not made size-bounded by Quadlet.

Fixture lifecycle evidence does not prove launch readiness. Apply the separate
tracking and public-website gates at the top of this document; real traffic,
spending, deployment and deletion still require their specific approvals.
Leave old GCS/B2 buckets untouched.
