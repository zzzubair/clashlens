from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from clashlens import filesystem
from clashlens.spool import Spool, SpoolError


def test_free_space_is_still_checked_after_the_filesystem_is_known(
    tmp_path: Path,
) -> None:
    spool = Spool(tmp_path / "spool", max_body_bytes=1024, free_space_floor=1 << 20)
    spool.reserve(512).release()
    full = type(
        "V", (), {"f_files": 1000, "f_favail": 900, "f_bavail": 0, "f_frsize": 4096}
    )()
    with mock.patch.object(filesystem.os, "fstatvfs", return_value=full):
        with pytest.raises(SpoolError, match="free-space floor"):
            spool.reserve(512)
    spool.reserve(512).release()
