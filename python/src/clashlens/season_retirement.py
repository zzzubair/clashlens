"""Bounded, preview-first completed-season detail retirement (issue #82).

Finalized seasons keep independently readable player/army summaries while
their heavyweight daily logs, army facts, and exclusively-retired battle
detail are removed in bounded, restartable batches. A durable
``season_detail_retirements`` row stores boundaries, summary digests, and
progress so the record survives log deletion and fences later writers.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .season_finalization_guard import close_blockers

RETIREMENT_VERSION = "season-detail-retirement-v1"
SEASON_DETAIL_RETIRED = "season_detail_retired"
# Late battles, replays and corrections land for a week after the Season
# ends; finalization stops corrections and retirement deletes detail.
SEASON_CLOSE_WAIT = timedelta(days=7)

_MIGRATION_RELATIONS = ("clash_lens_schema_migrations",)
_RETIREMENT_GATE = "clashlens:season-retirement-global"


def acquire_retirement_reader(connection: Any) -> None:
    connection.execute(
        "SELECT pg_advisory_xact_lock_shared(hashtextextended(%s, 0))",
        (_RETIREMENT_GATE,),
    )


def acquire_retirement_writer(connection: Any) -> None:
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (_RETIREMENT_GATE,),
    )


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _table_exists(connection: Any, table: str) -> bool:
    row = connection.execute("SELECT to_regclass(%s) IS NOT NULL", (table,)).fetchone()
    return bool(row and row[0])


_MEASURED_RELKINDS = ("r", "p", "m", "S")
_RELKIND_LABELS = {
    "r": "ordinary table",
    "p": "partitioned table parent",
    "m": "materialized view",
    "S": "sequence",
}


def _application_relations(connection: Any) -> list[tuple[str, str]]:
    rows = connection.execute(
        """
        SELECT c.relname, c.relkind
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = current_schema()
          AND c.relkind IN ('r', 'p', 'm', 'S')
          AND c.relname <> ALL(%s::text[])
        ORDER BY c.relname
        """,
        (list(_MIGRATION_RELATIONS),),
    ).fetchall()
    return [(_text(row[0]), _text(row[1])) for row in rows]


def acquire_season_lock(connection: Any, season_id: str) -> None:
    """Exclusive transaction-scoped season lock held by retirement only.

    Finalization and retirement take this so no writer can be mid-flight
    while a season is fenced or its detail deleted.
    """
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s))",
        (f"season-retirement:{season_id}",),
    )


def acquire_season_lock_shared(connection: Any, season_id: str) -> None:
    """Shared transaction-scoped season lock for detail writers.

    Writers hold this so finalization/retirement still fences them; they
    do not exclude each other. Writer-vs-writer conflicts stay on upsert
    keys, row locks, and finer-grained advisory locks.
    """
    connection.execute(
        "SELECT pg_advisory_xact_lock_shared(hashtext(%s))",
        (f"season-retirement:{season_id}",),
    )


_lock_season = acquire_season_lock


def _check_season_id(season_id: str) -> str:
    if not isinstance(season_id, str) or not season_id or len(season_id) > 128:
        raise ValueError("official season id is outside the supported range")
    return season_id


def _check_batch(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 1000:
        raise ValueError(f"{label} is outside the supported range")
    return value


def is_season_detail_retired(connection: Any, season_id: str) -> bool:
    """True once a season is finalized (writers fenced) or retired.

    Mutating callers hold a season lock first and keep it through their
    writes: shared for writers, exclusive for retirement.
    """
    if not _table_exists(connection, "season_detail_retirements"):
        return False
    row = connection.execute(
        """
        SELECT 1 FROM season_detail_retirements
        WHERE official_season_id = %s AND status IN ('finalized', 'retired')
        """,
        (season_id,),
    ).fetchone()
    return row is not None


def retired_day_ranges(connection: Any) -> list[tuple[Any, Any]]:
    """(start, end) bounds of fenced seasons for rolling-log filtering."""
    if not _table_exists(connection, "season_detail_retirements"):
        return []
    return [
        (row[0], row[1])
        for row in connection.execute(
            """
            SELECT season_start, season_end FROM season_detail_retirements
            WHERE status IN ('finalized', 'retired')
              AND season_start IS NOT NULL AND season_end IS NOT NULL
            """
        ).fetchall()
    ]


def is_detail_retired_for_day(connection: Any, day: datetime) -> bool:
    """True when a ranked-day start falls inside a fenced season."""
    for start, end in retired_day_ranges(connection):
        if start <= day.astimezone(UTC) < end.astimezone(UTC):
            return True
    return False


def filter_live_rows(rows: list[Any], day_of: Any, ranges: list[tuple[Any, Any]]) -> list[Any]:
    """Drop rows whose ranked day falls inside a fenced season.

    Rolling logs may carry retired battles: callers skip those rows but
    still process live-season content in the same observation.
    """
    if not ranges:
        return rows
    kept: list[Any] = []
    for row in rows:
        day = day_of(row)
        moment = day.astimezone(UTC) if day.tzinfo else day.replace(tzinfo=UTC)
        if not any(start <= moment < end for start, end in ranges):
            kept.append(row)
    return kept


def _aware_utc(now: datetime) -> datetime:
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("season close time must be timezone-aware")
    return now.astimezone(UTC)


def season_close_block(
    canonical: tuple[datetime, datetime] | None, stored: Any, now: datetime
) -> tuple[dict[str, str], str | None]:
    """Return the known eligible time and why a Season cannot close yet.

    ``canonical`` is the exact Season window from confirmed timing, or
    None when it cannot be established. ``stored`` is the window a record
    holds, which older code may have written early; it must match exactly,
    and a finalized record is not evidence that the wait passed. Closing
    waits until seven days after the Season end, equality included. This
    is the clock check only: every other check still applies.
    """
    now_utc = _aware_utc(now)
    if canonical is None:
        return {}, "unknown_season_boundary"
    eligible_at = canonical[1].astimezone(UTC) + SEASON_CLOSE_WAIT
    eligible = {"eligible_at": eligible_at.isoformat()}
    if None in tuple(stored):
        return eligible, "unknown_season_boundary"
    if tuple(stored) != tuple(canonical):
        return eligible, "conflicting_season_boundary"
    if now_utc < eligible_at:
        return eligible, "season_close_wait"
    return eligible, None


def _digest_pairs(pairs: list[tuple[str, str]]) -> str:
    canonical = "\n".join(f"{key}:{digest}" for key, digest in sorted(pairs))
    return hashlib.sha256(canonical.encode()).hexdigest()


def finalize_season_detail(
    connection: Any, season_id: str, now: datetime, *, apply: bool = False
) -> dict[str, Any]:
    """Verify a completed season and persist the retirement fence.

    Preview (``apply=False``) runs every check without writing. Apply
    reruns them and inserts the ``finalized`` record that fences writers;
    detail deletion happens separately in :func:`retire_season_detail` so
    a crash between the two is restart-safe. Both wait for
    :data:`SEASON_CLOSE_WAIT` after the Season end and refuse while the
    close guard finds blocking work or missing promised history.
    """
    from .army_history import HISTORY_CATEGORIES
    from .army_season_summaries import LENSES, _project_lens
    from .army_season_summaries import _digest as _army_digest
    from .season_summaries import _digest as _player_digest
    from .season_summaries import _project, _season_completed

    season_id = _check_season_id(season_id)
    now_utc = _aware_utc(now)
    acquire_retirement_writer(connection)
    _lock_season(connection, season_id)
    if _table_exists(connection, "season_detail_retirements"):
        existing = connection.execute(
            """
            SELECT status, season_start, season_end, player_summary_count,
                   player_summary_digest, army_summary_digest, finalized_at,
                   retired_at, progress
            FROM season_detail_retirements WHERE official_season_id = %s
            """,
            (season_id,),
        ).fetchone()
        if existing is not None:
            close_at, reason = season_close_block(
                _canonical_season_bounds(connection, season_id), existing[1:3], now_utc
            )
            blocking_work = (
                {} if reason else close_blockers(connection, season_id, *existing[1:3])
            )
            if reason is not None or blocking_work:
                return {
                    "season_id": season_id,
                    "status": "blocked",
                    "reason": reason or "verification_failed",
                    **close_at,
                    **({"blocking_work": blocking_work} if blocking_work else {}),
                    "existing_status": _text(existing[0]),
                    "already_finalized": True,
                    "applied": False,
                }
            return {
                "season_id": season_id,
                "status": _text(existing[0]),
                "already_finalized": True,
                "applied": False,
            }
    else:
        return {
            "season_id": season_id,
            "status": "missing_retirement_table",
            "already_finalized": False,
            "applied": False,
        }
    bounds = _canonical_season_bounds(connection, season_id)
    close_at, close_reason = season_close_block(bounds, bounds, now_utc)
    completed, reason = _season_completed(connection, season_id, now_utc)
    if not completed:
        return {
            "season_id": season_id,
            "status": "not_completed",
            "reason": reason,
            **close_at,
            "already_finalized": False,
            "applied": False,
        }
    if close_reason is not None:
        return {
            "season_id": season_id,
            "status": "blocked",
            "reason": close_reason,
            **close_at,
            "already_finalized": False,
            "applied": False,
        }
    season_start, season_end = bounds
    player_ids = [
        int(row[0])
        for row in connection.execute(
            """
            SELECT DISTINCT player_id FROM api_player_daily_logs
            WHERE official_season_id = %s ORDER BY player_id
            """,
            (season_id,),
        ).fetchall()
    ]
    if not player_ids:
        return {
            "season_id": season_id,
            "status": "blocked",
            "reason": "no_history",
            **close_at,
            "already_finalized": False,
            "applied": False,
        }
    stored_players = {
        int(row[0]): _text(row[1])
        for row in connection.execute(
            """
            SELECT player_id, content_digest FROM player_season_summaries
            WHERE official_season_id = %s
            """,
            (season_id,),
        ).fetchall()
    }
    missing_players = [pid for pid in player_ids if pid not in stored_players]
    stale_players: list[int] = []
    player_pairs: list[tuple[str, str]] = []
    failures: list[dict[str, Any]] = []
    for player_id in player_ids:
        if player_id in missing_players:
            continue
        try:
            projected = _project(player_id, season_id, connection)
        except Exception as error:  # noqa: BLE001 - reported, blocks finalization
            failures.append({"player_id": player_id, "error": str(error)[:200]})
            continue
        if projected is None:
            failures.append({"player_id": player_id, "error": "projection_missing"})
            continue
        digest = _player_digest(projected)
        player_pairs.append((str(player_id), digest))
        if digest != stored_players[player_id]:
            stale_players.append(player_id)
    army_pairs: list[tuple[str, str]] = []
    missing_army: list[str] = []
    stale_army: list[str] = []
    stored_army = {
        (_text(row[0]), _text(row[1])): _text(row[2])
        for row in connection.execute(
            """
            SELECT lens, category, content_digest FROM army_season_summaries
            WHERE official_season_id = %s
            """,
            (season_id,),
        ).fetchall()
    }
    for lens in LENSES:
        try:
            projected_lens = _project_lens(
                connection, season_id, lens, recount=True
            )
        except Exception as error:  # noqa: BLE001 - reported, blocks finalization
            failures.append({"lens": lens, "error": str(error)[:200]})
            continue
        for category in sorted(HISTORY_CATEGORIES):
            key = f"{lens}:{category}"
            summary = projected_lens.get(category)
            if summary is None:
                failures.append({"lens": lens, "category": category, "error": "projection_missing"})
                continue
            digest = _army_digest(summary)
            army_pairs.append((key, digest))
            if (lens, category) not in stored_army:
                missing_army.append(key)
            elif stored_army[(lens, category)] != digest:
                stale_army.append(key)
    blocking_work = close_blockers(connection, season_id, season_start, season_end)
    if missing_players or stale_players or missing_army or stale_army or failures or blocking_work:
        return {
            "season_id": season_id,
            "status": "blocked",
            "reason": "verification_failed",
            **close_at,
            "missing_player_summaries": missing_players[:50],
            "missing_player_count": len(missing_players),
            "stale_player_summaries": stale_players[:50],
            "stale_player_count": len(stale_players),
            "missing_army_summaries": missing_army,
            "stale_army_summaries": stale_army,
            "failures": failures[:20],
            "blocking_work": blocking_work,
            "already_finalized": False,
            "applied": False,
        }
    player_digest = _digest_pairs(player_pairs)
    army_digest = _digest_pairs(army_pairs)
    if not apply:
        return {
            "season_id": season_id,
            "status": "ready",
            "season_start": season_start.isoformat(),
            "season_end": season_end.isoformat(),
            "player_summary_count": len(player_pairs),
            "player_summary_digest": player_digest,
            "army_summary_digest": army_digest,
            "already_finalized": False,
            "applied": False,
        }
    with connection.transaction():
        _lock_season(connection, season_id)
        connection.execute(
            """
            INSERT INTO season_detail_retirements (
                official_season_id, status, season_start, season_end,
                player_summary_count, player_summary_digest,
                army_summary_digest, progress
            ) VALUES (%s, 'finalized', %s, %s, %s, %s, %s,
                      '{"finalized_by": "finalize-season-detail"}'::jsonb)
            ON CONFLICT (official_season_id) DO NOTHING
            """,
            (season_id, season_start, season_end, len(player_pairs), player_digest, army_digest),
        )
    return {
        "season_id": season_id,
        "status": "finalized",
        "season_start": season_start.isoformat(),
        "season_end": season_end.isoformat(),
        "player_summary_count": len(player_pairs),
        "player_summary_digest": player_digest,
        "army_summary_digest": army_digest,
        "already_finalized": False,
        "applied": True,
    }


def _canonical_season_bounds(connection: Any, season_id: str) -> tuple[datetime, datetime] | None:
    """Return the exact 28-day window, never the observed log envelope.

    The confirmed anchor gives the current and previous Season; an older
    Season's canonical id is its start on the same 28-day calendar. A
    later, misaligned or noncanonical id is unknown.
    """
    from .domain import (
        SEASON_ANCHOR_RULE_VERSION,
        SEASON_DURATION,
        DomainRuleError,
        _canonical_season_start,
    )

    anchor = connection.execute(
        """
        SELECT current_league_season_id, previous_league_season_id,
               current_start, previous_start
        FROM legend_season_anchors
        WHERE state = 'confirmed' AND anchor_rule_version = %s
        ORDER BY current_start DESC LIMIT 1
        """,
        (SEASON_ANCHOR_RULE_VERSION,),
    ).fetchone()
    if anchor is None:
        return None
    for anchored_id, start in ((anchor[0], anchor[2]), (anchor[1], anchor[3])):
        if _text(anchored_id) == season_id:
            return start, start + SEASON_DURATION
    try:
        start = _canonical_season_start(season_id)
    except DomainRuleError:
        return None
    if start >= anchor[3] or (anchor[3] - start) % SEASON_DURATION:
        return None
    return start, start + SEASON_DURATION


def retire_season_detail(
    connection: Any,
    season_id: str,
    *,
    max_rows: int = 500,
    apply: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Delete finalized-season detail in bounded, restartable batches.

    Preview (``apply=False``) counts eligible rows without writing.
    Apply deletes daily logs, army facts, then exclusively-retired battle
    detail, updates progress, and marks the season ``retired`` once every
    counter reaches zero. Summaries and protected records are never
    deleted here. Every call rechecks the stored window and the close
    wait against ``now``, the database clock by default, and the close
    guard, so a ``finalized`` record is never enough on its own.
    """
    season_id = _check_season_id(season_id)
    max_rows = _check_batch(max_rows, "retirement batch size")
    if now is not None:
        now = _aware_utc(now)
    _lock_season(connection, season_id)
    if now is None:
        now = _aware_utc(connection.execute("SELECT clock_timestamp()").fetchone()[0])
    if not _table_exists(connection, "season_detail_retirements"):
        return {"season_id": season_id, "status": "not_finalized", "applied": False}
    record = connection.execute(
        """
        SELECT status, season_start, season_end, player_summary_count,
               player_summary_digest, army_summary_digest, progress
        FROM season_detail_retirements WHERE official_season_id = %s
        """,
        (season_id,),
    ).fetchone()
    if record is None:
        return {"season_id": season_id, "status": "not_finalized", "applied": False}
    status, season_start, season_end = _text(record[0]), record[1], record[2]
    close_at, reason = season_close_block(
        _canonical_season_bounds(connection, season_id), (season_start, season_end), now
    )
    if reason is not None:
        return {
            "season_id": season_id,
            "status": "blocked",
            "reason": reason,
            **close_at,
            "existing_status": status,
            "applied": False,
        }
    summary_check = _verify_stored_summaries(
        connection, season_id, _text(record[4]), _text(record[5])
    )
    if not summary_check["ok"]:
        return {
            "season_id": season_id,
            "status": "blocked",
            "reason": summary_check["reason"],
            **close_at,
            "applied": False,
        }
    blocking_work = close_blockers(connection, season_id, season_start, season_end)
    if blocking_work:
        return {
            "season_id": season_id,
            "status": "blocked",
            "reason": "verification_failed",
            **close_at,
            "blocking_work": blocking_work,
            "existing_status": status,
            "applied": False,
        }
    eligible = _eligible_counts(connection, season_id, season_start, season_end, max_rows)
    if not apply:
        return {
            "season_id": season_id,
            "status": status,
            "applied": False,
            **{f"eligible_{key}": value for key, value in eligible.items()},
        }
    deleted = _delete_retirement_batch(
        connection, season_id, season_start, season_end, max_rows
    )
    remaining = _eligible_counts(connection, season_id, season_start, season_end, 1)
    done = all(value == 0 for value in remaining.values())
    with connection.transaction():
        _lock_season(connection, season_id)
        connection.execute(
            """
            UPDATE season_detail_retirements
            SET progress = %s::jsonb,
                status = CASE WHEN %s THEN 'retired' ELSE status END,
                retired_at = CASE WHEN %s THEN clock_timestamp() ELSE retired_at END
            WHERE official_season_id = %s
            """,
            (
                json.dumps(
                    {
                        "last_deleted": deleted,
                        "last_remaining": remaining,
                        "retirement_version": RETIREMENT_VERSION,
                    }
                ),
                done,
                done,
                season_id,
            ),
        )
    return {
        "season_id": season_id,
        "status": "retired" if done else status,
        "applied": True,
        **{f"deleted_{key}": value for key, value in deleted.items()},
        **{f"remaining_{key}": value for key, value in remaining.items()},
    }


