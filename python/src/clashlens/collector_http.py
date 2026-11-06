from __future__ import annotations

import asyncio
import http.client
import math
import socket
import sys
import threading
import weakref
from collections import deque
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from enum import StrEnum
from time import monotonic
from typing import Any, TypeVar
from urllib.parse import quote, urljoin, urlsplit

import certifi
import urllib3
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import (
    ConnectTimeoutError,
    LocationParseError,
    NameResolutionError,
    NewConnectionError,
)
from urllib3.util import connection as urllib3_connection

MAX_REDIRECTS = 3
SAFE_RESPONSE_HEADERS = (
    "cache-control",
    "content-type",
    "date",
    "etag",
    "last-modified",
    "retry-after",
)


class ProviderFailure(RuntimeError):
    def __init__(
        self,
        category: str,
        *,
        retryable: bool,
        http_status: int | None = None,
        key_label: str | None = None,
    ) -> None:
        super().__init__(category)
        self.category = category
        self.retryable = retryable
        self.http_status = http_status
        self.key_label = key_label


class CollectionWindowClosed(RuntimeError):
    """A request would start after its work's collection window closed."""


class _DetachedTimeout(ProviderFailure):
    def __init__(self, release_when: asyncio.Future[None]) -> None:
        super().__init__("timeout", retryable=True)
        self.release_when = release_when


class _DetachedCancellation(asyncio.CancelledError):
    def __init__(self, release_when: asyncio.Future[None]) -> None:
        super().__init__()
        self.release_when = release_when


@dataclass(frozen=True, slots=True)
class ApiKey:
    label: str
    value: str = field(repr=False)


class _StartLimiter:
    def __init__(self, starts_per_second: int) -> None:
        if not 1 <= starts_per_second <= 29:
            raise ValueError("request start rate must be between 1 and 29")
        self._interval = 1.0 / starts_per_second
        self._next_start = 0.0
        self._recent: deque[float] = deque(maxlen=starts_per_second)

    async def wait(self) -> None:
        # No second ever holds more starts than the rate allows.
        window = self._recent[0] + 1.0 if len(self._recent) == self._recent.maxlen else 0.0
        delay = max(self._next_start, window) - monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

    def started(self) -> None:
        # A start that woke late keeps the schedule, so the next starts make up
        # the lost time instead of each adding its own delay; an idle key
        # starts a new schedule.
        now = monotonic()
        on_schedule = now - self._next_start < 1.0
        self._next_start = (self._next_start if on_schedule else now) + self._interval
        self._recent.append(now)


class KeyCondition(StrEnum):
    """Whether one API key may start a request; ``_KeyState.condition`` decides.

    - READY: may start, within its start rate and concurrency cap.
    - PAUSED: a rate limit (429) paused it until ``paused_until``; it becomes
      READY again by itself when that time passes, and a later 429 can only
      extend the pause.
    - QUARANTINED: the API rejected the key (401 or 403). It wins over a
      pause and lasts until the collector restarts, which starts every key
      READY. A provider outage never changes a key's condition.
    """

    READY = "ready"
    PAUSED = "paused"
    QUARANTINED = "quarantined"


@dataclass(slots=True)
class _KeyState:
    key: ApiKey
    limiter: _StartLimiter
    start_lock: asyncio.Lock
    quarantined: bool = False
    paused_until: float = 0.0
    starts: int = 0
    active: int = 0  # slots held by requests: HELD or DRAINING

    def condition(self, now: float) -> KeyCondition:
        if self.quarantined:
            return KeyCondition.QUARANTINED
        if self.paused_until > now:
            return KeyCondition.PAUSED
        return KeyCondition.READY

    async def wait_out_pause(self) -> None:
        delay = self.paused_until - monotonic()
        if delay > 0:
            await asyncio.sleep(delay)


class SlotPhase(StrEnum):
    """Where one reserved request slot stands: a key's or a network thread's.

    - HELD: reserved by a request that is waiting to start or running.
    - DRAINING: its caller gave up (timeout or cancellation) while the
      blocking network call still runs; the slot stays taken until that call
      ends, so the call keeps counting against the key and thread limits.
    - RELEASED: handed back, exactly once.

    ``_RequestSlot`` is the only owner. Slots live in memory; a restart
    starts with none held.
    """

    HELD = "held"
    DRAINING = "draining"
    RELEASED = "released"


class _RequestSlot:
    def __init__(self, release: Callable[[], None]) -> None:
        self.phase = SlotPhase.HELD
        self._release = release

    def release(self) -> None:
        if self.phase is not SlotPhase.RELEASED:
            self.phase = SlotPhase.RELEASED
            self._release()

    def end(self, error: BaseException) -> None:
        """Release after ``error``: now, or when a detached network call ends."""
        if isinstance(error, (_DetachedTimeout, _DetachedCancellation)):
            self.phase = SlotPhase.DRAINING
            error.release_when.add_done_callback(lambda _finished: self.release())
        else:
            self.release()


T = TypeVar("T")
StartRequest = Callable[[], Awaitable[None]]


_request_context = threading.local()


