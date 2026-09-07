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

RETIREMENT_VERSION = "season-detail-retirement-v1"
SEASON_DETAIL_RETIRED = "season_detail_retired"
TERMINAL_JOB_STATUSES = ("complete", "failed", "cancelled")
TERMINAL_REPLAY_STATUSES = ("complete", "failed", "cancelled")
TERMINAL_CORRECTION_STATES = ("finalized", "terminal")
ACTIVE_GENERATION_STATES = ("pending", "ready", "building")

_MEASURE_TABLES = (
    "player_season_summaries",
    "army_season_summaries",
    "api_player_daily_logs",
    "ranked_day_versions",
    "army_analytics_battle_facts",
    "army_analytics_completed_days",
    "legend_battles",
    "battle_evidence",
    "battle_perspectives",
    "battle_army_decodes",
    "battle_source_rows",
    "battle_log_observation_rows",
    "battle_payload_rows",
    "battle_log_observations",
    "collector_jobs",
    "collector_observations",
    "python_processing_jobs",
    "python_replay_requests",
    "known_player_discoveries",
    "player_discovery_events",
    "boundary_publication_generations",
    "boundary_publication_corrections",
    "api_frozen_leaderboards",
    "api_frozen_leaderboard_entries",
)


def _text(value: Any) -> str:
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


def _table_exists(connection: Any, table: str) -> bool:
    row = connection.execute("SELECT to_regclass(%s) IS NOT NULL", (table,)).fetchone()
    return bool(row and row[0])


