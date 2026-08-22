from __future__ import annotations

import errno
import os
import pty
import re
import select
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_support_transfer_is_a_restricted_host_wrapper() -> None:
    wrapper = ROOT / "deploy" / "support-transfer"

    assert wrapper.is_file()
    assert stat.S_IMODE(wrapper.stat().st_mode) & stat.S_IXUSR
    text = wrapper.read_text(encoding="utf-8")
    assert "SUDO_USER" in text
    assert "SUDO_UID" in text
    assert "clashlens_support_transfer" in text
    assert "verification_request_id" in text
    assert "reason" in text
    assert "player_tag" in text
    assert "from_account_public_id" in text
    assert "to_account_public_id" in text
    assert "PGSERVICEFILE" in text
    assert "PGSERVICE=clashlens_support_transfer" in text
    assert "SELECT status FROM clashlens_support_transfer" in text
    assert "/usr/bin/psql" in text
    assert 'protected_root_file "$script_path"' in text
    assert "(8#$mode & 0077)" in text
    assert "CLASHLENS_DATABASE_URL" not in text
    assert "token" not in text.lower()
    assert re.search(r"(?<!SUDO_)\$\{?USER\b", text) is None

    shell = shutil.which("bash")
    assert shell is not None
    syntax = subprocess.run(
        [shell, "-n", str(wrapper)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert syntax.returncode == 0, syntax.stderr

    rejected = subprocess.run(
        [
            str(wrapper),
            "--verification-request-id",
            "00000000-0000-4000-8000-000000000029",
            "--player-tag",
            "#2PP",
            "--from-account-public-id",
            "00000000-0000-4000-8000-000000000030",
            "--to-account-public-id",
            "00000000-0000-4000-8000-000000000031",
            "--reason",
            "sentinel-player-secret-must-not-appear",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "SUDO_USER": "untrusted", "SUDO_UID": "1000"},
    )
    assert rejected.returncode != 0
    assert rejected.stdout.strip() == "support_transfer_status=denied"
    assert "sentinel-player-secret-must-not-appear" not in (
        rejected.stdout + rejected.stderr
    )


def test_support_recovery_is_a_restricted_host_wrapper() -> None:
    wrapper = ROOT / "deploy" / "support-recovery"

    assert wrapper.is_file()
    assert stat.S_IMODE(wrapper.stat().st_mode) & stat.S_IXUSR
    text = wrapper.read_text(encoding="utf-8")
    assert "SUDO_USER" in text
    assert "SUDO_UID" in text
    # The operator identity is derived from the sudo environment, never a
    # human-supplied argument.
    assert 'operator_identity="sudo:${SUDO_USER}:${SUDO_UID}"' in text
    assert re.search(r"(?<!SUDO_)\$\{?USER\b", text) is None
    assert "/etc/clashlens/support-recovery-operators" in text
    assert "/etc/clashlens/support-recovery.conf" in text
    assert 'grep -Fqx -- "$SUDO_USER" "$OPERATOR_ALLOWLIST"' in text
    assert "support-recovery-exec" in text
    assert '--operator="$operator_identity"' in text
    assert "--target-account-public-id" in text
    assert "--player-tag" in text
    assert "--discord-user-id" in text
    assert "--reason" in text
    assert 'protected_root_file "$script_path"' in text
    assert '(8#$mode & 0077)' in text
    # The wrapper enters the configured deployment account's rootless Podman
    # context; deploy.sh owns runtime binary and container-name overrides.
    assert '"$RUNUSER" --user "$service_account"' in text
    assert 'XDG_RUNTIME_DIR="/run/user/$service_uid"' in text
    assert '"$deploy_script" support-recovery-exec' in text
    assert "read_recovery_token || status_unavailable" in text
    assert "printf '%s\\n' \"$recovery_token\"" in text
    assert "unset recovery_token" in text
    assert "clashlens-python-api" not in text
    assert "/usr/bin/podman" not in text
    assert "CLASHLENS_DATABASE_URL" not in text
    assert re.search(r"--official-(key-file|proxy-url)", text) is None
    assert re.search(r"--[a-z-]*token", text) is None
    # Expected CLI refusals exit 1 but still carry a safe JSON status. The
    # wrapper must parse those instead of collapsing every nonzero exit into
    # an unavailable result; unexpected exit/status pairs stay unavailable.
    assert "recovery_exit=$?" in text
    assert "refused_collision:1" in text
    assert "attached:0" in text
    assert "status_unavailable" in text

    shell = shutil.which("bash")
    assert shell is not None
    syntax = subprocess.run(
        [shell, "-n", str(wrapper)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert syntax.returncode == 0, syntax.stderr

    rejected = subprocess.run(
        [
            str(wrapper),
            "--target-account-public-id",
            "00000000-0000-4000-8000-00000000abcd",
            "--player-tag",
            "#2PP",
            "--discord-user-id",
            "1234567890123456789",
            "--reason",
            "sentinel-recovery-secret-must-not-appear",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "SUDO_USER": "untrusted", "SUDO_UID": "1000"},
    )
    assert rejected.returncode != 0
    assert rejected.stdout.strip() == "support_recovery_status=denied"
    assert "sentinel-recovery-secret-must-not-appear" not in (
        rejected.stdout + rejected.stderr
    )

    denied_without_sudo = subprocess.run(
        [str(wrapper)],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "SUDO_USER": "", "SUDO_UID": ""},
    )
    assert denied_without_sudo.returncode != 0
    assert denied_without_sudo.stdout.strip() == "support_recovery_status=denied"


def test_support_recovery_token_prompt_is_visible_and_does_not_echo() -> None:
    wrapper_text = (ROOT / "deploy" / "support-recovery").read_text(encoding="utf-8")
    prompt_function = re.search(
        r"(?ms)^read_recovery_token\(\) \{\n.*?^\}\n", wrapper_text
    )
    assert prompt_function is not None

    shell = shutil.which("bash")
    assert shell is not None
    secret = "sentinel-pty-token"
    probe = f"""{prompt_function.group(0)}
recovery_token=''
read_recovery_token || exit 1
printf 'accepted_length=%s\\n' "${{#recovery_token}}"
unset recovery_token
"""
    pid, master_fd = pty.fork()
    if pid == 0:
        os.execv(shell, [shell, "-c", probe])

    output = b""
    status: int | None = None
    try:
        deadline = time.monotonic() + 5
        prompt = b"Current in-game API token: "
        accepted = f"accepted_length={len(secret)}".encode()
        while prompt not in output:
            readable, _, _ = select.select(
                [master_fd], [], [], max(0, deadline - time.monotonic())
            )
            assert readable, output.decode(errors="replace")
            output += os.read(master_fd, 4096)

        os.write(master_fd, secret.encode() + b"\n")
        while accepted not in output:
            readable, _, _ = select.select(
                [master_fd], [], [], max(0, deadline - time.monotonic())
            )
            assert readable, output.decode(errors="replace")
            try:
                output += os.read(master_fd, 4096)
            except OSError as error:
                assert error.errno == errno.EIO
                break
        _, status = os.waitpid(pid, 0)
    finally:
        os.close(master_fd)
        if status is None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)

    decoded = output.decode(errors="replace")
    assert os.waitstatus_to_exitcode(status) == 0
    assert "Current in-game API token: " in decoded
    assert secret not in decoded
    assert f"accepted_length={len(secret)}" in decoded


def test_replay_request_is_a_restricted_host_wrapper() -> None:
    wrapper = ROOT / "deploy" / "replay-request"

    assert wrapper.is_file()
    assert stat.S_IMODE(wrapper.stat().st_mode) & stat.S_IXUSR
    text = wrapper.read_text(encoding="utf-8")
    assert "SUDO_USER" in text
    assert "SUDO_UID" in text
    assert "clashlens_request_python_replay_v2" in text
    assert "observation_id" in text
    assert "reason" in text
    assert "PGSERVICEFILE" in text
    assert "PGSERVICE=clashlens_replay_request" in text
    assert "SELECT request_id, job_id, request_status" in text
    assert "/usr/bin/psql" in text
    assert "statement_timeout" in text
    assert "lock_timeout" in text
    assert 'protected_root_file "$script_path"' in text
    assert "(8#$mode & 0077)" in text
    assert "supercell-source-parser-v2" in text
    assert "supercell-source-parser-v1" in text
    assert "clashlens-domain-processing-v1" in text
    assert "clashlens-domain-rules-v1" in text
    assert "legend-analytics-v1" in text
    assert "CLASHLENS_DATABASE_URL" not in text
    assert "token" not in text.lower()
    assert "--parser-version" in text
    assert "--processing-version" not in text
    assert "--domain-rule-version" not in text
    assert "--analytics-rule-version" not in text
    assert re.search(r"(?<!SUDO_)\$\{?USER\b", text) is None

    shell = shutil.which("bash")
    assert shell is not None
    syntax = subprocess.run(
        [shell, "-n", str(wrapper)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert syntax.returncode == 0, syntax.stderr

    rejected = subprocess.run(
        [
            str(wrapper),
            "--observation-id",
            "7",
            "--reason",
            "sentinel-replay-secret-must-not-appear",
            "--parser-version",
            "profile-parser-v2",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "SUDO_USER": "untrusted", "SUDO_UID": "1000"},
    )
    assert rejected.returncode != 0
    assert rejected.stdout.strip() == "replay_request_status=denied"
    assert "sentinel-replay-secret-must-not-appear" not in (
        rejected.stdout + rejected.stderr
    )
