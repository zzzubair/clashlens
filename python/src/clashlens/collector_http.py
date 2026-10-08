from __future__ import annotations

import asyncio
import http.client
import math
import socket
import sys
import threading
from collections import deque
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from time import monotonic
from typing import TypeVar
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


@dataclass(slots=True)
class _KeyState:
    key: ApiKey
    limiter: _StartLimiter
    semaphore: asyncio.Semaphore
    start_lock: asyncio.Lock
    healthy: bool = True
    paused_until: float = 0.0
    starts: int = 0


T = TypeVar("T")
StartRequest = Callable[[], Awaitable[None]]


_request_context = threading.local()


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
        self._connection: HTTPConnection | socket.socket | None = None
        self._active = True
        self._started = False
        self.expired = False

    def begin(self) -> bool:
        with self._lock:
            if self.expired or not self._active:
                return False
            self._started = True
            return True

    @property
    def started(self) -> bool:
        with self._lock:
            return self._started

    def register(self, connection: HTTPConnection | socket.socket) -> None:
        with self._lock:
            if self.expired:
                _close_connection(connection)
            elif self._active:
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
            if self.expired or not self._active:
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
            if self.expired or monotonic() >= self.ends_at:
                self.expired = True
                self._active = False
                connection = self._connection
                self._connection = None
                if connection is not None:
                    _close_connection(connection)
                raise ProviderFailure("timeout", retryable=True)
            self._active = False
            self._connection = None

    def expire(self) -> None:
        with self._lock:
            if not self._active:
                return
            self.expired = True
            self._active = False
            connection = self._connection
            self._connection = None
            if connection is not None:
                _close_connection(connection)
        self._loop.call_soon_threadsafe(self._signal_timeout)

    def cancel_before_start(self) -> bool:
        with self._lock:
            if self._started:
                return False
            self.expired = True
            self._active = False
            return True

    def finish(self) -> None:
        with self._lock:
            self._active = False
            connection = self._connection
            self._connection = None
            if isinstance(connection, socket.socket):
                connection.close()

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


class _DeadlineHTTPSConnection(HTTPSConnection):
    def _new_conn(self) -> socket.socket:
        return _new_deadline_socket(self)

    def connect(self) -> None:
        super().connect()
        _register_request_connection(self)

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


class _DeadlinePoolManager(urllib3.PoolManager):
    def __init__(self, **connection_pool_kw: object) -> None:
        super().__init__(**connection_pool_kw)
        self.pool_classes_by_scheme = {
            "http": _DeadlineHTTPConnectionPool,
            "https": _DeadlineHTTPSConnectionPool,
        }


class _DeadlineProxyManager(urllib3.ProxyManager):
    def __init__(self, proxy_url: str, **connection_pool_kw: object) -> None:
        super().__init__(proxy_url, **connection_pool_kw)
        self.pool_classes_by_scheme = {
            "http": _DeadlineHTTPConnectionPool,
            "https": _DeadlineHTTPSConnectionPool,
        }