def acquire_season_lock(connection: Any, season_id: str) -> None:
    """Use one transaction-scoped lock for every season detail writer."""
    connection.execute(
        "SELECT pg_advisory_xact_lock(hashtext(%s))",
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

    Mutating callers acquire_season_lock first and hold it through their writes.
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


def _digest_pairs(pairs: list[tuple[str, str]]) -> str:
    canonical = "\n".join(f"{key}:{digest}" for key, digest in sorted(pairs))
    return hashlib.sha256(canonical.encode()).hexdigest()


def finalize_season_detail(
    connection: Any, season_id: str, now: datetime, *, apply: bool = False
) -> dict[str, Any]:
    """Verify a completed season and persist the retirement fence.

    Preview (``apply=False``) runs every check without writing. Apply
    inserts the ``finalized`` record that fences writers; detail deletion
    happens separately in :func:`retire_season_detail` so a crash between
    the two is restart-safe.
    """
    from .army_analytics import CATEGORIES
    from .army_season_summaries import LENSES, _project_lens
    from .army_season_summaries import _digest as _army_digest
    from .season_summaries import _digest as _player_digest
    from .season_summaries import _project, _season_completed

    season_id = _check_season_id(season_id)
    now_utc = now.astimezone(UTC)
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
    completed, reason = _season_completed(connection, season_id, now_utc)
    if not completed:
        return {
            "season_id": season_id,
            "status": "not_completed",
            "reason": reason,
            "already_finalized": False,
            "applied": False,
        }
    season_start, season_end = _canonical_season_bounds(connection, season_id, now_utc)
    if season_start is None or season_end is None or not season_end <= now_utc:
        return {
            "season_id": season_id,
            "status": "blocked",
            "reason": "unknown_season_boundary",
            "already_finalized": False,
            "applied": False,
        }
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
            projected_lens = _project_lens(connection, season_id, lens)
        except Exception as error:  # noqa: BLE001 - reported, blocks finalization
            failures.append({"lens": lens, "error": str(error)[:200]})
            continue
        for category in sorted(CATEGORIES):
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
    blocking_work = _blocking_season_work(connection, season_start, season_end)
    if missing_players or stale_players or missing_army or stale_army or failures or blocking_work:
        return {
            "season_id": season_id,
            "status": "blocked",
            "reason": "verification_failed",
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


def _canonical_season_bounds(
    connection: Any, season_id: str, now: datetime
) -> tuple[Any, Any]:
    """Return the exact 28-day window, never the observed log envelope."""
    from .domain import SEASON_ANCHOR_RULE_VERSION, SEASON_DURATION

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
    if anchor is not None:
        for anchored_id, start in ((anchor[0], anchor[2]), (anchor[1], anchor[3])):
            if _text(anchored_id) == season_id and start is not None:
                return start, start + SEASON_DURATION
    witness = connection.execute(
        """
        SELECT ranked_day_start FROM api_player_daily_logs
        WHERE official_season_id = %s AND season_day_number = 28
          AND state = 'Complete' AND ranked_day_end IS NOT NULL
          AND ranked_day_end <= %s
        ORDER BY ranked_day_start LIMIT 1
        """,
        (season_id, now),
    ).fetchone()
    if witness is None:
        return None, None
    start = witness[0] - timedelta(days=27)
    return start, start + timedelta(days=28)


def _blocking_season_work(connection: Any, season_start: Any, season_end: Any) -> dict[str, int]:
    """Count non-terminal work scoped to the half-open season interval."""
    blocking: dict[str, int] = {}
    if _table_exists(connection, "python_processing_jobs") and _table_exists(
        connection, "collector_observations"
    ):
        row = connection.execute(
            """
            SELECT count(*) FROM python_processing_jobs AS job
            JOIN collector_observations AS observation
              ON observation.id = COALESCE(job.observation_id, job.replay_observation_id)
            WHERE job.status <> ALL(%s::text[])
              AND observation.response_completed_at >= %s
              AND observation.response_completed_at < %s
            """,
            (list(TERMINAL_JOB_STATUSES), season_start, season_end),
        ).fetchone()
        if row and int(row[0]):
            blocking["processing_jobs"] = int(row[0])
    if _table_exists(connection, "python_processing_jobs_worker") and _table_exists(
        connection, "legend_battles"
    ):
        row = connection.execute(
            """
            SELECT count(*) FROM python_processing_jobs_worker AS job
            WHERE job.state <> ALL(%s::text[])
              AND job.observation_id IS NULL
              AND job.replay_observation_id IS NULL
              AND (
                  (job.work_type = 'build_army_analytics' AND (
                      (job.input_json ->> 'ranked_day_start')::timestamptz >= %s
                      AND (job.input_json ->> 'ranked_day_start')::timestamptz < %s
                      OR (job.input_json ->> 'boundary_at')::timestamptz >= %s
                      AND (job.input_json ->> 'boundary_at')::timestamptz < %s
                  ))
                  OR (job.work_type = 'redecode_army' AND EXISTS (
                      SELECT 1
                      FROM jsonb_array_elements_text(
                          CASE
                              WHEN jsonb_typeof(job.input_json -> 'battle_ids') = 'array'
                                  THEN job.input_json -> 'battle_ids'
                              WHEN jsonb_typeof(job.input_json -> 'battle_id') = 'number'
                                  THEN jsonb_build_array(job.input_json -> 'battle_id')
                              ELSE '[]'::jsonb
                          END
                      ) AS requested(battle_id)
                      JOIN legend_battles AS battle
                        ON battle.id = requested.battle_id::bigint
                      WHERE battle.ranked_day_start >= %s
                        AND battle.ranked_day_start < %s
                  ))
              )
            """,
            (
                list(TERMINAL_JOB_STATUSES),
                season_start,
                season_end,
                season_start,
                season_end,
                season_start,
                season_end,
            ),
        ).fetchone()
        if row and int(row[0]):
            blocking["observationless_army_jobs"] = int(row[0])
    if _table_exists(connection, "python_replay_requests"):
        row = connection.execute(
            """
            SELECT count(*) FROM python_replay_requests AS request
            JOIN collector_observations AS observation
              ON observation.id = request.observation_id
            WHERE request.status <> ALL(%s::text[])
              AND observation.response_completed_at >= %s
              AND observation.response_completed_at < %s
            """,
            (list(TERMINAL_REPLAY_STATUSES), season_start, season_end),
        ).fetchone()
        if row and int(row[0]):
            blocking["replay_requests"] = int(row[0])
    if _table_exists(connection, "boundary_publication_generations"):
        row = connection.execute(
            """
            SELECT count(*) FROM boundary_publication_generations
            WHERE boundary_at >= %s AND boundary_at < %s
              AND (snapshot_state = ANY(%s::text[])
                   OR army_state = ANY(%s::text[]))
            """,
            (season_start, season_end, list(ACTIVE_GENERATION_STATES), list(ACTIVE_GENERATION_STATES)),
        ).fetchone()
        if row and int(row[0]):
            blocking["boundary_generations"] = int(row[0])
    if _table_exists(connection, "boundary_publication_corrections"):
        row = connection.execute(
            """
            SELECT count(*) FROM boundary_publication_corrections
            WHERE boundary_at >= %s AND boundary_at < %s
              AND state <> ALL(%s::text[])
            """,
            (season_start, season_end, list(TERMINAL_CORRECTION_STATES)),
        ).fetchone()
        if row and int(row[0]):
            blocking["boundary_corrections"] = int(row[0])
    return blocking


def retire_season_detail(
    connection: Any, season_id: str, *, max_rows: int = 500, apply: bool = False
) -> dict[str, Any]:
    """Delete finalized-season detail in bounded, restartable batches.

    Preview (``apply=False``) counts eligible rows without writing.
    Apply deletes daily logs, army facts, then exclusively-retired battle
    detail, updates progress, and marks the season ``retired`` once every
    counter reaches zero. Summaries and protected records are never
    deleted here.
    """
    season_id = _check_season_id(season_id)
    max_rows = _check_batch(max_rows, "retirement batch size")
    _lock_season(connection, season_id)
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
    if season_start is None or season_end is None:
        return {
            "season_id": season_id,
            "status": "blocked",
            "reason": "unknown_season_boundary",
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
        connection.execute(
            """
            DELETE FROM battle_payload_rows
            WHERE source_row_id = ANY(%s::bigint[])
              AND NOT EXISTS (
                  SELECT 1 FROM battle_evidence AS remaining
                  WHERE remaining.source_row_id = battle_payload_rows.source_row_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM battle_log_observation_rows AS remaining
                  WHERE remaining.source_row_id = battle_payload_rows.source_row_id
              )
            """,
            (source_rows,),
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
    for table in _MEASURE_TABLES:
        if not _table_exists(connection, table):
            continue
        size = connection.execute(
            "SELECT pg_total_relation_size(to_regclass(%s))", (table,)
        ).fetchone()[0]
        heap = connection.execute(
            "SELECT pg_relation_size(to_regclass(%s))", (table,)
        ).fetchone()[0]
        count = connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        tables[table] = {
            "allocated_bytes": int(size),
            "heap_bytes": int(heap),
            "index_toast_bytes": int(size) - int(heap),
            "rows": int(count),
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
