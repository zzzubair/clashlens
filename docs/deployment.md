# Fedora operation

`./ops` runs Clash Lens as rootless Podman containers managed by the user's
systemd service manager. The tracked files under `deploy/quadlet/` are
Quadlets: Podman turns them into ordinary system services. PostgreSQL, the
collector, private API, worker, and website are owned by one
`clashlens.target`, so they start together after reboot and stop together.

Building and running are separate operations. `up` never builds or pulls an
image. `build` records the exact image IDs and a fingerprint of every source,
migration, and unit input; `up` refuses to mix those images with changed init
files. A new build is only staged: operator and recovery commands keep using
the last successfully started release until `up` promotes the new one.

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

Copy `app.env.example` to `app.env`, replace every `CHANGE_ME`, and make it
private:

```sh
cp app.env.example app.env
chmod 600 app.env
```

Create the configured spool as a dedicated directory owned by the service
account with mode 700. Keep it outside the account's home, checkout, ops state
and unit directories, and secret directory; `up` refuses overlapping paths
before stopping the current stack. Under
`CLASHLENS_API_KEY_HOST_DIR`, create five mode-600 files named
`clashlens-normal-1` through `clashlens-normal-4` and
`clashlens-interactive-1`, plus the HMAC file named by
`CLASHLENS_HMAC_SECRET_FILE`. `./ops` transfers their values into Podman's
private secret store. API keys are supplied to the collector as an environment
secret because its current CLI accepts `label=value` key pools; they are not
written to a unit, environment file, or process argument. The API receives
only the interactive key as a mounted file.

The private API also requires `CLASHLENS_OFFICIAL_API_PROXY_URL` for its
one-player token verification calls. Configure the fixed-egress proxy at an
HTTP(S) origin reachable from inside the pod. The collector does not use this
setting; its regular collection calls connect directly from the Fedora host.

The archive credentials have separate duties. The collector credential creates
immutable raw responses and may read back the marker or one exact object to
prove a write; the worker credential can only read. Neither runtime credential
may list, overwrite, delete, or broadly browse archive objects.
The database also has separate collector, worker, and API roles. The admin
database URL exists only as a short-lived Podman secret during fixture
bootstrap or while an operator explicitly handles a failed item.

Build from the checkout to be released, review the resulting commit, then run
the already-built release:

```sh
./ops build
./ops up
./ops status
```

The website and collector health endpoints bind to `127.0.0.1`; PostgreSQL and
the private API have no host port. Production discovery remains disabled under
the issue #110 decision, while the single global Top-200 request remains on
each five-minute cycle.

`up` first disables and stops the whole target. It then starts PostgreSQL by
itself, applies every missing numbered migration in order, verifies the fixed
archive contract, rotates the admin and runtime-role passwords through standard
input, and only then enables the application target. A failed migration leaves application
services disabled for the next reboot. Re-running `up` applies only migrations
whose recorded version is absent.

## Status and logs

```sh
./ops status
./ops logs
./ops logs collector
./ops logs postgres --since today
./ops logs worker -f
./ops queue-status
```

`status` fails when the target is stopped or any required container is absent,
stopped, or unhealthy. Logs come from the user journal, which includes both
container output and systemd lifecycle failures without printing configuration
files.

## Failed work

Listing and previewing are the default; a retry needs both one exact item and
`--apply`:

```sh
./ops failed-items --limit 20
./ops failed-items --work-id 123
./ops failed-items --work-id 123 --apply
./ops failed-items --upload-hash SHA256
./ops failed-items --upload-hash SHA256 --apply
```

This command starts an ephemeral copy of the pinned Python image with an
init-only database secret, then removes the secret. The long-running worker
never receives retry authority. Repair configuration or authentication before
restarting the collector and retrying archive configuration failures. Archive
checksum or catalogue contradictions return
`archive_integrity_repair_required` and are never requeued automatically.
Failed profile and battle-log observation processing is replayed only through
the existing audited `deploy/replay-request --observation-id ID --reason REASON`
path. League-history, global, and derived processing failures require
investigation. Transport failures are evidence governed by the normal work
policy and are not manually requeued.

## Support recovery

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

This setup proves service lifecycle against fixtures. Before going live,
separately authorize and prove backups/restoration, production OAuth, one real
Legend day with agreed keys and costs, and alerts. Do not infer launch readiness
from the fixture or reboot checks, and do not touch old GCS/B2 buckets.