def _verify_stored_summaries(
    connection: Any, season_id: str, player_digest: str, army_digest: str
) -> dict[str, Any]:
    """Refuse retirement when the only valid summaries are gone or changed."""
    player_pairs = [
        (str(row[0]), _text(row[1]))
        for row in connection.execute(
            """
            SELECT player_id, content_digest FROM player_season_summaries
            WHERE official_season_id = %s ORDER BY player_id
            """,
            (season_id,),
        ).fetchall()
    ]
    if not player_pairs:
        return {"ok": False, "reason": "player_summaries_missing"}
    if _digest_pairs(player_pairs) != player_digest:
        return {"ok": False, "reason": "player_summaries_changed"}
    army_pairs = [
        (f"{_text(row[0])}:{_text(row[1])}", _text(row[2]))
        for row in connection.execute(
            """
            SELECT lens, category, content_digest FROM army_season_summaries
            WHERE official_season_id = %s ORDER BY lens, category
            """,
            (season_id,),
        ).fetchall()
    ]
    if not army_pairs:
        return {"ok": False, "reason": "army_summaries_missing"}
    if _digest_pairs(army_pairs) != army_digest:
        return {"ok": False, "reason": "army_summaries_changed"}
    return {"ok": True}


def _eligible_counts(
    connection: Any, season_id: str, season_start: Any, season_end: Any, limit: int
) -> dict[str, int]:
    counts: dict[str, int] = {}
    counts["daily_logs"] = connection.execute(
        """
        SELECT count(*) FROM (
            SELECT id FROM api_player_daily_logs
            WHERE official_season_id = %s ORDER BY id LIMIT %s
        ) AS candidates
        """,
        (season_id, limit),
    ).fetchone()[0]
    counts["army_facts"] = connection.execute(
        """
        SELECT count(*) FROM (
            SELECT fact.id FROM army_analytics_battle_facts AS fact
            WHERE fact.official_season_id = %s
              AND NOT EXISTS (
                  SELECT 1 FROM army_analytics_battle_facts AS newer
                  WHERE newer.supersedes_id = fact.id
              )
            ORDER BY fact.id LIMIT %s
        ) AS candidates
        """,
        (season_id, limit),
    ).fetchone()[0]
    counts["completed_days"] = connection.execute(
        """
        SELECT count(*) FROM (
            SELECT ranked_day_start FROM army_analytics_completed_days
            WHERE official_season_id = %s ORDER BY ranked_day_start LIMIT %s
        ) AS candidates
        """,
        (season_id, limit),
    ).fetchone()[0]
    counts["battles"] = len(
        _eligible_battle_ids(connection, season_start, season_end, limit)
    )
    return counts