class DeadlinePhase(StrEnum):
    """Where one blocking request stands against its wall-clock budget.

    - WAITING: its network thread has not begun the request.
    - RUNNING: the thread began (``begin``); the connection it uses is
      tracked so an expiry can interrupt it.
    - FINISHED: it ended within the budget (``complete``) or was cleaned up
      after running (``finish``); nothing is tracked.
    - ABANDONED: the budget ran out, the caller was cancelled, or cleanup
      came before the thread began; the thread will refuse to begin.
    - INTERRUPTED: the budget ran out, or the caller was cancelled, after the
      thread began; its connection was shut so the blocking call ends soon.

    ``_RequestDeadline`` owns the phase behind one lock shared with the
    network thread. Only ``expire`` (budget over, or cancellation once
    running) tells the waiting caller. A caller that stops waiting on an
    INTERRUPTED request keeps its key slot and network thread DRAINING
    (``SlotPhase``) until the blocking call returns. A retry is a new request
    with a new deadline; nothing survives a restart.
    """

    WAITING = "waiting"
    RUNNING = "running"
    FINISHED = "finished"
    ABANDONED = "abandoned"
    INTERRUPTED = "interrupted"


_DEADLINE_EXPIRED = (DeadlinePhase.ABANDONED, DeadlinePhase.INTERRUPTED)


class _RequestDeadline:
    """Interrupt one blocking urllib3 request when its wall-clock budget ends."""

    def __init__(
        self,
        seconds: float,
        loop: asyncio.AbstractEventLoop,
        timeout_reached: asyncio.Future[None],
    ) -> None:
        self.ends_at = monotonic() + seconds
        self._loop = loop
        self._timeout_reached = timeout_reached
        self._lock = threading.Lock()
        self._phase = DeadlinePhase.WAITING
        # Only ever set while RUNNING; every move out of RUNNING clears it.
        self._connection: HTTPConnection | socket.socket | None = None

    @property
    def phase(self) -> DeadlinePhase:
        with self._lock:
            return self._phase

    @property
    def started(self) -> bool:
        return self.phase in (
            DeadlinePhase.RUNNING,
            DeadlinePhase.FINISHED,
            DeadlinePhase.INTERRUPTED,
        )

    @property
    def expired(self) -> bool:
        return self.phase in _DEADLINE_EXPIRED

    def begin(self) -> bool:
        with self._lock:
            if self._phase not in (DeadlinePhase.WAITING, DeadlinePhase.RUNNING):
                return False
            self._phase = DeadlinePhase.RUNNING
            return True

    def register(self, connection: HTTPConnection | socket.socket) -> None:
        with self._lock:
            if self._phase in _DEADLINE_EXPIRED:
                _close_connection(connection)
            elif self._phase is DeadlinePhase.RUNNING:
                previous = self._connection
                self._connection = connection
                if isinstance(previous, socket.socket):
                    previous.close()

    def clear(self, connection: HTTPConnection | socket.socket | None) -> None:
        with self._lock:
            if self._connection is connection:
                self._connection = None

    def guard_connected_socket(self, connected: socket.socket) -> None:
        with self._lock:
            if self._phase is not DeadlinePhase.RUNNING:
                _close_connection(connected)
                raise ProviderFailure("timeout", retryable=True)
            if self._connection is connected:
                self._connection = connected.dup()

    def remaining(self) -> float:
        remaining = self.ends_at - monotonic()
        if remaining <= 0:
            self.expire()
            raise ProviderFailure("timeout", retryable=True)
        return remaining

    def complete(self) -> None:
        with self._lock:
            if self._phase in _DEADLINE_EXPIRED or monotonic() >= self.ends_at:
                self._move(
                    DeadlinePhase.ABANDONED
                    if self._phase is DeadlinePhase.ABANDONED
                    else DeadlinePhase.INTERRUPTED,
                    close=True,
                )
                raise ProviderFailure("timeout", retryable=True)
            self._move(DeadlinePhase.FINISHED, close=False)

    def expire(self) -> None:
        with self._lock:
            if self._phase is DeadlinePhase.WAITING:
                self._move(DeadlinePhase.ABANDONED, close=True)
            elif self._phase is DeadlinePhase.RUNNING:
                self._move(DeadlinePhase.INTERRUPTED, close=True)
            else:
                return
        self._loop.call_soon_threadsafe(self._signal_timeout)

    def cancel_before_start(self) -> bool:
        with self._lock:
            if self._phase not in (DeadlinePhase.WAITING, DeadlinePhase.ABANDONED):
                return False
            self._phase = DeadlinePhase.ABANDONED
            return True

    def finish(self) -> None:
        with self._lock:
            if self._phase is DeadlinePhase.WAITING:
                self._phase = DeadlinePhase.ABANDONED
            elif self._phase is DeadlinePhase.RUNNING:
                self._phase = DeadlinePhase.FINISHED
            connection = self._connection
            self._connection = None
            if isinstance(connection, socket.socket):
                connection.close()

    def _move(self, phase: DeadlinePhase, *, close: bool) -> None:
        """Leave RUNNING (or stay ended), dropping the tracked connection."""
        connection = self._connection
        self._connection = None
        self._phase = phase
        if close and connection is not None:
            _close_connection(connection)

    def _signal_timeout(self) -> None:
        if not self._timeout_reached.done():
            self._timeout_reached.set_result(None)


