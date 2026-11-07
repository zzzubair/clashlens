"""Whether the collector may save new raw responses to the spool."""

from __future__ import annotations

import asyncio
import errno
import time
from collections.abc import Awaitable, Callable
from enum import StrEnum

from .spool import SpoolError

# A probe that fails waits this long before the next one may run.
PROBE_RETRY_SECONDS = 1.0


class SpoolState(StrEnum):
    """Whether collection may save new raw responses; values are the health reasons.

    - ``WRITABLE``: collection saves responses.
    - ``CAPACITY_EXHAUSTED``: the spool is full, or at a free-space or inode
      floor. Checks return ``capacity_paused`` while uploads and cleanup carry
      on, so archived bytes make room. The next check writes a full-size probe
      file; a failed probe waits PROBE_RETRY_SECONDS before the next, and a
      probe that succeeds makes the spool ``WRITABLE`` again.
    - ``IO_FAILED``: a spool read, write or integrity failure, in the collector
      or the uploads process. A later probe succeeding does not prove the spool
      safe, so collection, cleanup and readiness stay failed until a restart
      rechecks the spool.
    - ``RECOVERY_REQUIRED``: a response saved to the spool could not be
      committed to the database in its turn. Collection stays closed until a
      restart replays every saved response in order (``recover_handoffs``).

    Events: a capacity error (``ENOSPC``, ``EDQUOT`` or a ``degraded_capacity``
    refusal), any other spool error, a failed handoff, and a probe. The two
    closed states latch until restart and are independent of each other and of
    capacity, so each is kept as its own fact: ``/metrics`` reports all three
    and the cleanup loop pauses on ``IO_FAILED`` alone. ``SpoolHealth.state``
    reports the strongest: recovery, then input/output, then capacity.
    """

    WRITABLE = "ready"
    CAPACITY_EXHAUSTED = "degraded_capacity"
    IO_FAILED = "spool_io_failure"
    RECOVERY_REQUIRED = "handoff_recovery_required"


def is_capacity_error(error: OSError | SpoolError) -> bool:
    return (
        isinstance(error, SpoolError) and str(error).startswith("degraded_capacity:")
    ) or (isinstance(error, OSError) and error.errno in {errno.EDQUOT, errno.ENOSPC})


class SpoolHealth:
    """The collector's only owner of ``SpoolState``; lives as long as the process."""

    def __init__(self, count: Callable[[str], None]) -> None:
        self._count = count
        # None while writable; otherwise the monotonic time a probe may next run.
        self._next_probe_at: float | None = None
        self._io_failed = False
        self._recovery_required = False
        self._probe_lock = asyncio.Lock()

    @property
    def state(self) -> SpoolState:
        if self._recovery_required:
            return SpoolState.RECOVERY_REQUIRED
        if self._io_failed:
            return SpoolState.IO_FAILED
        if self._next_probe_at is not None:
            return SpoolState.CAPACITY_EXHAUSTED
        return SpoolState.WRITABLE

    @property
    def capacity_exhausted(self) -> bool:
        return self._next_probe_at is not None

    @property
    def io_failed(self) -> bool:
        return self._io_failed

    @property
    def recovery_required(self) -> bool:
        return self._recovery_required

    def metric_lines(self) -> list[str]:
        return [
            f"clashlens_collector_spool_io_failed {int(self._io_failed)}",
            f"clashlens_collector_spool_capacity_failed {int(self.capacity_exhausted)}",
            (
                "clashlens_collector_handoff_recovery_required "
                f"{int(self._recovery_required)}"
            ),
        ]

    def record_failure(self, error: OSError | SpoolError) -> None:
        if is_capacity_error(error):
            if self._next_probe_at is None:
                self._next_probe_at = 0.0
            self._count("degraded_capacity")
            return
        self.record_io_failure()

    def record_io_failure(self) -> None:
        self._io_failed = True
        self._count("spool_io_failure")

    def require_recovery(self) -> None:
        self._recovery_required = True

    async def collection_open(self, probe: Callable[[], Awaitable[None]]) -> bool:
        """Whether a check may save a response, probing a full spool when due."""
        if self._recovery_required or self._io_failed:
            return False
        if self._next_probe_at is None:
            return True
        async with self._probe_lock:
            if self._next_probe_at is None:
                return True
            if time.monotonic() < self._next_probe_at:
                return False
            try:
                await probe()
            except (OSError, SpoolError) as error:
                self._next_probe_at = time.monotonic() + PROBE_RETRY_SECONDS
                self.record_failure(error)
                return False
            if self._io_failed:
                return False
            self._next_probe_at = None
            self._count("capacity_recovered")
            return True
