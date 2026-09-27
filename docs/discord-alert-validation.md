# Private Discord alert implementation check

Worktree: `discord-alerts`, branch `zzzubair/discord-alerts`.
Checked locally on 2026-09-27 against the approved launch subset of #140.
The initial implementation used no production access or real webhook.
The later owner-requested delivery test changed no running services.
No deployment has occurred.

## Changes

Added `./ops alert-check`, its Python implementation, a one-minute systemd timer
and service, and production lifecycle wiring. It checks fetch progress, spool and
filesystem capacity, service restart history, existing backup status, and a signed
private player-data read. Persistent state prevents repeat messages and retries
failed deliveries. Intentional shutdown and startup suppress checks until `up`
finishes successfully. Configuration and response steps are in
[deployment.md](deployment.md#private-discord-alerts).

Corrected the existing last-success measurement: PostgreSQL's `greatest(0, NULL)`
reported zero seconds when no fetch had ever succeeded. It now omits that
measurement so a never-started tracker can reach the ten-minute alert threshold.
No existing test was rewritten to accept new behaviour.

Diff size: **1,162 added / 6 removed lines**, including this report. `ops` is 1392 lines; `collector_db.py` remains 1,489.
The addition exceeds 300 lines because this adds the approved alert delivery and
state handling plus behavioural tests. There was no existing alert implementation
to remove. Existing backup checks and request-signing code are reused.
No dependency, database table, monitoring stack or Discord bot was added.

## What ran and what happened

- `uv run --project python pytest -q python/tests/test_alerts.py
  python/tests/test_ops_backup.py python/tests/test_collector_metrics_postgres.py
  python/tests/test_api_security.py python/tests/test_operating.py`: **56 passed,
  2 skipped**. The skips require a disposable PostgreSQL test database.
- The final run includes **27 alert tests**. A local HTTP server
  received each alert and recovery, and confirmed no repeat delivery. Tests cover
  HTTP errors, redirects, network failure, retained retries, strict thresholds,
  Reset timing, missing secrets, private file permissions, intentional stops,
  unavailable measurements, and concurrent checks. The real private API route
  was also tested with a fake database that fails data reads while readiness
  stays healthy. The subprocess timeout test confirmed children did not survive.
- `uv run --project python ruff check python development/*.py`: passed.
- `uv run --project python python -m compileall -q python/src development`:
  passed. `bash -n ops` and `git diff --check`: passed.
- `systemd-analyze --user verify --man=no` accepted temporary rendered copies
  of the new service, timer and target, with disposable dependency stubs.
  `systemd-analyze calendar '*-*-* *:*:00 UTC' --iterations=3` showed three
  consecutive one-minute firings. No unit was installed or started.
- `./dev check` stopped immediately with `dev: Podman is required`.

## Unchecked and out of scope

During the initial implementation, Podman, a disposable PostgreSQL test server,
live Discord, channel access rules,
production service failures, a natural timer firing and host reboot were not
available or authorized here. The new SQL regression and existing persisted-metric
integration test were skipped, not passed. Static unit validation does not prove
live lifecycle behaviour. The real backup and read probes were not run on rogue.

The required `PartOf=clashlens.target` relationship also stops monitoring after an
out-of-band target stop. A host or network outage cannot be reported by this
local checker while it is offline. A lost delivery acknowledgement can cause a
retry duplicate; these limits and operator journal commands are documented.
Restart history needs at least one hour of retained user journal data. The
structured restart event and `USER_UNIT` field were checked against
[systemd's source](https://github.com/systemd/systemd/blob/v257/src/core/service.c)
and its [unit logging definitions](https://github.com/systemd/systemd/blob/v257/src/core/unit.h).

The existing `dev` file has 1,553 lines and was left untouched. Static systemd
validation also reported an unrelated installed `spice-vdagent.service` warning
about `StandardError` under `[Install]`; nothing on the host was changed.
Deferred job/upload stalls, missed Reset publication and capacity budgets remain
unimplemented. No implementation decision is needed now. A separately approved
live rehearsal is still required before treating #140 as proven for launch.

## Owner-requested live delivery test

Zubair subsequently requested one real test alert on 2026-09-27. The webhook file
on rogue passed ownership and mode-600 checks. Discord rejected the default
Python client with HTTP 403; a read-only request with an explicit Clash Lens
User-Agent succeeded. The worktree sender now always identifies that client,
with a regression test against a local server that rejects Python's default.

Discord then returned HTTP 2xx and a created message for the labelled test alert
at **2026-09-27 04:18:24 UTC / 05:18:24 UK time**, using the same sender with
the explicit client header. Channel ID: `1553615972252782632`. Message ID:
`1553621810048667739`. The webhook URL was never printed or saved in the repo.
Zubair confirmed the test message was visible in the channel. Membership
privacy, real recovery delivery, the scheduled checker and reboot behaviour
remain unproven. No services were changed or deployed; the one-off sender ran
in memory through SSH.

After the client-header fix, the alert suite passed **27 tests** and Ruff passed.
