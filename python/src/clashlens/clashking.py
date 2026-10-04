"""Past Legend Season finishes from ClashKing's public API.

ClashKing agreed on 2026-10-04 that Clash Lens may show a player's past Legend
Season finishes from its public API, credited with a link. Nothing else from
ClashKing is used. A player's history is fetched only when someone views
their page, at most once a day per player and at most two requests a second
in total. The rows are third-party reports kept in their own table; they never
feed our daily logs or totals.
"""

from __future__ import annotations

import json
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import certifi

from .domain import SEASON_DURATION, DomainRuleError, validate_legend_season_start
from .verification import _NoRedirectHandler

CLASHKING_ORIGIN = "https://api.clashk.ing"
REFRESH_AFTER = timedelta(days=1)
# Also stops a second view fetching while the first one is still waiting.
RETRY_AFTER = timedelta(hours=1)
REQUEST_TIMEOUT_SECONDS = 2.5
MAX_RESPONSE_BYTES = 512 * 1024
MIN_REQUEST_GAP_SECONDS = 0.5
# Stops a slow ClashKing from holding more than two of the website's threads.
MAX_CONCURRENT_REQUESTS = 2
FIRST_BACKOFF_SECONDS = 60.0
MAX_BACKOFF_SECONDS = 3600.0

# ClashKing labels rows three ways. Checked 2026-10-04 against our official
# league history: its `v2-2026-08-03T05:00:00Z` row (5,856 trophies, rank 1)
# is the Season that started 2026-08-10, and its dated `2026-05-18` row repeats
# `v2-2026-05-11T05:00:00Z`. So a dated label is a Season start and a v2 label
# is a week before one. `YYYY-MM` rows are the calendar-month Legend seasons
# from before 28-day Seasons.
_V2_LABEL = re.compile(r"v2-(\d{4}-\d{2}-\d{2})T05:00:00Z")
_DATED_LABEL = re.compile(r"\d{4}-\d{2}-\d{2}")
_MONTH_LABEL = re.compile(r"(\d{4})-(\d{2})")
_V2_OFFSET = timedelta(days=7)
_LEGEND_TIER_IDS = frozenset({105000034, 105000035, 105000036})
# ClashKing repeats the last calendar-month result as the first 28-day Season.
_LAST_MONTH_SEASON = "2025-09"
_FIRST_SEASON_ID = str(int(datetime(2025, 10, 6, 5, tzinfo=UTC).timestamp()))


class ClashKingUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SeasonFinish:
    season_id: str
    season_start: datetime | None
    source_season: str
    trophies: int
    global_rank: int | None

    @property
    def season_end(self) -> datetime | None:
        if self.season_start is None:
            return None
        return self.season_start + SEASON_DURATION


def parse_season_finishes(payload: bytes, *, now: datetime) -> list[SeasonFinish]:
    """Finished Legend seasons from one legend-history response, newest first.

    Rows outside Legend, off our 28-day Season phase or not yet finished are
    left out. When two rows describe the same Season, the v2 row wins: its rank
    matches official results, and the dated copy's win counts can exceed what
    28 days allow. A first 28-day Season (2025-10-06) row repeating the
    2025-09 row's trophies and rank is a copy of that month and is left out.
    """
    try:
        document = json.loads(payload)
    except (UnicodeDecodeError, ValueError) as error:
        raise ClashKingUnavailable("malformed legend history") from error
    items = document.get("items") if isinstance(document, dict) else None
    if not isinstance(items, list):
        raise ClashKingUnavailable("malformed legend history")
    chosen: dict[str, tuple[bool, SeasonFinish]] = {}
    for item in items:
        try:
            mapped = _map_row(item, now=now)
        except (OverflowError, ValueError):
            mapped = None  # a date outside what Python can hold
        if mapped is None:
            continue
        from_v2, finish = mapped
        existing = chosen.get(finish.season_id)
        if existing is None or (from_v2 and not existing[0]):
            chosen[finish.season_id] = (from_v2, finish)
    last_month = chosen.get(_LAST_MONTH_SEASON)
    first_season = chosen.get(_FIRST_SEASON_ID)
    if (
        last_month is not None
        and first_season is not None
        and (last_month[1].trophies, last_month[1].global_rank)
        == (first_season[1].trophies, first_season[1].global_rank)
    ):
        del chosen[_FIRST_SEASON_ID]
    return sorted(
        (finish for _, finish in chosen.values()), key=_sort_key, reverse=True
    )