def _close_connection(connection: HTTPConnection | socket.socket) -> None:
    sock = connection if isinstance(connection, socket.socket) else connection.sock
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    connection.close()


def _register_request_connection(connection: HTTPConnection) -> None:
    deadline = getattr(_request_context, "deadline", None)
    if deadline is not None:
        deadline.register(connection)


def _new_deadline_socket(connection: HTTPConnection) -> socket.socket:
    host = connection._dns_host
    if host.startswith("["):
        host = host.strip("[]")
    try:
        host.encode("idna")
    except UnicodeError:
        raise LocationParseError(f"'{host}', label empty or too long") from None

    deadline = getattr(_request_context, "deadline", None)
    family = urllib3_connection.allowed_gai_family()
    try:
        addresses = socket.getaddrinfo(
            host, connection.port, family, socket.SOCK_STREAM
        )
    except socket.gaierror as error:
        raise NameResolutionError(connection.host, connection, error) from error

    last_error: OSError | None = None
    for address in addresses:
        if deadline is not None:
            deadline.remaining()
        af, socktype, proto, _canonname, socket_address = address
        sock = None
        try:
            sock = socket.socket(af, socktype, proto)
            urllib3_connection._set_socket_options(sock, connection.socket_options)
            if connection.timeout is not urllib3.Timeout.DEFAULT_TIMEOUT:
                sock.settimeout(connection.timeout)
            if connection.source_address:
                sock.bind(connection.source_address)
            if deadline is not None:
                deadline.register(sock)
                deadline.remaining()
            sock.connect(socket_address)
            if deadline is not None:
                deadline.guard_connected_socket(sock)
            sys.audit("http.client.connect", connection, connection.host, connection.port)
            return sock
        except ProviderFailure:
            if sock is not None:
                sock.close()
            raise
        except OSError as error:
            last_error = error
            if sock is not None:
                sock.close()

    if isinstance(last_error, TimeoutError):
        raise ConnectTimeoutError(
            connection,
            f"Connection to {connection.host} timed out. "
            f"(connect timeout={connection.timeout})",
        ) from last_error
    if last_error is not None:
        raise NewConnectionError(
            connection, f"Failed to establish a new connection: {last_error}"
        ) from last_error
    raise NewConnectionError(connection, "getaddrinfo returned no addresses")


class _DeadlineHTTPConnection(HTTPConnection):
    def _new_conn(self) -> socket.socket:
        return _new_deadline_socket(self)

    def connect(self) -> None:
        super().connect()
        _register_request_connection(self)

    def request(self, *args: object, **kwargs: object) -> None:
        _register_request_connection(self)
        super().request(*args, **kwargs)


