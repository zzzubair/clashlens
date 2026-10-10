from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any

from .analytics import tie_order_sql
from .api_db import (
    ApiDatabase,
    _public_confidence,
    _public_snapshot_confidence,
    _season_reset_waiting_sql,
    _text,
)
from .domain import ranked_day_for, season_opening_reset

_LIVE_FRESHNESS_SECONDS = 600
# Rows kept beside a selected player when their page edge would hide them.
_FOCUS_NEIGHBORS = 5


# Shared membership and confirmation rule for the page and operator measurements.
# A profile still naming an earlier Season than the calendar shows trophies from
# before that player's Season reset, so it waits off the board until it updates.
# On a Season's first Legend day, trophies other than 5,000 that still equal
# the player's frozen pre-Reset final trophies, or that the day's recorded
# battles cannot explain, wait the same way.
_OPENING_DAY_WAITING_SQL = _season_reset_waiting_sql(
    "player.id", "profile.trophies", "%(opening_reset)s"
)
_LIVE_CANDIDATES_SQL = f"""
SELECT player.normalized_tag, profile.name, profile.trophies,
       greatest(
           player.current_observed_at, player.current_profile_confirmed_at
       ) AS observed_at,
       player.eligibility_state,
       profile.profile_json -> 'clan' ->> 'name' AS clan,
       COALESCE(profile.current_league_season_id = %(season_id)s, false)
           AND NOT {_OPENING_DAY_WAITING_SQL} AS season_current,
       tally.attacks, tally.destruction
FROM players AS player
JOIN LATERAL (
    SELECT name, trophies, profile_json, source_contract_state,
           current_league_season_id
    FROM player_profile_versions
    WHERE id = player.current_profile_version_id
    -- Keep one indexed current-profile lookup per player as history grows.
    OFFSET 0
) AS profile ON true
LEFT JOIN live_attack_tallies AS tally
  ON tally.player_id = player.id AND tally.official_season_id = %(season_id)s
WHERE player.active = true
  AND profile.source_contract_state = 'accepted'
  AND NOT EXISTS (
      SELECT 1 FROM collector_response_state AS checked
      WHERE checked.scope = 'player'
        AND checked.identity_key = player.normalized_tag
        AND checked.endpoint = 'profile'
        AND checked.last_not_found_at IS NOT NULL
        AND (checked.last_success_at IS NULL
             OR checked.last_not_found_at > checked.last_success_at)
  )
"""
_LIVE_PLAYERS_SQL = f"""
SELECT * FROM ({_LIVE_CANDIDATES_SQL}) AS candidate WHERE season_current
"""

# Equal trophies in the Daily board's order (analytics.tie_order_key), from
# the counts the worker last saved (analytics.refresh_live_attack_tallies).
# v1 ordered them by MD5 of the tag alone.
_LIVE_ORDER_VERSION = "tracked-trophies-attack-destruction-v2"
_LIVE_ORDER_SQL = (
    f"trophies DESC, {tie_order_sql('attacks', 'destruction', 'normalized_tag')}"
)
_LIVE_RANKED_SQL = f"""
SELECT *, row_number() OVER (ORDER BY {_LIVE_ORDER_SQL}) AS position
FROM ({_LIVE_PLAYERS_SQL}) AS live
"""


def _season_params(now: datetime) -> dict[str, Any]:
    return {
        "season_id": ranked_day_for(now).official_season_id,
        "opening_reset": season_opening_reset(now),
    }


def search_live_leaderboard(
    database: ApiDatabase,
    query: str,
    *,
    now: datetime,
    cache: LiveBoardCache | None = None,
) -> dict[str, Any]:
    """Filter the ranked board the focused Live Leaderboard page reads."""
    query = query.strip()
    if not query or len(query) > 80:
        raise ValueError("invalid leaderboard search")
    tag = "#" + query.removeprefix("#").upper()
    ranked = list(enumerate(_live_board(database, now, cache).rows, start=1))
    exact = [
        (rank, row) for rank, row in ranked if query.startswith("#") and row[0] == tag
    ]
    needle = query.lower()
    found = exact or [
        (rank, row)
        for rank, row in ranked
        if row[0] == tag or needle in _text(row[1]).lower()
    ]
    return {
        "exact_tag": tag if exact else None,
        "has_more": len(found) > 20,
        "results": [
            {
                "tag": _text(row[0]),
                "name": _text(row[1]),
                "trophies": int(row[2]),
                "rank": rank,
            }
            for rank, row in found[:20]
        ],
    }