def _map_row(item: Any, *, now: datetime) -> tuple[bool, SeasonFinish] | None:
    if not isinstance(item, dict):
        return None
    label = item.get("season")
    trophies = item.get("trophies")
    rank = item.get("rank")
    tier = item.get("leagueTier")
    if not isinstance(label, str) or not _is_int(trophies) or trophies < 0:
        return None
    if (
        isinstance(tier, dict)
        and tier.get("id") not in _LEGEND_TIER_IDS
        and not str(tier.get("name", "")).startswith("Legend League")
    ):
        return None
    global_rank = rank if _is_int(rank) and rank >= 1 else None
    if match := _MONTH_LABEL.fullmatch(label):
        year, month = int(match[1]), int(match[2])
        if not 1 <= month <= 12 or datetime(year, month, 1, tzinfo=UTC) > now:
            return None
        return False, SeasonFinish(label, None, label, trophies, global_rank)
    v2 = _V2_LABEL.fullmatch(label)
    if v2 is None and not _DATED_LABEL.fullmatch(label):
        return None
    start = _reset_on(label if v2 is None else v2[1])
    if start is not None and v2 is not None:
        start += _V2_OFFSET
    if start is None or start + SEASON_DURATION > now:
        return None
    season_id = str(int(start.timestamp()))
    try:
        validate_legend_season_start(season_id, observed_at=now)
    except DomainRuleError:
        return None
    return v2 is not None, SeasonFinish(season_id, start, label, trophies, global_rank)


def _reset_on(day: str) -> datetime | None:
    try:
        return datetime.strptime(day, "%Y-%m-%d").replace(hour=5, tzinfo=UTC)
    except ValueError:
        return None


def _is_int(value: Any) -> bool:
    # Also fits the database's integer columns.
    return isinstance(value, int) and not isinstance(value, bool) and value < 2**31


def _sort_key(finish: SeasonFinish) -> datetime:
    if finish.season_start is not None:
        return finish.season_start
    year, month = finish.season_id.split("-")
    return datetime(int(year), int(month), 1, tzinfo=UTC)


Transport = Callable[[str, float], tuple[int, dict[str, str], bytes]]


def _urllib_transport(url: str, timeout: float) -> tuple[int, dict[str, str], bytes]:
    context = ssl.create_default_context(cafile=certifi.where())
    # A redirect comes back as a failed fetch instead of leaving api.clashk.ing.
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=context), _NoRedirectHandler()
    )
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "ClashLens"}
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            return (
                response.status,
                dict(response.headers),
                response.read(MAX_RESPONSE_BYTES + 1),
            )
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers or {}), b""


