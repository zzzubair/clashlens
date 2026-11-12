"""One saved answer every API request shares (Live board, known names)."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Hashable
from datetime import datetime
from enum import StrEnum
from threading import Lock, Thread
from typing import Any

# After a shared copy's background read fails, the next waits as long as the
# copy's own refresh interval, then twice as long after each further failure,
# never longer than this, and no wait once a read succeeds. Each failed read
# can hold a pool connection for the full statement timeout, so at this cap a
# read that keeps failing costs at most 10 of every 300 seconds of one
# connection instead of starting again as soon as the last one fails.
SHARED_COPY_RETRY_CAP_SECONDS = 300
# A request that finds no copy to serve waits this long for the read under
# way, then is refused as busy (a retryable 503). Waiting longer held one of
# the API's 40 request threads per visitor until they were all taken and even
# the health checks could not answer, though the database had connections free.
SHARED_COPY_WAIT_SECONDS = 0.05


class CopyState(StrEnum):
    """How old the saved copy is for the period a request asks about.

    - ``MISSING``: no copy yet, or one read for another period (``key``, such
      as an earlier Legend day or Season). The request reads it itself.
    - ``FRESH``: younger than ``seconds``. Served as is.
    - ``STALE``: younger than ``longest``. Served at once while one
      background read replaces it.
    - ``EXPIRED``: ``longest`` or older, because background reads keep
      failing or running late. The request reads it itself.

    Events: a finished read makes the copy ``FRESH``, dated from when the
    read started; time moves it to ``STALE`` and then ``EXPIRED``; a new
    period makes it ``MISSING``. A failed background read is logged and
    leaves the old copy, still served until it is ``EXPIRED``; the next
    ``STALE`` request after its wait (``SHARED_COPY_RETRY_CAP_SECONDS``)
    starts another. A failed request read reaches that
    visitor and changes nothing. Reads take turns, so requests that find the
    copy ``MISSING`` or ``EXPIRED`` wait for the one read under way, up to
    ``SHARED_COPY_WAIT_SECONDS``, and use it when it is ``FRESH``; past that
    wait they are refused as busy. The copy lives in memory only: a restart
    begins ``MISSING``.

    Owner: ``SharedCopy._state`` is the only place the state is decided.
    """

    MISSING = "missing"
    FRESH = "fresh"
    STALE = "stale"
    EXPIRED = "expired"


class CopyBusy(Exception):
    """No copy to serve, and the read under way did not finish within the
    request's wait. The API answers it with a retryable 503 ``busy``."""


def _start_thread(work: Callable[[], None]) -> None:
    Thread(target=work, name="clashlens-shared-copy", daemon=True).start()


class SharedCopy[Copy]:
    """One answer every request shares, read with ``current(database, now)``;
    see ``CopyState`` for when it is served, refreshed or read again."""

    def __init__(
        self,
        read: Callable[[Any, datetime], Copy],
        *,
        key: Callable[[datetime], Hashable],
        seconds: float,
        longest: float,
        name: str,
        clock: Callable[[], float] = time.monotonic,
        start: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        if not 0 < seconds <= longest:
            raise ValueError("a shared copy must refresh before it expires")
        self._read = read
        self._key = key
        self._seconds = seconds
        self._longest = longest
        self._name = name
        self._clock = clock
        self._start = _start_thread if start is None else start
        self._lock = Lock()
        # Held for every read, so one runs at a time.
        self._reading = Lock()
        # (key, clock reading when its read started, copy)
        self._copy: tuple[Hashable, float, Copy] | None = None
        # True while the one background read started by a STALE copy runs.
        self._refreshing = False
        # No background read starts before this clock reading, which is
        # ``_retry_delay`` seconds after the last one failed.
        self._retry_at = float("-inf")
        self._retry_delay = 0.0

    def _state(self, key: Hashable) -> tuple[CopyState, Copy | None]:
        """The copy's state for ``key`` and the copy when it may be served.
        Call with ``_lock`` held."""
        copy = self._copy
        if copy is None or copy[0] != key:
            return CopyState.MISSING, None
        age = self._clock() - copy[1]
        if age < self._seconds:
            return CopyState.FRESH, copy[2]
        if age < self._longest:
            return CopyState.STALE, copy[2]
        return CopyState.EXPIRED, None

    def current(
        self,
        database: Any,
        now: datetime,
        *,
        wait: float | None = SHARED_COPY_WAIT_SECONDS,
    ) -> Copy:
        """The copy for ``now``; with none to serve, waits up to ``wait``
        seconds (None: as long as it takes) for the read under way, then
        raises ``CopyBusy``."""
        key = self._key(now)
        with self._lock:
            state, served = self._state(key)
            if state is CopyState.FRESH:
                return served  # type: ignore[return-value]
            refresh = (
                state is CopyState.STALE
                and not self._refreshing
                and self._clock() >= self._retry_at
            )
            self._refreshing = self._refreshing or refresh
        if refresh:
            self._start(lambda: self._refresh(database, now, key))
        if state is CopyState.STALE:
            return served  # type: ignore[return-value]
        if not self._reading.acquire(timeout=-1 if wait is None else wait):
            raise CopyBusy
        try:
            with self._lock:
                # Read by another request while this one waited.
                state, served = self._state(key)
            if state is CopyState.FRESH:
                return served  # type: ignore[return-value]
            return self._read_copy(database, now, key)
        finally:
            self._reading.release()

    def _read_copy(self, database: Any, now: datetime, key: Hashable) -> Copy:
        started = self._clock()
        copy = self._read(database, now)
        with self._lock:
            self._copy = (key, started, copy)
            self._retry_at = float("-inf")
            self._retry_delay = 0.0
        return copy

    def _refresh(self, database: Any, now: datetime, key: Hashable) -> None:
        try:
            with self._reading:
                with self._lock:
                    # A request may already have read one for a later Legend day.
                    current = self._copy is not None and self._copy[0] == key
                if current:
                    self._read_copy(database, now, key)
        except Exception as error:  # noqa: BLE001 - a later request tries again
            with self._lock:
                self._retry_delay = min(
                    2 * self._retry_delay or self._seconds,
                    SHARED_COPY_RETRY_CAP_SECONDS,
                )
                self._retry_at = self._clock() + self._retry_delay
                retry_in = self._retry_delay
            print(
                json.dumps(
                    {
                        "event": self._name,
                        "status": "refresh_failed",
                        "error": type(error).__name__,
                        "retry_in_seconds": retry_in,
                    }
                ),
                flush=True,
            )
        finally:
            with self._lock:
                self._refreshing = False