def live_positions(connection: Any, tags: list[str], *, now: datetime) -> dict[str, int]:
    """Each tag's Live Leaderboard position; a tag off the board is left out."""
    if not tags:
        return {}
    rows = connection.execute(
        f"""
        SELECT normalized_tag, position FROM ({_LIVE_RANKED_SQL}) AS ranked
        WHERE normalized_tag = ANY(%(tags)s)
        """,
        {"tags": tags, **_season_params(now)},
    ).fetchall()
    return {_text(row[0]): int(row[1]) for row in rows}


def live_freshness_metrics(database: ApiDatabase, *, now: datetime) -> dict[str, Any]:
    """Measure the whole Live Leaderboard, without sorting or fetching a page."""
    with database.pool.connection() as connection:
        row = connection.execute(
            f"""
            WITH selected AS ({_LIVE_PLAYERS_SQL}), ages AS (
                SELECT CASE WHEN observed_at IS NOT NULL
                            THEN greatest(0, extract(epoch FROM %(now)s - observed_at))
                       END AS age
                FROM selected
            )
            SELECT count(*), count(*) - count(age),
                   percentile_disc(0.5) WITHIN GROUP (ORDER BY age),
                   percentile_disc(0.95) WITHIN GROUP (ORDER BY age), max(age),
                   count(*) FILTER (WHERE age > %(fresh)s)
            FROM ages
            """,
            {
                "now": now,
                "fresh": _LIVE_FRESHNESS_SECONDS,
                **_season_params(now),
            },
        ).fetchone()
    return {
        "sample_timestamp_seconds": now.timestamp(),
        "entries": int(row[0]),
        "age_missing_entries": int(row[1]),
        "age_p50_seconds": None if row[2] is None else float(row[2]),
        "age_p95_seconds": None if row[3] is None else float(row[3]),
        "age_max_seconds": None if row[4] is None else float(row[4]),
        "older_than_10_minutes": int(row[5]),
    }


@dataclass(frozen=True)
class _LiveBoard:
    season: tuple[Any, ...]
    tracked_population: int
    season_reset_pending: int
    # (tag, name, trophies, observed_at, eligibility_state, clan) in rank order.
    rows: list[tuple[Any, ...]]


def _read_live_board(connection: Any, now: datetime) -> _LiveBoard:
    season = _season_params(now)
    rows = connection.execute(
        f"""
        WITH candidates AS MATERIALIZED (
            {_LIVE_CANDIDATES_SQL}
        ), totals AS (
            SELECT count(*)::bigint AS tracked_population,
                   (SELECT count(*) FROM candidates WHERE NOT season_current)
                       AS season_reset_pending
            FROM players WHERE active
        )
        SELECT totals.tracked_population, totals.season_reset_pending,
               live.normalized_tag, live.name, live.trophies, live.observed_at,
               live.eligibility_state, live.clan
        FROM totals LEFT JOIN (
            SELECT * FROM candidates WHERE season_current
        ) AS live ON true
        ORDER BY {_LIVE_ORDER_SQL}
        """,
        season,
    ).fetchall()
    return _LiveBoard(
        season=tuple(season.values()),
        tracked_population=int(rows[0][0]),
        season_reset_pending=int(rows[0][1]),
        rows=[tuple(row[2:]) for row in rows if row[2] is not None],
    )