def _eligible_battle_ids(
    connection: Any, season_start: Any, season_end: Any, limit: int
) -> list[int]:
    """Battles strictly inside the retired season no live work still needs."""
    return [
        int(row[0])
        for row in connection.execute(
            """
            SELECT battle.id FROM legend_battles AS battle
            WHERE battle.ranked_day_start >= %s
              AND battle.ranked_day_start < %s
              AND NOT EXISTS (
                  SELECT 1 FROM army_analytics_battle_facts AS fact
                  WHERE fact.battle_id = battle.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM api_player_daily_logs AS log
                  WHERE log.ranked_day_start = battle.ranked_day_start
              )
            ORDER BY battle.id LIMIT %s
            FOR UPDATE OF battle SKIP LOCKED
            """,
            (season_start, season_end, limit),
        ).fetchall()
    ]


def _delete_retirement_batch(
    connection: Any, season_id: str, season_start: Any, season_end: Any, limit: int
) -> dict[str, int]:
    deleted: dict[str, int] = {}
    with connection.transaction():
        connection.execute("SET LOCAL lock_timeout = '1s'")
        connection.execute("SET LOCAL statement_timeout = '30s'")
        rows = connection.execute(
            """
            SELECT id FROM api_player_daily_logs
            WHERE official_season_id = %s ORDER BY id LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,
            (season_id, limit),
        ).fetchall()
        ids = [int(row[0]) for row in rows]
        deleted["daily_logs"] = (
            connection.execute(
                "DELETE FROM api_player_daily_logs WHERE id = ANY(%s::bigint[])",
                (ids,),
            ).rowcount
            if ids
            else 0
        )
    with connection.transaction():
        connection.execute("SET LOCAL lock_timeout = '1s'")
        connection.execute("SET LOCAL statement_timeout = '30s'")
        rows = connection.execute(
            """
            SELECT ranked_day_start FROM army_analytics_completed_days
            WHERE official_season_id = %s ORDER BY ranked_day_start LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,
            (season_id, limit),
        ).fetchall()
        deleted["completed_days"] = (
            connection.execute(
                "DELETE FROM army_analytics_completed_days WHERE ranked_day_start = ANY(%s::timestamptz[])",
                ([row[0] for row in rows],),
            ).rowcount
            if rows
            else 0
        )
    with connection.transaction():
        connection.execute("SET LOCAL lock_timeout = '1s'")
        connection.execute("SET LOCAL statement_timeout = '30s'")
        rows = connection.execute(
            """
            SELECT fact.id FROM army_analytics_battle_facts AS fact
            WHERE fact.official_season_id = %s
              AND NOT EXISTS (
                  SELECT 1 FROM army_analytics_battle_facts AS newer
                  WHERE newer.supersedes_id = fact.id
              )
            ORDER BY fact.id LIMIT %s
            FOR UPDATE OF fact SKIP LOCKED
            """,
            (season_id, limit),
        ).fetchall()
        ids = [int(row[0]) for row in rows]
        # Version chains link newer facts to older ones through a
        # restrictive self-FK; delete leaves first so one batch never
        # trips the constraint. Anything left stays for the next batch.
        deleted["army_facts"] = _delete_version_chain(
            connection, "army_analytics_battle_facts", ids
        )
    with connection.transaction():
        connection.execute("SET LOCAL lock_timeout = '1s'")
        connection.execute("SET LOCAL statement_timeout = '30s'")
        battle_ids = _eligible_battle_ids(connection, season_start, season_end, limit)
        deleted["battles"] = _delete_battles(connection, battle_ids) if battle_ids else 0
    return deleted