class _OpenTunnels:
    """Relay connections that finished connecting and have not closed."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._open: weakref.WeakSet[HTTPSConnection] = weakref.WeakSet()

    def add(self, connection: HTTPSConnection) -> None:
        with self._lock:
            self._open.add(connection)

    def discard(self, connection: HTTPSConnection) -> None:
        with self._lock:
            self._open.discard(connection)

    def snapshot(self) -> set[HTTPSConnection]:
        with self._lock:
            return set(self._open)


class _DeadlineHTTPSConnection(HTTPSConnection):
    tunnels: _OpenTunnels | None = None

    def _new_conn(self) -> socket.socket:
        return _new_deadline_socket(self)

    def connect(self) -> None:
        super().connect()
        if self.tunnels is not None:
            self.tunnels.add(self)
        _register_request_connection(self)

    def close(self) -> None:
        if self.tunnels is not None:
            self.tunnels.discard(self)
        super().close()

    def _tunnel(self) -> None:
        try:
            super()._tunnel()
        except (OSError, http.client.HTTPException):
            self.close()
            raise

    def request(self, *args: object, **kwargs: object) -> None:
        _register_request_connection(self)
        super().request(*args, **kwargs)


class _DeadlineHTTPConnectionPool(HTTPConnectionPool):
    ConnectionCls = _DeadlineHTTPConnection


class _DeadlineHTTPSConnectionPool(HTTPSConnectionPool):
    ConnectionCls = _DeadlineHTTPSConnection
    tunnels: _OpenTunnels | None = None

    def _new_conn(self) -> HTTPSConnection:
        connection = super()._new_conn()
        connection.tunnels = self.tunnels
        return connection


class _DeadlinePoolManager(urllib3.PoolManager):
    def __init__(self, **connection_pool_kw: object) -> None:
        super().__init__(**connection_pool_kw)
        self.pool_classes_by_scheme = {
            "http": _DeadlineHTTPConnectionPool,
            "https": _DeadlineHTTPSConnectionPool,
        }


class _DeadlineProxyManager(urllib3.ProxyManager):
    def __init__(
        self, proxy_url: str, *, tunnels: _OpenTunnels, **connection_pool_kw: object
    ) -> None:
        super().__init__(proxy_url, **connection_pool_kw)
        self.tunnels = tunnels
        self.pool_classes_by_scheme = {
            "http": _DeadlineHTTPConnectionPool,
            "https": _DeadlineHTTPSConnectionPool,
        }

    def _new_pool(self, *args: Any, **kwargs: Any) -> HTTPConnectionPool:
        pool = super()._new_pool(*args, **kwargs)
        pool.tunnels = self.tunnels
        return pool


class OutagePhase(StrEnum):
    """Where the shared provider-outage pause stands; see ``ProviderOutage``."""

    ANSWERING = "answering"
    PAUSED = "paused"
    PROBE_DUE = "probe_due"
    PROBING = "probing"


@dataclass(frozen=True, slots=True)
class _Answering:
    failures: int = 0  # server errors or transport failures in a row


@dataclass(frozen=True, slots=True)
class _Paused:
    delay: float  # this pause's length; a failed probe doubles it
    until: float  # monotonic time the recovery probe becomes due


class ProviderOutage:
    """One pause for every key after the provider stops answering.

    Phases (``phase()``), moved only by ``record``, ``admit`` and ``stop``:

    - ANSWERING: requests go out. ``threshold`` transport failures or server
      errors in a row, with no other answer between them, start a pause of
      ``base_delay`` seconds.
    - PAUSED: every request waits instead of going out.
    - PROBE_DUE: the pause is over; the next request to ask becomes the one
      recovery probe.
    - PROBING: the probe is out and every other request waits for it.

    Any answer from any request, including a rate limit or a rejected key,
    ends the outage (back to ANSWERING). A probe that fails doubles the pause
    up to ``max_delay`` (back to PAUSED); a probe that ends without an answer
    or a failure (cancelled, or refused without retry) lets the next request
    probe. Other requests' failures during an outage change nothing. A probe
    can outlive its outage when another request's answer ended it; until it
    reports, no new probe goes out, and its failure doubles a later pause.

    ``stop()`` at shutdown is a latch, not a phase: from then on, while an
    outage lasts, waiting requests fail at once as a retryable
    ``provider_outage``. The state lives in memory only, so a restarted
    collector starts ANSWERING. Key health is untouched, so an outage never
    disables or pauses a key.
    """

    def __init__(
        self,
        *,
        threshold: int = 10,
        base_delay: float = 5.0,
        max_delay: float = 60.0,
    ) -> None:
        self.threshold = threshold
        self.base_delay = base_delay
        self.max_delay = max_delay
        self._state: _Answering | _Paused = _Answering()
        self._probe_out = False
        self._stopping = False
        self._changed = asyncio.Event()

    def phase(self) -> OutagePhase:
        return self._phase_at(monotonic())

    def _phase_at(self, now: float) -> OutagePhase:
        state = self._state
        if isinstance(state, _Answering):
            return OutagePhase.ANSWERING
        if now < state.until:
            return OutagePhase.PAUSED
        return OutagePhase.PROBING if self._probe_out else OutagePhase.PROBE_DUE

    @property
    def active(self) -> bool:
        return isinstance(self._state, _Paused)

    @property
    def paused(self) -> bool:
        """Whether requests now wait; false once a recovery probe is due."""
        return self.phase() is OutagePhase.PAUSED

    async def admit(self) -> bool:
        """Wait out a pause; return whether this request is the recovery probe."""
        while True:
            now = monotonic()
            phase = self._phase_at(now)
            state = self._state
            if not isinstance(state, _Paused):
                return False
            if self._stopping:
                raise ProviderFailure("provider_outage", retryable=True)
            if phase is OutagePhase.PROBE_DUE:
                self._probe_out = True
                return True
            changed = self._changed
            try:
                await asyncio.wait_for(
                    changed.wait(),
                    state.until - now if phase is OutagePhase.PAUSED else None,
                )
            except TimeoutError:
                pass

    def record(self, *, failed: bool | None, probe: bool) -> None:
        """Record one answer: failed, answered (False), or neither (None)."""
        if probe:
            self._probe_out = False
        state = self._state
        if failed is None:
            if probe:
                self._notify()
        elif not failed:
            self._state = _Answering()
            if isinstance(state, _Paused):
                self._notify()
        elif isinstance(state, _Paused):
            if probe:
                self._pause(min(self.max_delay, state.delay * 2))
        elif state.failures + 1 >= self.threshold:
            self._pause(self.base_delay)
        else:
            self._state = _Answering(state.failures + 1)

    def stop(self) -> None:
        """Release waiting requests as retryable failures during shutdown."""
        self._stopping = True
        self._notify()

    def _pause(self, delay: float) -> None:
        self._state = _Paused(delay, monotonic() + delay)
        self._notify()

    def _notify(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()


class KeyPool:
    """Fair key rotation with independent start rates and concurrency caps.

    One request's path: it queues for the pool, oldest first; ``_take``
    reserves a slot on the next READY key (``KeyCondition``) with one free;
    ``start_request`` waits out the key's pause and its start rate, and each
    redirect hop waits again; then its slot is released (``SlotPhase``). A
    key quarantined meanwhile fails the request as ``no_healthy_api_key``;
    a key paused meanwhile makes it wait.
    """

    def __init__(
        self,
        keys: list[ApiKey],
        *,
        starts_per_second: int,
        concurrency_per_key: int,
        before_start: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        if not keys:
            raise ValueError("at least one API key is required")
        if concurrency_per_key < 1:
            raise ValueError("key concurrency must be positive")
        labels = [key.label for key in keys]
        if any(not label or "\n" in label or "\r" in label for label in labels):
            raise ValueError("API key labels must be safe non-empty text")
        if len(labels) != len(set(labels)):
            raise ValueError("API key labels must be unique")
        self._states = [
            _KeyState(key, _StartLimiter(starts_per_second), asyncio.Lock())
            for key in keys
        ]
        self._cursor = 0
        self._waiters: deque[asyncio.Future[_KeyState]] = deque()
        self._before_start = before_start
        self.starts_per_second = starts_per_second
        self.concurrency_per_key = concurrency_per_key

    async def run(
        self, request: Callable[[ApiKey, StartRequest], Awaitable[T]]
    ) -> T:
        state = await self._acquire()
        slot = _RequestSlot(lambda: self._release(state))
        try:
            await state.wait_out_pause()
            if state.condition(monotonic()) is KeyCondition.QUARANTINED:
                raise ProviderFailure("no_healthy_api_key", retryable=False)

            async def start_request() -> None:
                async with state.start_lock:
                    while True:
                        condition = state.condition(monotonic())
                        if condition is KeyCondition.QUARANTINED:
                            raise ProviderFailure(
                                "no_healthy_api_key", retryable=False
                            )
                        if condition is KeyCondition.PAUSED:
                            await state.wait_out_pause()
                            continue
                        await state.limiter.wait()
                        if state.condition(monotonic()) is not KeyCondition.READY:
                            continue
                        if self._before_start is not None:
                            await self._before_start()
                        if state.condition(monotonic()) is not KeyCondition.READY:
                            continue
                        state.limiter.started()
                        state.starts += 1
                        return

            result = await request(state.key, start_request)
        except BaseException as error:
            slot.end(error)
            raise
        slot.release()
        return result

    async def _acquire(self) -> _KeyState:
        # Requests queue for the pool, not for one key: the oldest waiting
        # request takes the next free slot on any eligible key.
        waiter: asyncio.Future[_KeyState] = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        try:
            self._dispatch()
            return await waiter
        except asyncio.CancelledError:
            if waiter.done() and not waiter.cancelled() and not waiter.exception():
                self._release(waiter.result())
            raise
        finally:
            self._waiters.remove(waiter)

    def _release(self, state: _KeyState) -> None:
        state.active -= 1
        self._dispatch()

    def _dispatch(self) -> None:
        now = monotonic()
        for waiter in self._waiters:
            if waiter.done():
                continue
            if all(
                state.condition(now) is KeyCondition.QUARANTINED
                for state in self._states
            ):
                waiter.set_exception(
                    ProviderFailure("no_healthy_api_key", retryable=False)
                )
                continue
            state = self._take()
            if state is None:
                return
            waiter.set_result(state)

    def _take(self) -> _KeyState | None:
        """Reserve a free slot on the next healthy, unpaused key."""
        now = monotonic()
        count = len(self._states)
        for offset in range(count):
            index = (self._cursor + offset) % count
            state = self._states[index]
            if (
                state.condition(now) is KeyCondition.READY
                and state.active < self.concurrency_per_key
            ):
                self._cursor = (index + 1) % count
                state.active += 1
                return state
        return None

    def quarantine(self, label: str) -> None:
        self._state(label).quarantined = True
        self._dispatch()

    def pause(self, label: str, seconds: float) -> None:
        state = self._state(label)
        state.paused_until = max(state.paused_until, monotonic() + max(1.0, seconds))
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        # A waiting request may take this key's free slot once the pause ends.
        loop.call_later(state.paused_until - monotonic(), self._dispatch)

    def _state(self, label: str) -> _KeyState:
        for state in self._states:
            if state.key.label == label:
                return state
        raise ValueError("unknown API key label")

    def health(self) -> dict[str, int]:
        conditions = [state.condition(monotonic()) for state in self._states]
        return {
            "configured": len(conditions),
            "healthy": sum(c is not KeyCondition.QUARANTINED for c in conditions),
            "paused": conditions.count(KeyCondition.PAUSED),
        }

    def free_ready_slots(self) -> int:
        """Request slots free now on READY keys."""
        now = monotonic()
        return sum(
            self.concurrency_per_key - state.active
            for state in self._states
            if state.condition(now) is KeyCondition.READY
        )

    def key_health(self) -> list[tuple[str, bool, bool, int]]:
        """Return each key's label, health, pause state and request starts.

        A quarantined key still shows a pause it was given, unlike ``health``.
        """
        now = monotonic()
        return [
            (
                state.key.label,
                state.condition(now) is not KeyCondition.QUARANTINED,
                state.paused_until > now,
                state.starts,
            )
            for state in self._states
        ]


@dataclass(frozen=True, slots=True)
class FetchedResponse:
    endpoint: str
    body: bytes
    http_status: int
    request_started_at: datetime
    response_completed_at: datetime
    key_label: str
    headers: dict[str, str]


# Transport failures that do not show whether the relay or the API failed.
_UNCLEAR_TRANSPORT_FAILURES = frozenset(
    {"timeout", "network_failure", "truncated_response", "other_transport_failure"}
)


# The Paris relay serves 96 connections at once (deploy/egress-proxy). The
# collector may hold up to 64: six per key, nine regular keys and the
# interactive one. The rest is for player verification and operator commands.
RELAY_COLLECTOR_CONNECTIONS = 64


def collector_connections(arguments: Any, regular_keys: int) -> int:
    """Connections the collector's keys may open, refused above its relay share."""
    connections = (regular_keys + 1) * arguments.concurrency_per_key
    if arguments.official_proxy_url and connections > RELAY_COLLECTOR_CONNECTIONS:
        raise ValueError("collector keys would open more relay connections than its share")
    return connections


