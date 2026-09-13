from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from time import monotonic
from typing import TypeVar
from urllib.parse import quote, urljoin, urlsplit

import certifi
import urllib3

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


@dataclass(frozen=True, slots=True)
class ApiKey:
    label: str
    value: str = field(repr=False)


class _StartLimiter:
    def __init__(self, starts_per_second: int) -> None:
        if not 1 <= starts_per_second <= 30:
            raise ValueError("request start rate must be between 1 and 30")
        self._interval = 1.0 / starts_per_second
        self._next_start = 0.0
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = monotonic()
            delay = self._next_start - now
            if delay > 0:
                await asyncio.sleep(delay)
                now = monotonic()
            self._next_start = max(now, self._next_start) + self._interval


@dataclass(slots=True)
class _KeyState:
    key: ApiKey
    limiter: _StartLimiter
    semaphore: asyncio.Semaphore
    healthy: bool = True
    paused_until: float = 0.0


T = TypeVar("T")


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
            )
            for key in keys
        ]
        self._cursor = 0
        self._selection_lock = asyncio.Lock()
        self._before_start = before_start

    async def run(self, request: Callable[[ApiKey], Awaitable[T]]) -> T:
        state = await self._select()
        await state.semaphore.acquire()
        try:
            delay = state.paused_until - monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            if not state.healthy:
                raise ProviderFailure("no_healthy_api_key", retryable=False)
            await state.limiter.wait()
            if self._before_start is not None:
                await self._before_start()
            return await request(state.key)
        finally:
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


@dataclass(frozen=True, slots=True)
class FetchedResponse:
    endpoint: str
    body: bytes
    http_status: int
    request_started_at: datetime
    response_completed_at: datetime
    key_label: str
    headers: dict[str, str]


class OfficialApiClient:
    def __init__(
        self,
        origin: str,
        *,
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
        self._http = urllib3.PoolManager(
            maxsize=max_connections,
            block=True,
            cert_reqs="CERT_REQUIRED",
            ca_certs=certifi.where(),
            retries=False,
            timeout=urllib3.Timeout(
                total=total_timeout_seconds,
                connect=connection_timeout_seconds,
                read=response_timeout_seconds,
            ),
        )

    async def fetch_player(
        self, pool: KeyPool, normalized_tag: str, endpoint: str
    ) -> FetchedResponse:
        if endpoint == "profile":
            suffix = f"/v1/players/{quote(normalized_tag, safe='')}"
        elif endpoint == "battle_log":
            suffix = f"/v1/players/{quote(normalized_tag, safe='')}/battlelog"
        else:
            raise ValueError("unknown player endpoint")
        return await self._fetch(pool, endpoint, self.origin + suffix)

    async def fetch_rankings(self, pool: KeyPool) -> FetchedResponse:
        return await self._fetch(
            pool,
            "global_player_rankings",
            self.origin + "/v1/locations/global/rankings/players?limit=200",
        )

    async def _fetch(self, pool: KeyPool, endpoint: str, url: str) -> FetchedResponse:
        async def request(key: ApiKey) -> FetchedResponse:
            started_at = datetime.now(UTC)
            try:
                status, body, headers = await asyncio.to_thread(
                    self._request, url, key.value
                )
            except ProviderFailure as error:
                error.key_label = key.label
                raise
            except (OSError, urllib3.exceptions.HTTPError) as error:
                if isinstance(error, urllib3.exceptions.TimeoutError):
                    category = "timeout"
                elif isinstance(error, urllib3.exceptions.ProtocolError):
                    category = "truncated_response"
                elif isinstance(error, OSError):
                    category = "network_failure"
                else:
                    category = "other_transport_failure"
                raise ProviderFailure(
                    category, retryable=True, key_label=key.label
                ) from error
            completed_at = datetime.now(UTC)
            if status in {401, 403}:
                pool.quarantine(key.label)
            elif status == 429:
                pool.pause(key.label, _retry_after_seconds(headers.get("retry-after")))
            return FetchedResponse(
                endpoint=endpoint,
                body=body,
                http_status=status,
                request_started_at=started_at,
                response_completed_at=completed_at,
                key_label=key.label,
                headers=headers,
            )

        return await pool.run(request)

    def _request(self, url: str, key_value: str) -> tuple[int, bytes, dict[str, str]]:
        current = url
        for redirect_count in range(MAX_REDIRECTS + 1):
            response = None
            try:
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
                )
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
                body = response.read(self.max_body_bytes + 1)
                if len(body) > self.max_body_bytes:
                    raise ProviderFailure("response_too_large", retryable=False)
                return status, body, headers
            except ValueError as error:
                raise ProviderFailure(
                    "invalid_provider_response", retryable=False
                ) from error
            finally:
                if response is not None:
                    response.release_conn()
        raise AssertionError("unreachable redirect loop")


def _origin(value: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(value)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port


def _retry_after_seconds(value: str | None) -> float:
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
