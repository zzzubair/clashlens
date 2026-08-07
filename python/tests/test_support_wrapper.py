from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
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