def _delete_version_chain(connection: Any, table: str, ids: list[int]) -> int:
    """Delete rows linked by a restrictive ``supersedes_id`` self-FK.

    Newer rows reference older ones, so each pass removes only rows no
    remaining row still supersedes. Bounded passes keep one batch from
    tripping the constraint; leftovers stay for the next batch.
    """
    total = 0
    for _ in range(10):
        removed = connection.execute(
            f"""
            DELETE FROM {table}
            WHERE id = ANY(%s::bigint[])
              AND NOT EXISTS (
                  SELECT 1 FROM {table} AS newer
                  WHERE newer.supersedes_id = {table}.id
              )
            """,
            (ids,),
        ).rowcount
        total += removed
        if not removed:
            break
    return total


def _delete_battles(connection: Any, battle_ids: list[int]) -> int:
    """Delete battles plus exclusively-retired associated detail."""
    evidence = connection.execute(
        """
        SELECT id, source_row_id FROM battle_evidence
        WHERE battle_id = ANY(%s::bigint[])
        """,
        (battle_ids,),
    ).fetchall()
    source_rows = sorted({int(row[1]) for row in evidence})
    decode_rows = connection.execute(
        """
        SELECT id FROM battle_army_decodes
        WHERE battle_id = ANY(%s::bigint[])
        """,
        (battle_ids,),
    ).fetchall()
    _delete_version_chain(
        connection, "battle_army_decodes", [int(row[0]) for row in decode_rows]
    )
    if connection.execute(
        """
        SELECT 1 FROM battle_army_decodes
        WHERE battle_id = ANY(%s::bigint[])
        LIMIT 1
        """,
        (battle_ids,),
    ).fetchone():
        # A deep supersedes chain needs another bounded run. Do not remove
        # evidence while restrictive decode references still exist.
        return 0
    connection.execute(
        "DELETE FROM battle_perspectives WHERE battle_id = ANY(%s::bigint[])",
        (battle_ids,),
    )
    connection.execute(
        "DELETE FROM battle_evidence WHERE battle_id = ANY(%s::bigint[])",
        (battle_ids,),
    )
    if source_rows:
        unreferenced = [
            int(row[0])
            for row in connection.execute(
                """
                SELECT id FROM unnest(%s::bigint[]) AS candidate (id)
                WHERE NOT EXISTS (
                    SELECT 1 FROM battle_evidence AS remaining
                    WHERE remaining.source_row_id = candidate.id
                )
                  AND NOT EXISTS (
                    SELECT 1 FROM battle_log_observation_rows AS remaining
                    WHERE remaining.source_row_id = candidate.id
                )
                """,
                (source_rows,),
            ).fetchall()
        ]
        connection.execute(
            "DELETE FROM battle_payload_rows WHERE source_row_id = ANY(%s::bigint[])",
            (unreferenced,),
        )
        # A list keeps each remaining battle at its log position; a list
        # with nothing left goes, as its per-row predecessors did.
        connection.execute(
            """
            DELETE FROM battle_payload_row_lists AS list
            WHERE list.source_row_ids && %(ids)s::bigint[]
              AND NOT EXISTS (
                  SELECT 1 FROM unnest(list.source_row_ids) AS member (id)
                  WHERE member.id <> ALL(%(ids)s::bigint[])
              )
            """,
            {"ids": unreferenced},
        )
        connection.execute(
            """
            UPDATE battle_payload_row_lists AS list
            SET source_row_ids = ARRAY(
                SELECT CASE WHEN member.id = ANY(%(ids)s::bigint[]) THEN NULL
                            ELSE member.id END
                FROM unnest(list.source_row_ids) WITH ORDINALITY
                    AS member (id, position)
                ORDER BY member.position
            )
            WHERE list.source_row_ids && %(ids)s::bigint[]
            """,
            {"ids": unreferenced},
        )
        connection.execute(
            """
            DELETE FROM battle_source_rows
            WHERE id = ANY(%s::bigint[])
              AND NOT EXISTS (
                  SELECT 1 FROM battle_evidence AS remaining
                  WHERE remaining.source_row_id = battle_source_rows.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM battle_log_observation_rows AS remaining
                  WHERE remaining.source_row_id = battle_source_rows.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM battle_payload_rows AS remaining
                  WHERE remaining.source_row_id = battle_source_rows.id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM battle_payload_row_lists AS remaining
                  WHERE remaining.source_row_ids @> ARRAY[battle_source_rows.id]
              )
            """,
            (source_rows,),
        )
    return connection.execute(
        """
        DELETE FROM legend_battles WHERE id = ANY(%s::bigint[])
          AND NOT EXISTS (
              SELECT 1 FROM army_analytics_battle_facts AS fact
              WHERE fact.battle_id = legend_battles.id
          )
          AND NOT EXISTS (
              SELECT 1 FROM battle_evidence AS remaining
              WHERE remaining.battle_id = legend_battles.id
          )
          AND NOT EXISTS (
              SELECT 1 FROM battle_perspectives AS remaining
              WHERE remaining.battle_id = legend_battles.id
          )
          AND NOT EXISTS (
              SELECT 1 FROM battle_army_decodes AS remaining
              WHERE remaining.battle_id = legend_battles.id
          )
        """,
        (battle_ids,),
    ).rowcount


