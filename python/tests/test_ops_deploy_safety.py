"""What ./ops up checks before it stops anything, what a failed up leaves,
and when the database's start-up check stops a crash replay.

A full up stops the running stack before it starts the new release, so a
release that cannot run must be refused first, and an up that fails after
stopping must say so: until 8 Oct 2026 it left everything stopped with the
alerts switched off."""

import configparser
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from test_ops_keep_running import OPS, stack  # noqa: F401 - the shared fake stack

ROOT = OPS.parent
KNOWN = sorted(
    int(path.name.split("_", 1)[0]) for path in (ROOT / "deploy/migrations").glob("*.sql")
)
# The real up, with the host and configuration checks and the writing of
# settings and secrets skipped. Unit files hold a marker, or are rejected.
UP = r"""
source "$1" help >/dev/null
for check in require_host load_release load_production_config validate_runtime_values \
  guard_generated_units guard_existing_resources guard_trusted_proxy_ip guard_network_subnet \
  cleanup_stale_admin_state ensure_linger migrate_legacy_units guard_systemd_units \
  write_environment prepare_secrets; do
  eval "$check() { :; }"
done
render_units() {
  mkdir -p "$QUADLET_DIR"
  echo rendered > "$QUADLET_DIR/marker"
  [[ -z "$REJECT" ]] || die "unrendered value in clashlens-worker.container"
}
MODE=production PREFIX=clashlens POSTGRES_USER=clashlens POSTGRES_DB=clashlens
RELEASE=([COLLECTOR_IMAGE]=$NEW_COLLECTOR [POSTGRES_IMAGE]=$POSTGRES)
up_stack
"""
# A running database that has applied $APPLIED and answers psql as PostgreSQL
# would; SQL it is fed fails when it contains $FAILING, and is logged as a
# trial when it ends in a rollback.
FAKE_PODMAN = f"""#!{sys.executable}
import os, re, sys
args = sys.argv[1:]
if "psql" not in args:
    os.execv(os.environ["MANAGER"], [os.environ["MANAGER"], *args])
applied = os.environ["APPLIED"].split()
with open(os.environ["CALLS"], "a") as calls:
    if "--command" not in args:
        migration = sys.stdin.read()
        trial = migration.rstrip().endswith("ROLLBACK;")
        calls.write("psql trial\\n" if trial else "psql migration\\n")
        sys.exit(3 if os.environ.get("FAILING") and os.environ["FAILING"] in migration else 0)
    query = args[args.index("--command") + 1]
    if "to_regclass" in query:
        print("t")
    elif "SELECT version FROM" in query:
        print("\\n".join(applied))
    elif "EXISTS" in query:
        print("t" if re.search(r"version=(\\d+)", query)[1] in applied else "f")
"""