class ClashKingClient:
    """Shared request pacing for every ClashKing call this process makes.

    Requests are spaced at least half a second apart and at most two run at
    once. A 429 or 5xx answer, or no answer, pauses every request: one minute,
    doubling up to an hour, or longer when ClashKing asks for it. Calls never wait for a slot; a view
    that cannot have one shows what is already saved.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        transport: Transport = _urllib_transport,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # Stacks running against a fake Clash API never call the real ClashKing.
        self._enabled = enabled
        self._transport = transport
        self._clock = clock
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)
        self._next_request_at = 0.0
        self._backoff = 0.0

    def try_acquire(self) -> bool:
        """Take a request slot without waiting; every True needs a release()."""
        if not self._slots.acquire(blocking=False):
            return False
        with self._lock:
            now = self._clock()
            if self._enabled and now >= self._next_request_at:
                self._next_request_at = now + MIN_REQUEST_GAP_SECONDS
                return True
        self._slots.release()
        return False

    def release(self) -> None:
        self._slots.release()

    def fetch_legend_history(self, normalized_tag: str) -> bytes:
        url = f"{CLASHKING_ORIGIN}/v2/player/{quote(normalized_tag, safe='')}/legend-history"
        try:
            status, headers, body = self._transport(url, REQUEST_TIMEOUT_SECONDS)
        except OSError as error:
            self._pause(None)
            raise ClashKingUnavailable("no answer") from error
        if status == 429 or status >= 500:
            self._pause(headers.get("Retry-After") or headers.get("retry-after"))
            raise ClashKingUnavailable(f"HTTP {status}")
        with self._lock:
            self._backoff = 0.0
        if status == 404:
            return b'{"items":[]}'
        if status != 200 or len(body) > MAX_RESPONSE_BYTES:
            raise ClashKingUnavailable(f"HTTP {status}")
        return body

    def _pause(self, retry_after: str | None) -> None:
        with self._lock:
            self._backoff = min(
                MAX_BACKOFF_SECONDS, max(FIRST_BACKOFF_SECONDS, self._backoff * 2)
            )
            pause = self._backoff
            if retry_after is not None and retry_after.isdigit():
                pause = max(pause, min(MAX_BACKOFF_SECONDS, float(retry_after)))
            self._next_request_at = max(self._next_request_at, self._clock() + pause)


def get_past_seasons(
    database: Any, client: ClashKingClient, normalized_tag: str, *, now: datetime
) -> dict[str, Any] | None:
    """A tracked player's saved ClashKing finishes, refreshed first when due.

    None means we do not know the player, so nothing is fetched for them. The
    database connection is returned before any request to ClashKing.
    """
    with database.pool.connection() as connection:
        row = connection.execute(
            """
            SELECT player.id, history.attempted_at, history.fetched_at
            FROM players AS player
            LEFT JOIN clashking_history_fetches AS history
                ON history.player_id = player.id
            WHERE player.normalized_tag = %s
            """,
            (normalized_tag,),
        ).fetchone()
    if row is None:
        return None
    player_id, attempted_at, fetched_at = row
    due = (fetched_at is None or fetched_at <= now - REFRESH_AFTER) and (
        attempted_at is None or attempted_at <= now - RETRY_AFTER
    )
    # The slot comes before the claim, so a refused view writes nothing.
    if due and client.try_acquire():
        try:
            if _claim(database, player_id, now):
                payload = client.fetch_legend_history(normalized_tag)
                finishes = parse_season_finishes(payload, now=now)
                _store(database, player_id, finishes, now)
        except ClashKingUnavailable:
            pass
        finally:
            client.release()
    return _saved(database, player_id, normalized_tag)


def _claim(database: Any, player_id: int, now: datetime) -> bool:
    with database.pool.connection() as connection:
        claimed = connection.execute(
            """
            INSERT INTO clashking_history_fetches AS history (player_id, attempted_at)
            VALUES (%(player)s, %(now)s)
            ON CONFLICT (player_id) DO UPDATE SET attempted_at = EXCLUDED.attempted_at
            WHERE (history.attempted_at IS NULL OR history.attempted_at <= %(retry_before)s)
              AND (history.fetched_at IS NULL OR history.fetched_at <= %(stale_before)s)
            RETURNING player_id
            """,
            {
                "player": player_id,
                "now": now,
                "retry_before": now - RETRY_AFTER,
                "stale_before": now - REFRESH_AFTER,
            },
        ).fetchone()
    return claimed is not None


def _store(
    database: Any, player_id: int, finishes: list[SeasonFinish], now: datetime
) -> None:
    with database.pool.connection() as connection:
        with connection.transaction():
            connection.execute(
                "DELETE FROM clashking_season_finishes WHERE player_id = %s",
                (player_id,),
            )
            with connection.cursor() as cursor:
                cursor.executemany(
                    """
                    INSERT INTO clashking_season_finishes (
                        player_id, season_id, season_start, season_end,
                        source_season, trophies, global_rank
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            player_id,
                            finish.season_id,
                            finish.season_start,
                            finish.season_end,
                            finish.source_season,
                            finish.trophies,
                            finish.global_rank,
                        )
                        for finish in finishes
                    ],
                )
            connection.execute(
                "UPDATE clashking_history_fetches SET fetched_at = %s WHERE player_id = %s",
                (now, player_id),
            )


def _saved(database: Any, player_id: int, normalized_tag: str) -> dict[str, Any]:
    with database.pool.connection() as connection:
        fetched = connection.execute(
            "SELECT fetched_at FROM clashking_history_fetches WHERE player_id = %s",
            (player_id,),
        ).fetchone()
        rows = connection.execute(
            """
            SELECT season_id, season_start, trophies, global_rank
            FROM clashking_season_finishes
            WHERE player_id = %s
            """,
            (player_id,),
        ).fetchall()
    finishes = sorted(
        (SeasonFinish(row[0], row[1], "", row[2], row[3]) for row in rows),
        key=_sort_key,
        reverse=True,
    )
    return {
        "tag": normalized_tag,
        "source": "clashking",
        "fetched_at": None if fetched is None else fetched[0],
        "seasons": [
            {
                "season_id": finish.season_id,
                "season_start": finish.season_start,
                "season_end": finish.season_end,
                "trophies": finish.trophies,
                "global_rank": finish.global_rank,
            }
            for finish in finishes
        ],
    }