def measure_season_storage(
    connection: Any, season_id: str | None = None
) -> dict[str, Any]:
    """Reproducible storage snapshot for one season (or the whole database).

    Reports allocated bytes (heap + indexes + TOAST via
    ``pg_total_relation_size``), row counts, and compact-summary size
    distribution. It does not measure WAL generation, retained WAL/backups,
    spool occupancy, or remote raw tariffs; those need the documented
    host/archive procedure and are reported as explicit gaps, never as
    zero cost.
    """
    tables: dict[str, Any] = {}
    for table, relkind in _application_relations(connection):
        # Partition parents have no relation file; counting their children
        # again would overstate both rows and allocation. Sequences have
        # allocation but are not queryable as ordinary tables.
        if relkind == "p":
            size = heap = count = 0
        else:
            size = connection.execute(
                "SELECT pg_total_relation_size(to_regclass(%s))", (table,)
            ).fetchone()[0]
            heap = connection.execute(
                "SELECT pg_relation_size(to_regclass(%s))", (table,)
            ).fetchone()[0]
            count = (
                0
                if relkind == "S"
                else connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            )
        tables[table] = {
            "relation_kind": relkind,
            "allocated_bytes": int(size),
            "heap_bytes": int(heap),
            "index_toast_bytes": int(size) - int(heap),
            "rows": int(count),
            "row_count_semantics": (
                "not_applicable_for_sequence"
                if relkind == "S"
                else "not_counted_for_partition_parent"
                if relkind == "p"
                else "relation_rows"
            ),
        }
    relation_kinds = connection.execute(
        """
        SELECT c.relkind, count(*)
        FROM pg_class AS c
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = current_schema()
          AND c.relname <> ALL(%s::text[])
        GROUP BY c.relkind ORDER BY c.relkind
        """,
        (list(_MIGRATION_RELATIONS),),
    ).fetchall()
    included_kinds = {
        kind: _RELKIND_LABELS[kind]
        for kind in _MEASURED_RELKINDS
        if any(_text(row[0]) == kind for row in relation_kinds)
    }
    excluded_kinds = {
        _text(row[0]): f"not measured ({_text(row[0])} relation kind)"
        for row in relation_kinds
        if _text(row[0]) not in _MEASURED_RELKINDS
    }
    summaries: dict[str, Any] = {}
    if season_id is not None and _table_exists(connection, "player_season_summaries"):
        dist = connection.execute(
            """
            SELECT count(*), min(pg_column_size(summary.*)),
                   percentile_cont(0.5) WITHIN GROUP (
                       ORDER BY pg_column_size(summary.*)),
                   max(pg_column_size(summary.*)),
                   sum(pg_column_size(summary.*))
            FROM player_season_summaries AS summary
            WHERE official_season_id = %s
            """,
            (season_id,),
        ).fetchone()
        summaries["player_season"] = {
            "rows": int(dist[0] or 0),
            "min_bytes": int(dist[1] or 0),
            "p50_bytes": float(dist[2] or 0),
            "max_bytes": int(dist[3] or 0),
            "total_bytes": int(dist[4] or 0),
        }
    if season_id is not None and _table_exists(connection, "army_season_summaries"):
        dist = connection.execute(
            """
            SELECT count(*), sum(pg_column_size(summary.*))
            FROM army_season_summaries AS summary
            WHERE official_season_id = %s
            """,
            (season_id,),
        ).fetchone()
        summaries["army_season"] = {
            "rows": int(dist[0] or 0),
            "total_bytes": int(dist[1] or 0),
        }
    season_counts: dict[str, Any] = {}
    if season_id is not None:
        for table, column in (
            ("api_player_daily_logs", "official_season_id"),
            ("army_analytics_battle_facts", "official_season_id"),
            ("ranked_day_versions", "official_season_id"),
        ):
            if _table_exists(connection, table):
                count = connection.execute(
                    f"SELECT count(*) FROM {table} WHERE {column} = %s",
                    (season_id,),
                ).fetchone()[0]
                season_counts[table] = int(count)
    return {
        "season_id": season_id,
        "tables": tables,
        "summaries": summaries,
        "season_counts": season_counts,
        "measured_relation_total_bytes": sum(
            entry["allocated_bytes"] for entry in tables.values()
        ),
        "measured_relation_scope": (
            "public application tables, partition parents, materialized views, "
            "and sequences; partition parents are cataloged with zero allocation"
        ),
        "relation_kinds": {
            "included": included_kinds,
            "excluded": excluded_kinds,
        },
        "excluded_relations": {
            "migration_metadata": list(_MIGRATION_RELATIONS),
        },
        "unmeasured": [
            "generated WAL (use pg_current_wal_lsn delta around a bounded run)",
            "retained WAL and base backups (7-day recovery window, host procedure)",
            "bounded spool occupancy (filesystem, not NVMe proof on tmpfs)",
            "remote raw bytes and request tariffs (representative novelty + provider tariff)",
        ],
    }