class ProviderOutage:
    """One pause for every key after the provider stops answering.

    ``threshold`` transport failures or server errors in a row, with no other
    answer between them, start a pause. Requests wait instead of going out.
    When the pause ends one request goes out as a recovery probe: a server
    error or transport failure doubles the pause up to ``max_delay``; any other
    answer, including a rate limit or a rejected key, ends the outage. Key
    health is untouched, so an outage never disables or pauses a key.
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
        self.failures = 0
        self.delay = 0.0
        self.paused_until = 0.0
        self._probing = False
        self._stopped = False
        self._changed = asyncio.Event()

    @property
    def active(self) -> bool:
        return self.delay > 0

    @property
    def paused(self) -> bool:
        """Whether requests now wait; false once a recovery probe is due."""
        return self.delay > 0 and monotonic() < self.paused_until

    async def admit(self) -> bool:
        """Wait out a pause; return whether this request is the recovery probe."""
        while self.delay:
            if self._stopped:
                raise ProviderFailure("provider_outage", retryable=True)
            remaining = self.paused_until - monotonic()
            if remaining <= 0 and not self._probing:
                self._probing = True
                return True
            changed = self._changed
            try:
                await asyncio.wait_for(
                    changed.wait(), remaining if remaining > 0 else None
                )
            except TimeoutError:
                pass
        return False

    def record(self, *, failed: bool | None, probe: bool) -> None:
        """Record one answer: failed, answered (False), or neither (None)."""
        if probe:
            self._probing = False
        if failed is None:
            if probe:
                self._notify()
            return
        if not failed:
            self.failures = 0
            if self.delay:
                self.delay = 0.0
                self._notify()
            return
        self.failures += 1
        if probe and self.delay:
            self.delay = min(self.max_delay, self.delay * 2)
        elif not self.delay and self.failures >= self.threshold:
            self.delay = self.base_delay
        else:
            return
        self.paused_until = monotonic() + self.delay
        self._notify()

    def stop(self) -> None:
        """Release waiting requests as retryable failures during shutdown."""
        self._stopped = True
        self._notify()

    def _notify(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()


class KeyPool:
    """Fair key rotation with independent start rates and concurrency caps."""

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
            _KeyState(
                key,
                _StartLimiter(starts_per_second),
                asyncio.Semaphore(concurrency_per_key),
                asyncio.Lock(),
            )
            for key in keys
        ]
        self._cursor = 0
        self._selection_lock = asyncio.Lock()
        self._before_start = before_start
        self.starts_per_second = starts_per_second
        self.concurrency_per_key = concurrency_per_key

    async def run(
        self, request: Callable[[ApiKey, StartRequest], Awaitable[T]]
    ) -> T:
        state = await self._select()
        await state.semaphore.acquire()
        release_immediately = True
        try:
            delay = state.paused_until - monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            if not state.healthy:
                raise ProviderFailure("no_healthy_api_key", retryable=False)

            async def start_request() -> None:
                async with state.start_lock:
                    while True:
                        if not state.healthy:
                            raise ProviderFailure(
                                "no_healthy_api_key", retryable=False
                            )
                        delay = state.paused_until - monotonic()
                        if delay > 0:
                            await asyncio.sleep(delay)
                            continue
                        await state.limiter.wait()
                        if not state.healthy or state.paused_until > monotonic():
                            continue
                        if self._before_start is not None:
                            await self._before_start()
                        if not state.healthy or state.paused_until > monotonic():
                            continue
                        state.limiter.started()
                        state.starts += 1
                        return

            return await request(state.key, start_request)
        except (_DetachedTimeout, _DetachedCancellation) as error:
            release_immediately = False
            error.release_when.add_done_callback(
                lambda _finished: state.semaphore.release()
            )
            raise
        finally:
            if release_immediately:
                state.semaphore.release()

    async def _select(self) -> _KeyState:
        while True:
            async with self._selection_lock:
                healthy = [state for state in self._states if state.healthy]
                if not healthy:
                    raise ProviderFailure("no_healthy_api_key", retryable=False)
                now = monotonic()
                ready = [state for state in healthy if state.paused_until <= now]
                if not ready:
                    return min(healthy, key=lambda state: state.paused_until)
                count = len(self._states)
                for offset in range(count):
                    index = (self._cursor + offset) % count
                    state = self._states[index]
                    if state in ready:
                        self._cursor = (index + 1) % count
                        return state

    def quarantine(self, label: str) -> None:
        self._state(label).healthy = False

    def pause(self, label: str, seconds: float) -> None:
        state = self._state(label)
        state.paused_until = max(state.paused_until, monotonic() + max(1.0, seconds))

    def _state(self, label: str) -> _KeyState:
        for state in self._states:
            if state.key.label == label:
                return state
        raise ValueError("unknown API key label")

    def health(self) -> dict[str, int]:
        return {
            "configured": len(self._states),
            "healthy": sum(state.healthy for state in self._states),
            "paused": sum(
                state.healthy and state.paused_until > monotonic()
                for state in self._states
            ),
        }

    def key_health(self) -> list[tuple[str, bool, bool, int]]:
        """Return each key's label, health, pause state and request starts."""
        now = monotonic()
        return [
            (state.key.label, state.healthy, state.paused_until > now, state.starts)
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
            _DeadlineProxyManager(proxy_url, **pool_options)
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
        """Connections a request is using, or kept open for the next one."""
        total = 0
        pools = self._http.pools
        # Iterating the container itself is refused as unsafe across threads.
        for pool_key in pools.keys():  # noqa: SIM118
            idle = getattr(pools.get(pool_key), "pool", None)
            if idle is None:
                continue
            with idle.mutex:
                waiting = list(idle.queue)
            total += idle.maxsize - len(waiting)
            total += sum(
                connection is not None and connection.is_connected
                for connection in waiting
            )
        return total

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

    async def fetch_rankings(self, pool: KeyPool) -> FetchedResponse:
        return await self._fetch(
            pool,
            "global_player_rankings",
            self.origin + "/v1/locations/global/rankings/players?limit=200",
        )

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
            release_immediately = True
            try:
                response = await self._fetch_with_key(
                    pool,
                    endpoint,
                    url,
                    key,
                    lambda: start_when_provider_answers(start_request),
                )
            except (_DetachedTimeout, _DetachedCancellation) as error:
                release_immediately = False
                error.release_when.add_done_callback(
                    lambda _finished: self._executor_slots.release()
                )
                if isinstance(error, _DetachedTimeout):
                    self._count_relay("timeout")
                raise
            except ProviderFailure as error:
                self._count_relay(error.category)
                raise
            finally:
                if release_immediately:
                    self._executor_slots.release()
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