def up(stack, applied: list[int], failing: str = "", reject: str = ""):  # noqa: F811
    tmp_path = stack["tmp_path"]
    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    (fake / "podman").write_text(FAKE_PODMAN)
    (fake / "podman").chmod(0o700)
    calls = Path(stack["env"]["CALLS"])
    calls.unlink(missing_ok=True)
    result = subprocess.run(
        ["bash", "-c", UP, "deploy-safety-test", str(OPS)],
        env=dict(
            stack["env"],
            PODMAN_BIN=str(fake / "podman"),
            MANAGER=stack["env"]["PODMAN_BIN"],
            APPLIED=" ".join(map(str, applied)),
            FAILING=failing,
            REJECT=reject,
        ),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    result.calls = calls.read_text().splitlines() if calls.exists() else []
    result.stops = [i for i, call in enumerate(result.calls) if call.startswith("--user stop ")]
    state = tmp_path / "state" / "clashlens"
    result.staged = list(state.glob("staged.*"))
    result.rendered = (tmp_path / "config" / "containers" / "systemd" / "marker").exists()
    return result


def test_a_release_missing_an_applied_migration_is_refused_before_stopping(
    stack,  # noqa: F811
) -> None:
    result = up(stack, [*KNOWN, 999])
    assert result.returncode != 0
    assert "the database has migrations 999 that this release lacks" in result.stderr
    assert not result.stops
    assert "psql trial" not in result.calls


def test_a_failing_migration_leaves_the_old_release_running(stack) -> None:  # noqa: F811
    # Tried in a transaction that is rolled back: nothing is applied. The
    # newest migration fails on its first line, whichever migration that is.
    (newest,) = (ROOT / "deploy/migrations").glob(f"{KNOWN[-1]:04d}_*.sql")
    result = up(stack, KNOWN[:-1], failing=newest.read_text().splitlines()[0])
    assert result.returncode != 0
    assert "a pending migration failed when tried on the running database" in result.stderr
    assert "psql trial" in result.calls
    assert "psql migration" not in result.calls
    assert not result.stops
    assert not [call for call in result.calls if "start clashlens-alert.timer" in call]
    intent = stack["tmp_path"] / "state" / "clashlens" / "alert-intent"
    assert not intent.exists()


def test_a_failed_up_after_stopping_restarts_the_alert_schedule(
    stack,  # noqa: F811
) -> None:
    # The newest migration is still to apply: it is only tried, and rolled
    # back, before anything stops. This test's up then fails after
    # stopping, as a real one can at any later step.
    result = up(stack, KNOWN[:-1])
    assert result.returncode != 0
    assert "lacks" not in result.stderr
    assert result.calls.index("psql trial") < result.stops[0]
    assert "psql migration" not in result.calls[: result.stops[0]]
    # Unit files are written in place only after services stop.
    assert result.rendered and not result.staged
    assert "--user stop clashlens-worker.service" in result.calls
    assert any("clashlens-alert.timer" in result.calls[i] for i in result.stops)
    # The schedule comes back so the check reports the failed deploy and
    # retries its delivery every minute.
    restart = result.calls.index("--user start clashlens-alert.timer")
    assert restart > result.stops[-1]
    intent = stack["tmp_path"] / "state" / "clashlens" / "alert-intent"
    assert intent.read_text() == "failed\n"


def test_a_rejected_unit_file_stops_nothing_and_changes_nothing(stack) -> None:  # noqa: F811
    result = up(stack, KNOWN, reject="1")
    assert result.returncode != 0
    assert "unrendered value in clashlens-worker.container" in result.stderr
    assert not result.stops
    # Written only into a scratch folder, which is gone again.
    assert not result.rendered and not result.staged


@pytest.fixture
def replay_check(tmp_path):
    """The database's startup check, run with fake PostgreSQL tools."""
    unit = configparser.ConfigParser(interpolation=None, strict=False, allow_no_value=True)
    unit.optionxform = str
    unit.read(ROOT / "deploy/quadlet/clashlens-postgres.container")
    command = unit["Container"]["HealthStartupCmd"].replace("/tmp/", f"{tmp_path}/")
    # kill is a shell built-in; a function takes its place.
    command = 'kill() { echo "$*" >> "$STATE/killed"; }; ' + command
    fake = tmp_path / "bin"
    fake.mkdir()
    for name, body in (
        ("pg_isready", 'exit "$(cat "$STATE/ready")"'),
        ("ps", 'cat "$STATE/processes"'),
    ):
        (fake / name).write_text(f"#!/bin/sh\n{body}\n")
        (fake / name).chmod(0o700)

    def check(*, ready=False, replaying=None, minutes_since_change=0):
        (tmp_path / "ready").write_text("0" if ready else "1")
        # The checker itself shows in the list, with its own pattern.
        (tmp_path / "processes").write_text(
            f"postgres\nsh -c {command}\n"
            + (f"postgres: startup recovering {replaying}\n" if replaying else "")
        )
        seen = tmp_path / "replay"
        if seen.exists():
            then = time.time() - 60 * minutes_since_change
            os.utime(seen, (then, then))
        result = subprocess.run(
            ["sh", "-c", command],
            env={"PATH": f"{fake}:/usr/bin:/bin", "STATE": str(tmp_path)},
            capture_output=True,
            text=True,
            check=False,
        )
        killed = tmp_path / "killed"
        return result.returncode, killed.read_text() if killed.exists() else ""

    return check


def test_a_crash_replay_that_keeps_advancing_is_never_stopped(replay_check) -> None:
    assert replay_check(replaying="000000010000000A00000001") == (1, "")
    # Hours of replay, each file read within the last ten minutes.
    for segment in range(2, 40):
        result = replay_check(
            replaying=f"000000010000000A{segment:08X}", minutes_since_change=9
        )
        assert result == (1, "")
    assert replay_check(ready=True) == (0, "")


def test_a_crash_replay_stuck_on_one_file_for_ten_minutes_is_stopped(
    replay_check,
) -> None:
    replay_check(replaying="000000010000000A00000007")
    assert replay_check(replaying="000000010000000A00000007", minutes_since_change=9) == (1, "")
    assert replay_check(replaying="000000010000000A00000007", minutes_since_change=11) == (
        1,
        "-QUIT 1\n",
    )


def test_a_start_with_no_visible_replay_is_left_to_finish(replay_check) -> None:
    replay_check(replaying="000000010000000A00000007")
    # The replay is done or cannot be seen, and the checker's own pattern in
    # the process list is no replay either: waiting is safer than stopping it.
    for _check in range(3):
        assert replay_check(minutes_since_change=60) == (1, "")


def test_up_keeps_the_release_it_replaces_for_a_rollback(tmp_path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    release = tmp_path / "release.env"
    script = r"""
source "$1" help >/dev/null
STATE_DIR="$2" RELEASE_FILE="$3" ACTIVE_RELEASE_FILE="$2/active-release.env"
RELEASE=([COLLECTOR_IMAGE]=sha256:1 [POSTGRES_IMAGE]=sha256:2)
for revision in old new; do
  printf 'SOURCE_REVISION=%s\n' "$revision" > "$RELEASE_FILE"
  promote_active_release
done
"""
    result = subprocess.run(
        ["bash", "-c", script, "rollback-test", str(OPS), str(state), str(release)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "SOURCE_REVISION=new" in (state / "active-release.env").read_text()
    previous = state / "previous-release.env"
    assert "SOURCE_REVISION=old" in previous.read_text()
    assert previous.stat().st_mode & 0o777 == 0o600