class LiveBoardCache:
    """The ranked Live Leaderboard, read at most once per ``seconds`` and
    shared by every page of it, the home page's top players included."""

    def __init__(
        self, seconds: float = 30, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._seconds = seconds
        self._clock = clock
        self._lock = Lock()
        self._board: _LiveBoard | None = None
        self._refresh_after = 0.0

    def current(self, database: ApiDatabase, now: datetime) -> _LiveBoard:
        with self._lock:
            board = self._board
            if (
                board is None
                or self._clock() >= self._refresh_after
                or board.season != tuple(_season_params(now).values())
            ):
                with database.pool.connection() as connection:
                    board = _read_live_board(connection, now)
                self._board = board
                self._refresh_after = self._clock() + self._seconds
            return board


def _live_board(
    database: ApiDatabase, now: datetime, cache: LiveBoardCache | None
) -> _LiveBoard:
    if cache is not None:
        return cache.current(database, now)
    with database.pool.connection() as connection:
        return _read_live_board(connection, now)


def get_live_leaderboard(
    database: ApiDatabase,
    *,
    limit: int,
    offset: int = 0,
    now: datetime,
    focus_tag: str | None = None,
    cache: LiveBoardCache | None = None,
) -> dict[str, Any] | None:
    if offset < 0 or offset % limit:
        raise ValueError("offset must be non-negative and aligned to limit")
    board = _live_board(database, now, cache)
    rows = board.rows
    total_entries = len(rows)
    first, last = offset + 1, offset + limit
    if focus_tag is not None:
        focus_position = next(
            (index + 1 for index, row in enumerate(rows) if row[0] == focus_tag),
            None,
        )
        if focus_position is None:
            return None
        offset = (focus_position - 1) // limit * limit
        first = min(offset + 1, focus_position - _FOCUS_NEIGHBORS)
        last = max(offset + limit, focus_position + _FOCUS_NEIGHBORS)
    if offset and offset >= total_entries:
        return None
    observed = [row[3] for row in rows if row[3] is not None]
    stale_before = now - timedelta(seconds=_LIVE_FRESHNESS_SECONDS)
    stale_count = sum(1 for value in observed if value < stale_before)
    oldest_observed_at = min(observed, default=None)
    newest_observed_at = max(observed, default=None)
    entries = []
    first = max(first, 1)
    for position, row in enumerate(rows[first - 1 : last], start=first):
        observed_at = row[3].astimezone(UTC)
        age = max(0.0, (now.astimezone(UTC) - observed_at).total_seconds())
        age_seconds = int(age)
        freshness = "fresh" if age <= _LIVE_FRESHNESS_SECONDS else "stale"
        entries.append(
            {
                "position": position,
                "tag": _text(row[0]),
                "name": _text(row[1]),
                "trophies": int(row[2]),
                "observed_at": observed_at.isoformat(),
                "age_seconds": age_seconds,
                "freshness": freshness,
                "confidence": _text(row[4]),
                "public_confidence": _public_confidence(True, _text(row[4])),
                "clan": None if row[5] is None else _text(row[5]),
                "official_rank": None,
            }
        )
    page_count = (total_entries + limit - 1) // limit
    page = offset // limit + 1
    return {
        "kind": "live",
        "ordering_rule_version": _LIVE_ORDER_VERSION,
        "generated_at": now.astimezone(UTC).isoformat(),
        "tracked_population": board.tracked_population,
        "total_entries": total_entries,
        # Tracked players left off until a profile names this Season.
        "season_reset_pending": board.season_reset_pending,
        "page": page,
        "page_size": limit,
        "page_count": page_count,
        "has_previous": page > 1,
        "has_next": page < page_count,
        "coverage": {
            "state": "partial",
            "tracked_players": board.tracked_population,
            "measured_percent": (
                100.0 * total_entries / board.tracked_population
                if board.tracked_population
                else 0.0
            ),
            "note": "Tracked-player publication; complete Legend I coverage is not claimed.",
        },
        "provenance": {
            "source": "current accepted player profiles",
            "observed_at": (
                None
                if newest_observed_at is None
                else newest_observed_at.astimezone(UTC).isoformat()
            ),
            "freshness": "stale" if stale_count else "fresh",
            "confidence": "partial",
            "coverage": "partial",
            "version": _LIVE_ORDER_VERSION,
        },
        "source_observations": {
            "oldest_observed_at": (
                None
                if oldest_observed_at is None
                else oldest_observed_at.astimezone(UTC).isoformat()
            ),
            "newest_observed_at": (
                None
                if newest_observed_at is None
                else newest_observed_at.astimezone(UTC).isoformat()
            ),
            "stale_count": stale_count,
        },
        "quality_states": ["partial"] + (["stale"] if stale_count else []),
        "entries": entries,
    }


def get_frozen_leaderboard(
    database: ApiDatabase,
    *,
    limit: int,
    offset: int = 0,
    official_season_id: str | None = None,
    season_day_number: int | None = None,
    now: datetime | None = None,
    freshness_seconds: int = 900,
) -> dict[str, Any] | None:
    if offset < 0 or offset % limit:
        raise ValueError("offset must be non-negative and aligned to limit")
    if (official_season_id is None) != (season_day_number is None):
        raise ValueError("season and day must be supplied together")
    now = datetime.now(UTC) if now is None else now
    with database.pool.connection() as connection:
        coordinator_exists, snapshots_exist = connection.execute(
            """
            SELECT to_regclass('boundary_publication_generations') IS NOT NULL
                       AND to_regclass('boundary_publication_manifest_rows') IS NOT NULL,
                   to_regclass('leaderboard_snapshots') IS NOT NULL
            """
        ).fetchone()
        legacy_publications = """
            SELECT 'legacy'::text AS source, leaderboard.id,
                   leaderboard.boundary_at, leaderboard.version, 0 AS generation,
                   selector.official_season_id, selector.season_day_number
            FROM api_frozen_leaderboards AS leaderboard
            JOIN LATERAL (
                SELECT day.official_season_id, day.season_day_number
                FROM ranked_day_versions AS day
                WHERE day.ranked_day_end = leaderboard.boundary_at
                ORDER BY day.id DESC LIMIT 1
            ) AS selector ON true
        """
        if coordinator_exists:
            v2_publications = """
                SELECT 'v2'::text AS source, snapshot.id, snapshot.boundary_at,
                       snapshot.version, COALESCE(generation.generation, 0) AS generation,
                       COALESCE(generation_day.official_season_id, source_day.official_season_id) AS official_season_id,
                       COALESCE(generation_day.season_day_number, source_day.season_day_number) AS season_day_number
                FROM leaderboard_snapshots AS snapshot
                LEFT JOIN ranked_day_versions AS source_day
                  ON source_day.id = snapshot.source_ranked_day_version_id
                LEFT JOIN boundary_publication_generations AS generation
                  ON generation.snapshot_id = snapshot.id
                LEFT JOIN LATERAL (
                    -- A proof frozen since 0081 records its newest ranked day;
                    -- an older one stores all its rows, so they give it.
                    SELECT ranked.official_season_id, ranked.season_day_number
                    FROM boundary_publication_manifests AS manifest
                    JOIN ranked_day_versions AS ranked
                      ON ranked.id = COALESCE(
                          manifest.newest_ranked_day_version_id,
                          (SELECT member.ranked_day_version_id
                           FROM boundary_publication_manifest_rows AS member
                           WHERE member.manifest_id = manifest.id
                             AND member.ranked_day_version_id IS NOT NULL
                           ORDER BY member.ranked_day_version_id DESC LIMIT 1))
                    WHERE manifest.id = generation.snapshot_manifest_id
                ) AS generation_day ON true
                WHERE snapshot.snapshot_kind = 'frozen' AND snapshot.state = 'published'
                  AND (generation.id IS NULL OR generation.snapshot_state <> 'superseded')
            """
        elif snapshots_exist:
            v2_publications = """
                SELECT 'v2'::text AS source, snapshot.id, snapshot.boundary_at,
                       snapshot.version, 0 AS generation, source_day.official_season_id,
                       source_day.season_day_number
                FROM leaderboard_snapshots AS snapshot
                LEFT JOIN ranked_day_versions AS source_day
                  ON source_day.id = snapshot.source_ranked_day_version_id
                WHERE snapshot.snapshot_kind = 'frozen' AND snapshot.state = 'published'
            """
        else:
            v2_publications = ""
        publications = (
            v2_publications + " UNION ALL " + legacy_publications
            if v2_publications
            else legacy_publications
        )
        locator = connection.execute(
            f"""
            WITH publications AS ({publications}), selected AS (
                SELECT * FROM publications
                WHERE (%s::text IS NULL OR
                       (official_season_id = %s AND season_day_number = %s))
                ORDER BY boundary_at DESC, (source = 'v2') DESC,
                         version DESC, generation DESC, id DESC LIMIT 1
            )
            SELECT selected.source, selected.id,
                   (SELECT json_build_object(
                               'official_season_id', p.official_season_id,
                               'season_day_number', p.season_day_number)
                    FROM publications p WHERE p.boundary_at < selected.boundary_at
                    ORDER BY p.boundary_at DESC, (p.source = 'v2') DESC,
                             p.version DESC, p.generation DESC, p.id DESC LIMIT 1),
                   (SELECT json_build_object(
                               'official_season_id', p.official_season_id,
                               'season_day_number', p.season_day_number)
                    FROM publications p WHERE p.boundary_at > selected.boundary_at
                    ORDER BY p.boundary_at, (p.source = 'v2') DESC,
                             p.version DESC, p.generation DESC, p.id DESC LIMIT 1)
            FROM selected
            """,
            (official_season_id, official_season_id, season_day_number),
        ).fetchone()
        if locator is None:
            return None
        snapshot = None
        if _text(locator[0]) == "v2":
            if coordinator_exists:
                snapshot_query = """
                    SELECT snapshot.id, snapshot.boundary_at, snapshot.version,
                           snapshot.ordering_rule_version,
                           snapshot.freshness_rule_version,
                           snapshot.measured_coverage,
                           snapshot.eligible_population_count,
                           snapshot.included_entry_count, snapshot.stale_entry_count,
                           snapshot.fresh_entry_count, snapshot.excluded_missing_count,
                           snapshot.excluded_invalid_count,
                           snapshot.excluded_malformed_count,
                           snapshot.excluded_conflicting_count,
                           COALESCE(generation_day.official_season_id, source_day.official_season_id) AS official_season_id,
                           COALESCE(generation_day.season_day_number, source_day.season_day_number) AS season_day_number,
                           (SELECT max(entry.profile_observed_at)
                            FROM leaderboard_snapshot_entries AS entry
                            WHERE entry.snapshot_id = snapshot.id)
                    FROM leaderboard_snapshots AS snapshot
                    LEFT JOIN ranked_day_versions AS source_day
                      ON source_day.id = snapshot.source_ranked_day_version_id
                    LEFT JOIN boundary_publication_generations AS generation
                      ON generation.snapshot_id = snapshot.id
                    LEFT JOIN LATERAL (
                        -- A proof frozen since 0081 records its newest ranked day;
                        -- an older one stores all its rows, so they give it.
                        SELECT ranked.official_season_id, ranked.season_day_number
                        FROM boundary_publication_manifests AS manifest
                        JOIN ranked_day_versions AS ranked
                          ON ranked.id = COALESCE(
                              manifest.newest_ranked_day_version_id,
                              (SELECT member.ranked_day_version_id
                               FROM boundary_publication_manifest_rows AS member
                               WHERE member.manifest_id = manifest.id
                                 AND member.ranked_day_version_id IS NOT NULL
                               ORDER BY member.ranked_day_version_id DESC LIMIT 1))
                        WHERE manifest.id = generation.snapshot_manifest_id
                    ) AS generation_day ON true
                    WHERE snapshot.id = %s
                      AND (generation.id IS NULL OR generation.snapshot_state <> 'superseded')
                    ORDER BY generation.generation DESC NULLS LAST
                    LIMIT 1
                """
            else:
                snapshot_query = """
                    SELECT snapshot.id, snapshot.boundary_at, snapshot.version,
                           snapshot.ordering_rule_version,
                           snapshot.freshness_rule_version,
                           snapshot.measured_coverage,
                           snapshot.eligible_population_count,
                           snapshot.included_entry_count, snapshot.stale_entry_count,
                           snapshot.fresh_entry_count, snapshot.excluded_missing_count,
                           snapshot.excluded_invalid_count,
                           snapshot.excluded_malformed_count,
                           snapshot.excluded_conflicting_count,
                           source_day.official_season_id,
                           source_day.season_day_number,
                           (SELECT max(entry.profile_observed_at)
                            FROM leaderboard_snapshot_entries AS entry
                            WHERE entry.snapshot_id = snapshot.id)
                    FROM leaderboard_snapshots AS snapshot
                    LEFT JOIN ranked_day_versions AS source_day
                      ON source_day.id = snapshot.source_ranked_day_version_id
                    WHERE snapshot.id = %s
                """
            snapshot = connection.execute(snapshot_query, (locator[1],)).fetchone()
            assert snapshot is not None
            snapshot = (*snapshot, locator[2], locator[3])
        if snapshot is None:
            legacy_row = connection.execute(
                """
                SELECT leaderboard.id, leaderboard.public_id,
                       leaderboard.boundary_at, leaderboard.version,
                       leaderboard.ordering_rule_version, leaderboard.coverage,
                       selector.official_season_id, selector.season_day_number,
                       (SELECT count(*) FROM api_frozen_leaderboard_entries e
                        WHERE e.leaderboard_id = leaderboard.id),
                       (SELECT count(*) FROM api_frozen_leaderboard_entries e
                        WHERE e.leaderboard_id = leaderboard.id
                          AND e.freshness = 'stale'),
                       (SELECT max(e.observed_at) FROM api_frozen_leaderboard_entries e
                        WHERE e.leaderboard_id = leaderboard.id)
                FROM api_frozen_leaderboards AS leaderboard
                JOIN LATERAL (
                    SELECT day.official_season_id, day.season_day_number
                    FROM ranked_day_versions AS day
                    WHERE day.ranked_day_end = leaderboard.boundary_at
                    ORDER BY day.id DESC LIMIT 1
                ) AS selector ON true
                WHERE leaderboard.id = %s
                """,
                (locator[1],),
            ).fetchone()
            assert legacy_row is not None
            legacy = (*legacy_row[:8], locator[2], locator[3], *legacy_row[8:])
            total_entries = int(legacy[10])
            if offset and offset >= total_entries:
                return None
            rows = connection.execute(
                """
                SELECT entry.position, player.normalized_tag, profile.name,
                       profile.profile_json -> 'clan' ->> 'name',
                       entry.trophies, entry.observed_at, entry.freshness,
                       entry.confidence, entry.official_rank
                FROM api_frozen_leaderboard_entries AS entry
                JOIN players AS player ON player.id = entry.player_id
                LEFT JOIN player_profile_versions AS profile
                  ON profile.id = player.current_profile_version_id
                WHERE entry.leaderboard_id = %s
                ORDER BY entry.position LIMIT %s OFFSET %s
                """,
                (legacy[0], limit, offset),
            ).fetchall()
            boundary_at = legacy[2].astimezone(UTC)
            newest_input_at = (legacy[12] or boundary_at).astimezone(UTC)
            season_start = boundary_at - timedelta(days=int(legacy[7]))
            season_end = season_start + timedelta(days=28)
            coverage = dict(legacy[5])
            measured = float(coverage.get("measured", 0))
            population = int(coverage.get("eligible_population", total_entries))
            page_count = (total_entries + limit - 1) // limit
            page = offset // limit + 1
            return {
                "kind": "frozen",
                "snapshot_id": str(legacy[1]),
                "boundary_at": boundary_at.isoformat(),
                "reset_at": boundary_at.isoformat(),
                "official_season_id": _text(legacy[6]),
                "season_day_number": int(legacy[7]),
                "season_start_at": season_start.isoformat(),
                "season_end_at": season_end.isoformat(),
                "previous_snapshot": legacy[8],
                "next_snapshot": legacy[9],
                "generated_at": boundary_at.isoformat(),
                "version": int(legacy[3]),
                "ordering_rule_version": _text(legacy[4]),
                "tracked_population": population,
                "total_entries": total_entries,
                "page": page,
                "page_size": limit,
                "page_count": page_count,
                "has_previous": page > 1,
                "has_next": page < page_count,
                "coverage": {
                    "state": "partial",
                    "tracked_players": population,
                    "measured_percent": measured * 100
                    if measured <= 1
                    else measured,
                    "note": "Published frozen snapshot coverage is measured from its accepted population.",
                },
                "provenance": {
                    "source": "published frozen leaderboard snapshot",
                    "observed_at": newest_input_at.isoformat(),
                    "freshness": "stale" if int(legacy[11]) else "fresh",
                    "confidence": "partial",
                    "coverage": "partial",
                    "version": _text(legacy[4]),
                },
                "quality_states": ["partial"]
                + (["stale"] if int(legacy[11]) else []),
                "entries": [
                    {
                        "position": int(row[0]),
                        "tag": _text(row[1]),
                        "name": None if row[2] is None else _text(row[2]),
                        "clan": None if row[3] is None else _text(row[3]),
                        "trophies": int(row[4]),
                        "observed_at": row[5].astimezone(UTC).isoformat(),
                        "age_seconds": max(
                            0,
                            int(
                                (
                                    now.astimezone(UTC) - row[5].astimezone(UTC)
                                ).total_seconds()
                            ),
                        ),
                        "freshness": _text(row[6]),
                        "confidence": _text(row[7]),
                        "public_confidence": _public_snapshot_confidence(
                            _text(row[7])
                        ),
                        "official_rank": None if row[8] is None else int(row[8]),
                    }
                    for row in rows
                ],
            }
        total_entries = int(snapshot[7])
        if offset and offset >= total_entries:
            return None
        profile_identity_clause = (
            """
            version.id = entry.profile_version_id
               OR (entry.profile_version_id IS NULL
                   AND version.observation_id = entry.profile_observation_id)
            """
            if coordinator_exists
            else "version.observation_id = entry.profile_observation_id"
        )
        rows = connection.execute(
            f"""
            SELECT entry.position, player.normalized_tag, profile.name,
                   entry.trophies, entry.profile_observed_at,
                   entry.profile_freshness, entry.profile_confidence,
                   entry.official_rank, profile.clan
            FROM leaderboard_snapshot_entries AS entry
            JOIN players AS player ON player.id = entry.player_id
            LEFT JOIN LATERAL (
                SELECT version.name, version.profile_json -> 'clan' ->> 'name' AS clan
                FROM player_profile_versions AS version
                WHERE {profile_identity_clause}
                ORDER BY version.id DESC LIMIT 1
            ) AS profile ON true
            WHERE entry.snapshot_id = %s
            ORDER BY entry.position LIMIT %s OFFSET %s
            """,
            (snapshot[0], limit, offset),
        ).fetchall()
        boundary_at = snapshot[1].astimezone(UTC)
        newest_input_at = (snapshot[16] or boundary_at).astimezone(UTC)
        season_start = boundary_at - timedelta(days=int(snapshot[15]))
        season_end = season_start + timedelta(days=28)
        measured = float(snapshot[5])
        page_count = (total_entries + limit - 1) // limit
        page = offset // limit + 1
        return {
            "kind": "frozen",
            "snapshot_id": str(snapshot[0]),
            "boundary_at": boundary_at.isoformat(),
            "reset_at": boundary_at.isoformat(),
            "official_season_id": _text(snapshot[14]),
            "season_day_number": int(snapshot[15]),
            "season_start_at": season_start.isoformat(),
            "season_end_at": season_end.isoformat(),
            "previous_snapshot": snapshot[17],
            "next_snapshot": snapshot[18],
            "generated_at": boundary_at.isoformat(),
            "version": int(snapshot[2]),
            "ordering_rule_version": _text(snapshot[3]),
            "tracked_population": int(snapshot[6]),
            "total_entries": total_entries,
            "page": page,
            "page_size": limit,
            "page_count": page_count,
            "has_previous": page > 1,
            "has_next": page < page_count,
            "coverage": {
                "state": "partial",
                "tracked_players": int(snapshot[6]),
                "measured_percent": measured * 100 if measured <= 1 else measured,
                "note": "Published frozen snapshot coverage is measured from its accepted population.",
            },
            "provenance": {
                "source": "published frozen leaderboard snapshot",
                "observed_at": newest_input_at.isoformat(),
                "freshness": "stale" if int(snapshot[8]) else "fresh",
                "confidence": "partial",
                "coverage": "partial",
                "version": _text(snapshot[3]),
            },
            "quality_states": ["partial"] + (["stale"] if int(snapshot[8]) else []),
            "entries": [
                {
                    "position": int(row[0]),
                    "tag": _text(row[1]),
                    "name": None if row[2] is None else _text(row[2]),
                    "trophies": int(row[3]),
                    "observed_at": row[4].astimezone(UTC).isoformat(),
                    "age_seconds": max(
                        0,
                        int(
                            (
                                now.astimezone(UTC) - row[4].astimezone(UTC)
                            ).total_seconds()
                        ),
                    ),
                    "freshness": _text(row[5]),
                    "confidence": _text(row[6]),
                    "public_confidence": _public_snapshot_confidence(_text(row[6])),
                    "official_rank": None if row[7] is None else int(row[7]),
                    "clan": None if row[8] is None else _text(row[8]),
                }
                for row in rows
            ],
        }
