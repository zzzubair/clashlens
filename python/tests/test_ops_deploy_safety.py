"""What ./ops up checks before it stops anything, what a failed up leaves,
and when the database's start-up check stops a crash replay.

A full up stops the running stack before it starts the new release, so a
release that cannot run must be refused first, and an up that fails after
stopping must say so: until 8 Oct 2026 it left everything stopped with the
alerts switched off."""

import configparser
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest
from ops_keep_running_support import OPS

ROOT = OPS.parent
KNOWN = sorted(
    int(path.name.split("_", 1)[0]) for path in (ROOT / "deploy/migrations").glob("*.sql")
)
# The trial runs only migrations in a transaction; one that builds an index
# without blocking writes, such as 0093, is applied for real only.
NEWEST_TRIED = max(
    path for path in (ROOT / "deploy/migrations").glob("*.sql")
    if "BEGIN;" in path.read_text().splitlines()
)
TRIED_PENDING = [number for number in KNOWN if number < int(NEWEST_TRIED.name[:4])]
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


def up(stack, applied: list[int], failing: str = "", reject: str = ""):
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
    stack,
) -> None:
    result = up(stack, [*KNOWN, 999])
    assert result.returncode != 0
    assert "the database has migrations 999 that this release lacks" in result.stderr
    assert not result.stops
    assert "psql trial" not in result.calls


def test_a_failing_migration_leaves_the_old_release_running(stack) -> None:
    # Tried in a transaction that is rolled back: nothing is applied. The
    # newest tried migration fails on its first line, whichever that is.
    result = up(stack, TRIED_PENDING, failing=NEWEST_TRIED.read_text().splitlines()[0])
    assert result.returncode != 0
    assert "a pending migration failed when tried on the running database" in result.stderr
    assert "psql trial" in result.calls
    assert "psql migration" not in result.calls
    assert not result.stops
    assert not [call for call in result.calls if "start clashlens-alert.timer" in call]
    intent = stack["tmp_path"] / "state" / "clashlens" / "alert-intent"
    assert not intent.exists()


def test_a_failed_up_after_stopping_restarts_the_alert_schedule(
    stack,
) -> None:
    # The newest tried migration is still to apply: it is only tried, and
    # rolled back, before anything stops. This test's up then fails after
    # stopping, as a real one can at any later step.
    result = up(stack, TRIED_PENDING)
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


def test_a_rejected_unit_file_stops_nothing_and_changes_nothing(stack) -> None:
    result = up(stack, KNOWN, reject="1")
    assert result.returncode != 0
    assert "unrendered value in clashlens-worker.container" in result.stderr
    assert not result.stops
    # Written only into a scratch folder, which is gone again.
    assert not result.rendered and not result.staged


def test_the_private_api_is_reachable_only_from_inside_the_pod() -> None:
    unit = configparser.ConfigParser(interpolation=None, strict=False, allow_no_value=True)
    unit.optionxform = str
    unit.read(ROOT / "deploy/quadlet/clashlens-api.container")
    command = unit["Container"]["Exec"].split()
    assert command[command.index("--host") + 1] == "127.0.0.1"
    # Its one caller, the website, shares the pod and its loopback address.
    assert set(re.findall(r"CLASHLENS_PYTHON_API_URL=(\S+)", OPS.read_text())) == {
        "http://127.0.0.1:8000"
    }
    assert ":8000" not in (ROOT / "deploy/quadlet/clashlens.pod").read_text()


def unit_settings(name: str) -> list[tuple[str, str]]:
    path = ROOT / "deploy/quadlet" / name
    return [
        tuple(line.split("=", 1))
        for line in path.read_text().splitlines()
        if "=" in line and not line.startswith("#")
    ]


def test_the_database_container_gains_no_privileges_and_writes_only_its_mounts() -> None:
    settings = unit_settings("clashlens-postgres.container")
    for required in (("ReadOnly", "true"), ("NoNewPrivileges", "true"), ("DropCapability", "all")):
        assert required in settings
    # The start script hands the data and socket folders to the postgres user
    # and switches to it; the startup check signals that user's server.
    added = [v for k, v in settings if k == "AddCapability"]
    assert len(added) == 1
    assert set(added[0].split()) == {"CHOWN", "DAC_READ_SEARCH", "FOWNER", "SETUID", "SETGID", "KILL"}
    tmpfs = {v.split(":", 1)[0]: set(v.split(":", 1)[1].split(",")) for k, v in settings if k == "Tmpfs"}
    assert set(tmpfs) == {"/tmp", "/run/postgresql"}
    for options in tmpfs.values():
        assert {"noexec", "nosuid", "nodev"} <= options
        assert any(option.startswith("size=") for option in options)


ONE_OFF_RUNS = r"""
source "$1" help >/dev/null
for check in require_host prepare_managed_runtime load_production_config create_admin_secret cleanup_admin_secret archive_retention_enabled; do
  eval "$check() { :; }"
done
setting() { echo apply; }
psql_exec() { echo 0; }
PODMAN_BIN=$2 STATE_DIR=$3 SPOOL_ROOT=$3/spool MODE=production PREFIX=clashlens
RELEASE=([PYTHON_IMAGE]=sha256:python [FIXTURES_IMAGE]=sha256:fixtures)
mkdir -p "$STATE_DIR/fixture"
: > "$STATE_DIR/fixture/cohort-200.txt"
archive_prune --apply
history_prune
failed_items --limit 1
bootstrap_fixture
"""


def test_every_one_off_container_gains_no_privileges_and_writes_only_its_mounts(tmp_path) -> None:
    calls = tmp_path / "calls"
    podman = tmp_path / "podman"
    podman.write_text(
        f"#!{sys.executable}\nimport json, sys\n"
        f"open({str(calls)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
    )
    podman.chmod(0o700)
    result = subprocess.run(
        ["bash", "-c", ONE_OFF_RUNS, "one-off-test", str(OPS), str(podman), str(tmp_path / "state")],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    runs = [json.loads(line) for line in calls.read_text().splitlines()]
    commands = [run[next(i for i, a in enumerate(run) if a.startswith("sha256:")) + 1] for run in runs]
    assert commands == ["prune-archive", "prune-history", "failed-items", "manifest", "bootstrap-population"]
    for run in runs:
        options = run[: next(i for i, a in enumerate(run) if a.startswith("sha256:"))]
        assert options[:2] == ["run", "--rm"]
        for flag in (
            "--cap-drop=all", "--security-opt=no-new-privileges", "--memory=1g",
            "--pids-limit=256", "--read-only",
        ):
            assert flag in options, (run, flag)
        assert options[options.index("--tmpfs") + 1] == "/tmp:rw,noexec,nosuid,nodev,size=16m"
        assert options[options.index("--pod") + 1] == "clashlens-pod"
    # The administrator login reaches only the two administrator runs.
    admin = [run for run in runs if any(a.startswith("clashlens-init-database-url,") for a in run)]
    assert [run[run.index("--name") + 1] for run in admin] == ["clashlens-admin-operation"] * 2
    assert admin[0][-4:-1] == ["--limit", "1", "--operator"]


def test_application_code_in_the_images_belongs_to_root() -> None:
    # Owned by root, the code stays read-only to the service user even in a
    # container started without a read-only root.
    for containerfile in (ROOT / "Containerfile", ROOT / "python/Containerfile"):
        copies = [line for line in containerfile.read_text().splitlines() if line.startswith("COPY ")]
        assert any("/opt/clashlens/src" in line for line in copies)
        assert not [line for line in copies if "--chown" in line], containerfile


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
