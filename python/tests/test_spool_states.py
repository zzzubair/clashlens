"""The spool's named states: collection pause, reservations and where a raw
body stands (issue #617, entries 8 to 10). The publication barrier is tested
in test_spool.py."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import time
from pathlib import Path

import pytest

import clashlens.spool as spool_module
from clashlens.raw_body import RawBodyStage, may_delete, raw_body_stage
from clashlens.spool import ReservationState, Spool, SpoolError
from clashlens.spool_health import SpoolHealth, SpoolState


def _health() -> tuple[SpoolHealth, dict[str, int]]:
    counts: dict[str, int] = {}
    return SpoolHealth(lambda outcome: counts.update({outcome: counts.get(outcome, 0) + 1})), counts


def test_a_full_spool_pauses_collection_until_a_probe_writes() -> None:
    health, counts = _health()
    probes: list[str] = []
    outcomes = iter([OSError(errno.ENOSPC, "still full"), None])

    async def probe() -> None:
        probes.append("probe")
        if (error := next(outcomes)) is not None:
            raise error

    async def scenario() -> None:
        assert health.state is SpoolState.WRITABLE
        assert await health.collection_open(probe) is True
        health.record_failure(SpoolError("degraded_capacity: spool reservation denied"))
        assert health.state is SpoolState.CAPACITY_EXHAUSTED
        # The first probe fails, so the next waits a second.
        assert await health.collection_open(probe) is False
        assert await health.collection_open(probe) is False
        assert probes == ["probe"]
        health._next_probe_at = 0.0
        assert await health.collection_open(probe) is True
        assert probes == ["probe", "probe"]

    asyncio.run(scenario())
    assert health.state is SpoolState.WRITABLE
    assert counts == {"degraded_capacity": 2, "capacity_recovered": 1}


def test_closed_states_hold_until_restart_and_keep_each_fact() -> None:
    health, counts = _health()
    probed: list[str] = []

    async def probe() -> None:
        probed.append("probe")

    health.record_failure(OSError(errno.EDQUOT, "quota"))
    health.record_failure(OSError(errno.EIO, "disk"))
    assert health.state is SpoolState.IO_FAILED
    # An input/output failure is not cleared by a probe: none even runs.
    assert asyncio.run(health.collection_open(probe)) is False
    assert probed == []
    health.require_recovery()
    assert health.state is SpoolState.RECOVERY_REQUIRED
    assert (health.capacity_exhausted, health.io_failed, health.recovery_required) == (
        True,
        True,
        True,
    )
    assert health.metric_lines() == [
        "clashlens_collector_spool_io_failed 1",
        "clashlens_collector_spool_capacity_failed 1",
        "clashlens_collector_handoff_recovery_required 1",
    ]
    assert counts == {"degraded_capacity": 1, "spool_io_failure": 1}


def test_a_probe_that_hits_an_input_output_failure_closes_collection() -> None:
    health, counts = _health()

    async def probe() -> None:
        raise OSError(errno.EIO, "disk")

    health.record_failure(OSError(errno.ENOSPC, "full"))
    assert asyncio.run(health.collection_open(probe)) is False
    assert health.state is SpoolState.IO_FAILED
    assert health.capacity_exhausted
    assert counts == {"degraded_capacity": 1, "spool_io_failure": 1}


def test_a_reservation_moves_reserved_writing_and_back_to_unreserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = Spool(tmp_path / "spool", max_body_bytes=1024)
    body = b"exact response"
    digest = hashlib.sha256(body).hexdigest()
    reservation = spool.reserve()
    assert reservation.state is ReservationState.RESERVED
    seen: list[tuple[ReservationState, int]] = []
    link = os.link

    def observe_link(source: str, *args: object, **kwargs: object) -> None:
        # The body is in its temporary file; an old one is still kept.
        tmp = tmp_path / "spool" / "tmp" / source
        os.utime(tmp, (time.time() - 3600, time.time() - 3600))
        seen.append((reservation.state, spool.cleanup_stale(60.0)))
        link(source, *args, **kwargs)

    monkeypatch.setattr(spool_module.os, "link", observe_link)
    reservation.publish(body, digest)

    assert seen == [(ReservationState.WRITING, 0)]
    assert reservation.state is ReservationState.UNRESERVED
    assert list((tmp_path / "spool" / "tmp").iterdir()) == []
    assert spool.stats()["reserved_objects"] == 0
    with reservation:
        assert reservation.state is ReservationState.RESERVED
    assert reservation.state is ReservationState.UNRESERVED
    spool.close()


def test_a_failed_write_returns_the_reservation_to_reserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = Spool(tmp_path / "spool", max_body_bytes=1024)
    body = b"exact response"
    digest = hashlib.sha256(body).hexdigest()

    fsync = os.fsync
    failures = iter([OSError(errno.EIO, "disk")])

    def fsync_failing_once(fd: int) -> None:
        if (error := next(failures, None)) is not None:
            raise error
        fsync(fd)

    reservation = spool.reserve()
    monkeypatch.setattr(spool_module.os, "fsync", fsync_failing_once)
    with pytest.raises(OSError):
        spool.publish(body, digest, reservation)
    assert reservation.state is ReservationState.RESERVED
    assert list((tmp_path / "spool" / "tmp").iterdir()) == []
    spool.close()
    assert reservation.state is ReservationState.UNRESERVED


@pytest.mark.parametrize(
    ("handed_off", "recorded", "finished", "stage"),
    [
        (True, True, True, RawBodyStage.HANDED_OFF),
        (True, False, False, RawBodyStage.HANDED_OFF),
        (False, True, True, RawBodyStage.DELETABLE),
        (False, True, False, RawBodyStage.RECORDED),
        (False, False, False, RawBodyStage.UNREFERENCED),
    ],
)
def test_a_sidecar_always_keeps_the_body(
    handed_off: bool, recorded: bool, finished: bool, stage: RawBodyStage
) -> None:
    found = raw_body_stage(handed_off=handed_off, recorded=recorded, finished=finished)
    assert found is stage
    assert may_delete(found) is (stage in {RawBodyStage.DELETABLE, RawBodyStage.UNREFERENCED})


def test_each_raw_body_stage_decides_what_cleanup_deletes(tmp_path: Path) -> None:
    spool = Spool(tmp_path / "spool", max_body_bytes=1024)
    hashes: dict[RawBodyStage, str] = {}
    for stage in (
        RawBodyStage.HANDED_OFF,
        RawBodyStage.RECORDED,
        RawBodyStage.DELETABLE,
        RawBodyStage.UNREFERENCED,
    ):
        body = stage.value.encode()
        hashes[stage] = hashlib.sha256(body).hexdigest()
        payload = f'{{"response_hash":"{hashes[stage]}"}}'.encode()
        with spool.reservation() as reservation:
            spool.publish_handoff(body, hashes[stage], stage.value, payload, reservation)
        if stage is not RawBodyStage.HANDED_OFF:
            # The database commit removes the sidecar.
            spool.remove_handoff(stage.value)
    database = {hashes[RawBodyStage.RECORDED], hashes[RawBodyStage.DELETABLE]}

    # The once-a-minute sweep removes only what nothing names.
    assert spool.remove_unreferenced(lambda: database) == 1
    assert hashes[RawBodyStage.UNREFERENCED] not in spool.final_hashes()
    # Cleanup of finished uploads skips a body a sidecar names again.
    with spool.delete_unreferenced_batch() as delete:
        assert delete(hashes[RawBodyStage.HANDED_OFF]) is False
        assert delete(hashes[RawBodyStage.DELETABLE]) is True
    assert spool.final_hashes() == {
        hashes[RawBodyStage.HANDED_OFF],
        hashes[RawBodyStage.RECORDED],
    }
    spool.close()