class OfficialApiClient:
    def __init__(
        self,
        origin: str,
        *,
        proxy_url: str = "",
        allow_insecure_test_origin: bool = False,
        max_body_bytes: int = 4 << 20,
        connection_timeout_seconds: float = 5.0,
        response_timeout_seconds: float = 15.0,
        total_timeout_seconds: float = 20.0,
        max_connections: int = 25,
    ) -> None:
        parsed = urlsplit(origin)
        if (
            not parsed.scheme
            or not parsed.netloc
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("official API origin must not contain a path")
        if parsed.scheme != "https" and not (
            allow_insecure_test_origin
            and parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost"}
        ):
            raise ValueError("official API origin must use HTTPS")
        if proxy_url:
            proxy = urlsplit(proxy_url)
            if (
                proxy.scheme not in {"http", "https"}
                or not proxy.hostname
                or proxy.username is not None
                or proxy.password is not None
                or proxy.path not in {"", "/"}
                or proxy.query
                or proxy.fragment
                or proxy.port == 0
            ):
                raise ValueError("official API proxy must be an HTTP(S) origin without credentials")
            if parsed.scheme != "https":
                raise ValueError("proxied official API origin must use HTTPS")
        if max_body_bytes < 1 or max_body_bytes > 4 << 20:
            raise ValueError("raw response limit must be between 1 byte and 4 MiB")
        if any(
            not math.isfinite(value) or value <= 0
            for value in (
                connection_timeout_seconds,
                response_timeout_seconds,
                total_timeout_seconds,
            )
        ):
            raise ValueError("official API timeouts must be positive")
        self.origin = origin.rstrip("/")
        self.max_body_bytes = max_body_bytes
        self._connection_timeout_seconds = connection_timeout_seconds
        self._response_timeout_seconds = response_timeout_seconds
        self._total_timeout_seconds = total_timeout_seconds
        self._executor = ThreadPoolExecutor(
            max_workers=max_connections, thread_name_prefix="official-api"
        )
        self._executor_slots = asyncio.Semaphore(max_connections)
        self.max_connections = max_connections
        self.provider_outage = ProviderOutage()
        self._proxied = bool(proxy_url)
        self._tunnels = _OpenTunnels()
        self._relay_counts = {"requests": 0, "timeouts": 0, "admission_failures": 0}
        self._relay_reachable: int | None = None
        pool_options = {
            "maxsize": max_connections,
            "block": True,
            "cert_reqs": "CERT_REQUIRED",
            "ca_certs": certifi.where(),
            "retries": False,
            "timeout": urllib3.Timeout(
                total=total_timeout_seconds,
                connect=connection_timeout_seconds,
                read=response_timeout_seconds,
            ),
        }
        self._http = (
            _DeadlineProxyManager(proxy_url, tunnels=self._tunnels, **pool_options)
            if proxy_url
            else _DeadlinePoolManager(**pool_options)
        )

    def metric_lines(self) -> list[str]:
        """Requests in flight, and the most connections this client keeps open.

        Through the relay each open connection holds one of its tunnels, idle
        ones kept for reuse included, so those show too.
        """
        in_flight = self.max_connections - self._executor_slots._value
        lines = [
            f"clashlens_collector_api_requests_in_flight {in_flight}",
            f"clashlens_collector_api_connection_limit {self.max_connections}",
        ]
        if not self._proxied:
            return lines
        counts = self._relay_counts
        lines += [
            f"clashlens_collector_relay_tunnels_open {self._open_connections()}",
            f"clashlens_collector_relay_requests_total {counts['requests']}",
            f"clashlens_collector_relay_timeouts_total {counts['timeouts']}",
            (
                "clashlens_collector_relay_admission_failures_total "
                f"{counts['admission_failures']}"
            ),
        ]
        if self._relay_reachable is not None:
            lines.append(f"clashlens_collector_relay_reachable {self._relay_reachable}")
        return lines

    def _open_connections(self) -> int:
        """Set-up tunnels a request is using, or kept open for the next one.

        A tunnel counts once its connection, including the handshake with the
        official API, is done, until it closes; an idle one the relay has
        since closed no longer counts.
        """
        tunnels = self._tunnels.snapshot()
        pools = self._http.pools
        # Iterating the container itself is refused as unsafe across threads.
        for pool_key in pools.keys():  # noqa: SIM118
            idle = getattr(pools.get(pool_key), "pool", None)
            if idle is None:
                continue
            with idle.mutex:
                waiting = list(idle.queue)
            tunnels -= {
                connection
                for connection in waiting
                if connection in tunnels and not connection.is_connected
            }
        return len(tunnels)

    def _count_relay(self, outcome: str) -> None:
        counts = self._relay_counts
        counts["requests"] += 1
        if outcome in ("timeout", "proxy_timeout"):
            counts["timeouts"] += 1
        if outcome in ("proxy_failure", "proxy_timeout"):
            counts["admission_failures"] += 1
            self._relay_reachable = 0
        elif outcome not in _UNCLEAR_TRANSPORT_FAILURES:
            # The relay opened a tunnel and the API answered through it.
            self._relay_reachable = 1

    async def fetch_player(
        self,
        pool: KeyPool,
        normalized_tag: str,
        endpoint: str,
        start_before: datetime | None = None,
    ) -> FetchedResponse:
        if endpoint == "profile":
            suffix = f"/v1/players/{quote(normalized_tag, safe='')}"
        elif endpoint == "battle_log":
            suffix = f"/v1/players/{quote(normalized_tag, safe='')}/battlelog"
        elif endpoint == "league_history":
            suffix = f"/v1/players/{quote(normalized_tag, safe='')}/leaguehistory"
        else:
            raise ValueError("unknown player endpoint")
        return await self._fetch(pool, endpoint, self.origin + suffix, start_before)

    async def fetch_rankings(
        self, pool: KeyPool, location: str = "global"
    ) -> FetchedResponse:
        """One location's top 200 players: ``global`` or a game location ID."""
        return await self._fetch(
            pool,
            "global_player_rankings" if location == "global" else "location_player_rankings",
            self.origin + f"/v1/locations/{quote(location, safe='')}/rankings/players?limit=200",
        )

    async def fetch_locations(self, pool: KeyPool) -> FetchedResponse:
        return await self._fetch(pool, "locations", self.origin + "/v1/locations?limit=1000")

    async def _fetch(
        self,
        pool: KeyPool,
        endpoint: str,
        url: str,
        start_before: datetime | None = None,
    ) -> FetchedResponse:
        outage = self.provider_outage
        probe = False
        # A request that cannot start within the request timeout, waiting for
        # a key, a connection or an outage pause, gives up so its work drains.
        waiting = asyncio.timeout(self._total_timeout_seconds)

        async def start_when_provider_answers(start_request: StartRequest) -> None:
            # Check again after waiting for a start slot: the outage may have
            # begun meanwhile, and a request must not go out during the pause.
            # The recovery probe's own redirect hops follow it without waiting.
            nonlocal probe
            if probe:
                await start_request()
            else:
                while True:
                    probe = await outage.admit()
                    await start_request()
                    if probe or not outage.active:
                        waiting.reschedule(None)
                        break
            if start_before is not None and datetime.now(UTC) >= start_before:
                raise CollectionWindowClosed

        async def request(key: ApiKey, start_request: StartRequest) -> FetchedResponse:
            await self._executor_slots.acquire()
            slot = _RequestSlot(self._executor_slots.release)
            sending = False

            async def start_sending() -> None:
                nonlocal sending
                await start_when_provider_answers(start_request)
                sending = True

            try:
                response = await self._fetch_with_key(
                    pool, endpoint, url, key, start_sending
                )
            except BaseException as error:
                slot.end(error)
                if isinstance(error, _DetachedTimeout):
                    self._count_relay("timeout")
                elif isinstance(error, ProviderFailure) and sending:
                    self._count_relay(error.category)
                raise
            slot.release()
            self._count_relay("answered")
            return response

        try:
            async with waiting:
                response = await pool.run(request)
        except ProviderFailure as error:
            outage.record(failed=True if error.retryable else None, probe=probe)
            raise
        except TimeoutError as error:
            outage.record(failed=None, probe=probe)
            if not waiting.expired():
                raise
            raise ProviderFailure("timeout", retryable=True) from error
        except BaseException:
            outage.record(failed=None, probe=probe)
            raise
        outage.record(failed=response.http_status >= 500, probe=probe)
        return response

    async def _fetch_with_key(
        self,
        pool: KeyPool,
        endpoint: str,
        url: str,
        key: ApiKey,
        start_request: StartRequest,
    ) -> FetchedResponse:
        await start_request()
        started_at = datetime.now(UTC)
        loop = asyncio.get_running_loop()
        timeout_reached: asyncio.Future[None] = loop.create_future()
        deadline = _RequestDeadline(self._total_timeout_seconds, loop, timeout_reached)
        timeout_handle = loop.call_later(
            self._total_timeout_seconds, deadline.expire
        )
        request_task = loop.run_in_executor(
            self._executor,
            self._request,
            url,
            key.value,
            deadline,
            loop,
            start_request,
        )
        cleanup_here = True

        def detach_request() -> asyncio.Future[None]:
            nonlocal cleanup_here
            release_when: asyncio.Future[None] = loop.create_future()

            def request_finished(finished: asyncio.Future[object]) -> None:
                if not finished.cancelled():
                    finished.exception()
                timeout_handle.cancel()
                deadline.finish()
                timeout_reached.cancel()
                release_when.set_result(None)

            request_task.add_done_callback(request_finished)
            cleanup_here = False
            return release_when

        try:
            done, _pending = await asyncio.wait(
                (request_task, timeout_reached),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if request_task not in done and deadline.started:
                raise _DetachedTimeout(detach_request())
            if timeout_reached in done:
                request_task.cancel()
                await asyncio.gather(request_task, return_exceptions=True)
                raise ProviderFailure("timeout", retryable=True)
            status, body, headers = await request_task
        except asyncio.CancelledError:
            if deadline.cancel_before_start():
                request_task.cancel()
            else:
                deadline.expire()
                raise _DetachedCancellation(detach_request())
            raise
        except ProviderFailure as error:
            error.key_label = key.label
            raise
        except (OSError, urllib3.exceptions.HTTPError) as error:
            if isinstance(error, urllib3.exceptions.TimeoutError):
                category = "timeout"
            elif isinstance(error, urllib3.exceptions.ProxyError):
                # The relay refused the connection or could not be reached,
                # in time or at all.
                cause = error.original_error
                timed_out = isinstance(
                    cause, (TimeoutError, urllib3.exceptions.TimeoutError)
                ) and not isinstance(cause, urllib3.exceptions.NewConnectionError)
                category = "proxy_timeout" if timed_out else "proxy_failure"
            elif isinstance(error, urllib3.exceptions.ProtocolError):
                category = "truncated_response"
            elif isinstance(error, OSError):
                category = "network_failure"
            else:
                category = "other_transport_failure"
            raise ProviderFailure(
                category, retryable=True, key_label=key.label
            ) from error
        finally:
            if cleanup_here:
                timeout_handle.cancel()
                deadline.finish()
                timeout_reached.cancel()
        completed_at = datetime.now(UTC)
        if status in {401, 403}:
            pool.quarantine(key.label)
        elif status == 429:
            pool.pause(key.label, retry_after_seconds(headers.get("retry-after")))
        return FetchedResponse(
            endpoint=endpoint,
            body=body,
            http_status=status,
            request_started_at=started_at,
            response_completed_at=completed_at,
            key_label=key.label,
            headers=headers,
        )

    def _request(
        self,
        url: str,
        key_value: str,
        deadline: _RequestDeadline,
        loop: asyncio.AbstractEventLoop,
        start_request: StartRequest,
    ) -> tuple[int, bytes, dict[str, str]]:
        if not deadline.begin():
            raise ProviderFailure("timeout", retryable=True)
        _request_context.deadline = deadline
        try:
            current = url
            for redirect_count in range(MAX_REDIRECTS + 1):
                if redirect_count:
                    redirect_start = asyncio.run_coroutine_threadsafe(
                        start_request(), loop
                    )
                    try:
                        redirect_start.result(timeout=deadline.remaining())
                    except FutureTimeoutError as error:
                        redirect_start.cancel()
                        deadline.expire()
                        raise ProviderFailure("timeout", retryable=True) from error
                    deadline.remaining()
                response = None
                response_complete = False
                try:
                    remaining = deadline.remaining()
                    response = self._http.request(
                        "GET",
                        current,
                        headers={
                            "Authorization": f"Bearer {key_value}",
                            "Accept": "application/json",
                            "User-Agent": "clashlens-python-collector/1",
                        },
                        preload_content=False,
                        redirect=False,
                        timeout=urllib3.Timeout(
                            total=remaining,
                            connect=min(self._connection_timeout_seconds, remaining),
                            read=min(self._response_timeout_seconds, remaining),
                        ),
                    )
                    deadline.remaining()
                    status = int(response.status)
                    headers = {
                        name: value
                        for name in SAFE_RESPONSE_HEADERS
                        if (value := response.headers.get(name)) is not None
                    }
                    if status in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location or redirect_count == MAX_REDIRECTS:
                            raise ProviderFailure("redirect_rejected", retryable=False)
                        target = urljoin(current, location)
                        if _origin(target) != _origin(current):
                            raise ProviderFailure("redirect_rejected", retryable=False)
                        current = target
                        continue
                    length = response.headers.get("Content-Length")
                    if length is not None and int(length) > self.max_body_bytes:
                        raise ProviderFailure("response_too_large", retryable=False)
                    body = response.read(
                        self.max_body_bytes + 1, decode_content=False
                    )
                    deadline.remaining()
                    if len(body) > self.max_body_bytes:
                        raise ProviderFailure("response_too_large", retryable=False)
                    response_complete = True
                    deadline.complete()
                    return status, body, headers
                except ValueError as error:
                    raise ProviderFailure(
                        "invalid_provider_response", retryable=False
                    ) from error
                except (OSError, urllib3.exceptions.HTTPError) as error:
                    if deadline.expired:
                        raise ProviderFailure("timeout", retryable=True) from error
                    raise
                finally:
                    if response is not None:
                        deadline.clear(response.connection)
                        if response_complete:
                            response.release_conn()
                        else:
                            response.close()
                            response.release_conn()
            raise AssertionError("unreachable redirect loop")
        finally:
            del _request_context.deadline


def _origin(value: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(value)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port


def retry_after_seconds(value: str | None) -> float:
    if not value:
        return 5.0
    try:
        return min(300.0, max(1.0, float(value)))
    except ValueError:
        try:
            target = parsedate_to_datetime(value).astimezone(UTC)
        except (TypeError, ValueError):
            return 5.0
        return min(300.0, max(1.0, (target - datetime.now(UTC)).total_seconds()))
