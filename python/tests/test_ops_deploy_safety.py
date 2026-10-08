"""What ./ops up checks before it stops anything, and what a failed up leaves.

A full up stops the running stack before it starts the new release, so a
release that cannot run must be refused first, and an up that fails after
stopping must say so: until 8 Oct 2026 it left everything stopped with the
alerts switched off."""

import subprocess
from pathlib import Path

from test_ops_keep_running import OPS, stack  # noqa: F401 - the shared fake stack

ROOT = OPS.parent
KNOWN = sorted(
    int(path.name.split("_", 1)[0]) for path in (ROOT / "deploy/migrations").glob("*.sql")
)
# The real up, with the host and configuration checks before stopping skipped.
UP = r"""
source "$1" help >/dev/null
for check in require_host load_release load_production_config validate_runtime_values \
  guard_generated_units guard_existing_resources guard_trusted_proxy_ip guard_network_subnet \
  cleanup_stale_admin_state ensure_linger migrate_legacy_units guard_systemd_units; do
  eval "$check() { :; }"
done
MODE=production PREFIX=clashlens POSTGRES_USER=clashlens POSTGRES_DB=clashlens
RELEASE=([COLLECTOR_IMAGE]=$NEW_COLLECTOR [POSTGRES_IMAGE]=$POSTGRES)
up_stack
"""


def up(stack, applied: list[int]):  # noqa: F811
    tmp_path = stack["tmp_path"]
    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    # The database answers with the migrations it has applied.
    (fake / "podman").write_text(
        '#!/bin/sh\ncase " $* " in *" psql "*) printf "%s\\n" $APPLIED; exit 0 ;; esac\n'
        f'exec {stack["env"]["PODMAN_BIN"]} "$@"\n'
    )
    # Records the failed-deploy alert and what the alert check would see.
    (fake / "python3").write_text(
        '#!/bin/sh\necho "alert $*" >> "$CALLS"\ncat "$3/alert-intent" >> "$CALLS"\n'
    )
    for name in ("podman", "python3"):
        (fake / name).chmod(0o700)
    calls = Path(stack["env"]["CALLS"])
    calls.unlink(missing_ok=True)
    result = subprocess.run(
        ["bash", "-c", UP, "deploy-safety-test", str(OPS)],
        env=dict(
            stack["env"],
            PATH=f"{fake}:/usr/bin:/bin",
            PODMAN_BIN=str(fake / "podman"),
            APPLIED=" ".join(map(str, applied)),
        ),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    result.calls = calls.read_text().splitlines() if calls.exists() else []
    return result


def test_a_release_missing_an_applied_migration_is_refused_before_stopping(
    stack,  # noqa: F811
) -> None:
    result = up(stack, [*KNOWN, 999])
    assert result.returncode != 0
    assert "the database has migrations 999 that this release lacks" in result.stderr
    assert not [call for call in result.calls if call.startswith("--user stop ")]
    assert not [call for call in result.calls if call.startswith("alert ")]


def test_a_failed_up_after_stopping_alerts_that_the_stack_is_stopped(
    stack,  # noqa: F811
) -> None:
    # The newest migration is still to apply: the release may go ahead. This
    # test's up then fails after stopping, as a real one can at any later step.
    result = up(stack, KNOWN[:-1])
    assert result.returncode != 0
    assert "lacks" not in result.stderr
    assert "--user stop clashlens-worker.service" in result.calls
    alert = result.calls.index(
        f"alert {ROOT / 'python/src/clashlens/alerts.py'} --deploy-failed "
        f"{stack['tmp_path'] / 'state' / 'clashlens'} "
        "/srv/clashlens-secrets/clashlens-discord-alert-webhook"
    )
    # The alert check runs while up has failed instead of staying quiet.
    assert result.calls[alert + 1] == "failed"
    intent = stack["tmp_path"] / "state" / "clashlens" / "alert-intent"
    assert intent.read_text() == "failed\n"


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