def project_six_months(
    *,
    player_season_bytes: float | None,
    army_season_bytes: float | None,
    live_detail_bytes_per_day: float | None,
    daily_bookkeeping_bytes_per_day: float | None,
    players: int = 12500,
    season_days: int = 28,
    months: int = 6,
    usable_bytes: int | None = None,
    headroom_fraction: float = 0.2,
) -> dict[str, Any]:
    """Project six calendar months from measured per-season/per-day bytes.

    All inputs are measured bytes, never tariff money: remote request costs
    still need representative novelty and the selected provider tariff.
    Vacuum-reusable space is not disk shrinkage. Extrapolation is labeled
    and protected-work sensitivity is explicit.
    """
    if players <= 0 or season_days <= 0 or months <= 0:
        raise ValueError("projection population is outside the supported range")
    if not 0 <= headroom_fraction < 1:
        raise ValueError("projection headroom is outside the supported range")
    days = months * 365 // 12
    seasons = days / season_days
    retained_summaries = (
        players * float(player_season_bytes) * seasons
        if player_season_bytes is not None else 0
    )
    retained_army = (
        float(army_season_bytes) * seasons if army_season_bytes is not None else 0
    )
    live_detail = (
        float(live_detail_bytes_per_day) * season_days
        if live_detail_bytes_per_day is not None else None
    )
    bookkeeping = (
        float(daily_bookkeeping_bytes_per_day) * days
        if daily_bookkeeping_bytes_per_day is not None else None
    )
    total = retained_summaries + retained_army + (live_detail or 0) + (bookkeeping or 0)
    unmeasured = [
        name for name, value in (
            ("live_detail_bytes_per_day", live_detail_bytes_per_day),
            ("daily_bookkeeping_bytes_per_day", daily_bookkeeping_bytes_per_day),
            ("player_season_bytes", player_season_bytes),
            ("army_season_bytes", army_season_bytes),
        ) if value is None
    ]
    usable = usable_bytes
    if usable is None:
        try:
            root = Path(__file__).resolve()
            usable = shutil.disk_usage(root.anchor).total
        except OSError:
            usable = 0
    budget = (float(usable) * (1.0 - headroom_fraction)) if usable else 0
    return {
        "days": days,
        "seasons": round(seasons, 2),
        "players": players,
        "retained_player_summary_bytes": int(retained_summaries),
        "retained_army_summary_bytes": int(retained_army),
        "live_detail_bytes": int(live_detail) if live_detail is not None else None,
        "bookkeeping_bytes": int(bookkeeping) if bookkeeping is not None else None,
        "projected_total_bytes": int(total),
        "projection_semantics": "lower_bound_with_unmeasured_components" if unmeasured else "measured_components",
        "unmeasured_components": unmeasured,
        "usable_bytes": int(usable or 0),
        "headroom_fraction": headroom_fraction,
        "budget_bytes": int(budget),
        "fits_budget": (None if unmeasured or not budget else bool(total <= budget)),
        "labels": [
            "synthetic extrapolation, not #60 Step 9 live validation",
            "vacuum-reusable space is not disk shrinkage",
            "protected-work residue and correction frequency shift bookkeeping",
            "remote raw/backup tariffs need representative novelty + provider rates",
        ],
    }
