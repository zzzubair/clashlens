from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.errors import LockNotAvailable
from psycopg.types.json import Jsonb

from . import battle_day_repair
from .analytics import FRESHNESS_RULE_VERSION, SNAPSHOT_ORDERING_RULE_VERSION
from .army_decoder import DECODER_VERSION
from .boundary_manifest import (
    _moved_decode_ids,
    profiles_not_found,
    reset_trophies,
)
from .boundary_manifest import (
    freeze_boundary_manifest as _freeze_boundary_manifest,
)
from .catalog import CATALOG_VERSION
from .db import (
    ANALYTICS_RULE_VERSION,
    ARMY_ANALYTICS_RULE_VERSION,
    DEFAULT_PARSER_VERSION,
    DOMAIN_RULE_VERSION,
    PROCESSING_VERSION,
    Database,
    _text_value,
    ended_day_priority,
    lock_wait,
)
from .domain import SEASON_DURATION, is_season_boundary
from .domain_repair import boundary_held
from .past_reset_pacing import past_reset_build_waits, past_reset_correction_waits


def lock_boundary_publication(
    connection: Any, boundary_at: datetime, wait: str | None = None
) -> None:
    """Take a Reset's publication lock for the rest of the transaction,
    waiting at most ``wait`` for it when given.

    Lock order everywhere: a player-day lock or army battle locks
    (army_ingestion._upsert_army_decodes), then this lock for the Reset
    ending that day, then that Reset's settlement locks
    (reset_settlement.lock_resets), then its generation rows; several Resets
    are taken oldest first. Every path that locks or updates a generation row
    takes this lock first: a build that locked the row and then waited here,
    while a day-result rebuild held this lock and waited for the row,
    deadlocked. A rebuild locks its latest day result before this lock, so
    reconciliation_db.recalculate_ranked_day uses FOR NO KEY UPDATE: reference
    checks that publication paths run under this lock do not wait for it.
    """
    with lock_wait(connection, wait):
        connection.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"boundary-publication:{boundary_at.astimezone(UTC).isoformat()}",),
        )


def lock_boundary_publication_once_swept(connection: Any, boundary_at: datetime) -> bool:
    """Take a Reset's publication lock, shared until its sweep exists.

    No generation exists before the collector saves a Reset's sweep, so work
    for a Reset that has not happened yet shares the lock and runs in
    parallel. A generation is created only under the full lock, which waits
    for those jobs to commit. Once swept, member results take it as
    ``lock_boundary_members`` decides. Returns whether the Reset is swept.
    """

    def swept() -> bool:
        return connection.execute(
            "SELECT 1 FROM collector_reset_sweeps WHERE boundary_at = %s",
            (boundary_at,),
        ).fetchone() is not None

    if not swept():
        connection.execute(
            "SELECT pg_advisory_xact_lock_shared(hashtextextended(%s, 0))",
            (f"boundary-publication:{boundary_at.astimezone(UTC).isoformat()}",),
        )
        if not swept():
            return False
    lock_boundary_members(connection, boundary_at)
    return True


# The last members an open generation waits for take the full lock, so the
# result that leaves none waiting sees every other one committed and starts
# the build. Above the transactions that can write member results at once:
# 12 worker lanes, their maintenance and the late-battle sweep.
OPEN_GENERATION_TAIL = 100


def lock_boundary_members(
    connection: Any, boundary_at: datetime, wait: str | None = None
) -> bool:
    """Take a swept Reset's publication lock to write member results;
    returns whether it is shared.

    On 2026-10-06 every Reset reading, battle log and day result took the
    full lock in turn, about 13 a second, and the 12,795-member board was
    still waiting at 06:00. While the Reset's newest generation is open,
    with nothing frozen or corrected, and more than OPEN_GENERATION_TAIL
    members wait, the lock is shared: each holder writes only its own member
    rows, and the generation changes only under the full lock, which waits
    for them. A transaction keeps the shared mode it holds, because two
    upgrading it would deadlock; an open generation cannot change meanwhile.
    """
    key = f"boundary-publication:{boundary_at.astimezone(UTC).isoformat()}"
    held_full, held, open_generation, busy = connection.execute(
        """
        WITH generation AS (
            SELECT id, snapshot_state, army_state, correction_state,
                   snapshot_manifest_id, army_manifest_id
            FROM boundary_publication_generations
            WHERE boundary_at = %(boundary_at)s
              AND snapshot_state <> 'superseded'
              AND army_state <> 'superseded'
            ORDER BY generation DESC
            LIMIT 1
        ), own AS (
            SELECT lock.mode
            FROM pg_locks AS lock
            WHERE lock.locktype = 'advisory' AND lock.granted
              AND lock.pid = pg_backend_pid() AND lock.objsubid = 1
              AND lock.classid::bigint
                  = (hashtextextended(%(key)s, 0) >> 32) & 4294967295
              AND lock.objid::bigint
                  = hashtextextended(%(key)s, 0) & 4294967295
        ), open_generation AS (
            SELECT id FROM generation
            WHERE snapshot_state = 'pending' AND army_state = 'pending'
              AND correction_state = 'none'
              AND snapshot_manifest_id IS NULL AND army_manifest_id IS NULL
        )
        SELECT
            COALESCE((SELECT bool_or(mode = 'ExclusiveLock') FROM own), false),
            EXISTS (SELECT 1 FROM own),
            EXISTS (SELECT 1 FROM open_generation),
            EXISTS (
                SELECT 1 FROM open_generation
                WHERE (
                    SELECT count(*) FROM (
                        SELECT 1 FROM boundary_publication_generation_members
                        WHERE generation_id = open_generation.id
                          AND status = 'pending'
                        LIMIT %(tail)s + 1
                    ) AS waiting
                ) > %(tail)s
            )
        """,
        {"boundary_at": boundary_at, "key": key, "tail": OPEN_GENERATION_TAIL},
    ).fetchone()
    if held_full:
        return False
    # Only a lock shared before the sweep can meet no open generation; it
    # takes the full lock, as it always has.
    if held and open_generation:
        return True
    if not (open_generation and busy):
        lock_boundary_publication(connection, boundary_at, wait)
        return False
    with lock_wait(connection, wait):
        connection.execute(
            "SELECT pg_advisory_xact_lock_shared(hashtextextended(%s, 0))", (key,)
        )
    return True


def is_open_generation(row: Any) -> bool:
    """Whether a generation row's (snapshot_state, army_state,
    correction_state, snapshot_manifest_id, army_manifest_id) is open."""
    return row is not None and (
        _text_value(row[0]), _text_value(row[1]), _text_value(row[2]), row[3], row[4]
    ) == ("pending", "pending", "none", None, None)


def require_open_generation(row: Any, boundary_at: datetime) -> None:
    """Under the shared lock, refuse a generation that is not open.

    The lock is shared only while it is open, so this guards against a
    change made without the full lock: LockNotAvailable rolls the work back
    to retry, rather than writing to a frozen generation.
    """
    if not is_open_generation(row):
        raise LockNotAvailable(
            f"Reset {boundary_at.isoformat()} has no open generation for shared results"
        )


def _create_boundary_generation(
    database: Database,
    connection: Any,
    *,
    boundary_at: datetime,
    sweep_id: int,
    player_ids: list[int],
    generation: int,
    supersedes_id: int | None,
    pending_inputs: list[dict[str, Any]] | None = None,
    ordering_rule_version: str | None = None,
) -> tuple[int, int]:
    population_hash = _boundary_population_hash(player_ids)
    target_at = boundary_at + timedelta(
        minutes=10 if boundary_at.weekday() == 0 else 5
    )
    row = connection.execute(
        """
        INSERT INTO boundary_publication_generations (
            boundary_at, generation, sweep_id, ordering_rule_version,
            freshness_rule_version, expected_population_count,
            expected_population_hash, membership_rule_version,
            snapshot_rule_version, army_rule_version, target_rule, target_at,
            supersedes_id, source_generation_id,
            correction_state, affected_artifacts
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id, generation
        """,
        (
            boundary_at,
            generation,
            sweep_id,
            ordering_rule_version or SNAPSHOT_ORDERING_RULE_VERSION,
            FRESHNESS_RULE_VERSION,
            len(player_ids),
            population_hash,
            "active-members-v1",
            ANALYTICS_RULE_VERSION,
            ARMY_ANALYTICS_RULE_VERSION,
            "boundary-delay-v1",
            target_at,
            supersedes_id,
            supersedes_id,
            "active" if supersedes_id is not None else "none",
            ["snapshot", "army"] if supersedes_id is not None else [],
        ),
    ).fetchone()
    if row is None:
        row = connection.execute(
            """
            SELECT id, generation, expected_population_count,
                   expected_population_hash, sweep_id
            FROM boundary_publication_generations
            WHERE boundary_at = %s AND generation = %s
            FOR UPDATE
            """,
            (boundary_at, generation),
        ).fetchone()
    assert row is not None
    generation_id = int(row[0])
    if len(row) > 2 and (
        int(row[2]) != len(player_ids)
        or _text_value(row[3]) != population_hash
        or int(row[4]) != sweep_id
    ):
        raise ValueError(
            "boundary generation inputs conflict with captured membership"
        )
    if supersedes_id is None:
        connection.execute(
            """
            INSERT INTO boundary_publication_generation_members
                (generation_id, player_id)
            SELECT %s, unnest(%s::bigint[])
            ON CONFLICT DO NOTHING
            """,
            (generation_id, player_ids),
        )
    else:
        connection.execute(
            """
            INSERT INTO boundary_publication_generation_members (
                generation_id, player_id, ranked_day_version_id,
                ranked_day_input_hash, status, snapshot_status, army_status
            )
            SELECT %s, player_id, ranked_day_version_id,
                   ranked_day_input_hash, status, snapshot_status, army_status
            FROM boundary_publication_generation_members
            WHERE generation_id = %s
            ON CONFLICT DO NOTHING
            """,
            (generation_id, supersedes_id),
        )
    for pending in pending_inputs or []:
        pending_version = pending.get("ranked_day_version_id")
        pending_snapshot_status = _boundary_snapshot_status(
            connection,
            player_id=int(pending["player_id"]),
            ranked_day_version_id=int(pending_version),
            boundary_at=boundary_at,
        )
        pending_army_status = _boundary_army_status(database, 
            connection,
            player_id=int(pending["player_id"]),
            ranked_day_version_id=int(pending_version),
            snapshot_status=pending_snapshot_status,
        )
        connection.execute(
            """
            UPDATE boundary_publication_generation_members
            SET ranked_day_version_id = %s, ranked_day_input_hash = %s,
                status = 'terminal', snapshot_status = %s,
                army_status = %s, updated_at = clock_timestamp()
            WHERE generation_id = %s AND player_id = %s
            """,
            (
                pending_version,
                pending.get("input_hash"),
                pending_snapshot_status,
                pending_army_status,
                generation_id,
                pending.get("player_id"),
            ),
        )
    connection.execute(
        """
        UPDATE boundary_publication_generations
        SET membership_captured_at = COALESCE(membership_captured_at, clock_timestamp())
        WHERE id = %s
        """,
        (generation_id,),
    )
    return generation_id, int(row[1])


def _inherit_deferred_army_successor_snapshot(
    database, connection: Any, *, generation_id: int
) -> bool:
    target = connection.execute(
        """
        SELECT source_generation_id, snapshot_state, snapshot_manifest_id,
               affected_artifacts
        FROM boundary_publication_generations
        WHERE id = %s
        FOR UPDATE
        """,
        (generation_id,),
    ).fetchone()
    if target is None or _text_value(target[1]) != "pending":
        return False
    if [_text_value(value) for value in (target[3] or [])] != ["army"]:
        return False
    if target[0] is None or target[2] is not None:
        return False
    if (
        connection.execute(
            """
        SELECT 1
        FROM boundary_publication_corrections
        WHERE source_generation_id = %s
          AND state IN ('queued', 'pending_inputs', 'active')
        LIMIT 1
        """,
            (generation_id,),
        ).fetchone()
        is not None
    ):
        return False
    source = connection.execute(
        """
        SELECT snapshot_state, snapshot_id, snapshot_input_hash,
               snapshot_manifest_id, snapshot_analytics_publication_id,
               snapshot_coverage
        FROM boundary_publication_generations
        WHERE id = %s
        FOR UPDATE
        """,
        (target[0],),
    ).fetchone()
    if (
        source is None
        or _text_value(source[0]) not in {"published", "superseded"}
        or any(value is None for value in source[1:5])
    ):
        return False
    connection.execute(
        """
        UPDATE boundary_publication_generations
        SET snapshot_state = %s,
            snapshot_id = %s,
            snapshot_input_hash = %s,
            snapshot_manifest_id = %s,
            snapshot_analytics_publication_id = %s,
            snapshot_coverage = %s,
            updated_at = clock_timestamp()
        WHERE id = %s
          AND snapshot_state = 'pending'
          AND snapshot_manifest_id IS NULL
          AND affected_artifacts = ARRAY['army']::text[]
        """,
        ("published", *source[1:5], Jsonb(source[5]), generation_id),
    )
    return True


def _supersede_generation(
    database: Database,
    connection: Any,
    *,
    boundary_at: datetime,
    sweep_id: int,
    generation_id: int,
    generation: int,
    pending_inputs: list[dict[str, Any]] | None = None,
) -> tuple[int, int]:
    """Replace ``generation_id`` with a generation rebuilding both artifacts
    under the current rules, started as the Reset's active correction.
    Corrections still waiting on the replaced generation wait on its
    replacement."""
    connection.execute(
        """
        UPDATE boundary_publication_generations
        SET snapshot_state = 'superseded', army_state = 'superseded',
            correction_state = 'finalized', updated_at = clock_timestamp()
        WHERE id = %s
        """,
        (generation_id,),
    )
    connection.execute(
        """
        UPDATE boundary_publication_corrections
        SET state = 'finalized', finalized_at = clock_timestamp()
        WHERE generation_id = %s AND state = 'active'
        """,
        (generation_id,),
    )
    frozen_members = connection.execute(
        """
        SELECT player_id
        FROM boundary_publication_generation_members
        WHERE generation_id = %s
        ORDER BY player_id
        """,
        (generation_id,),
    ).fetchall()
    new_id, new_generation = _create_boundary_generation(
        database,
        connection,
        boundary_at=boundary_at,
        sweep_id=sweep_id,
        player_ids=[int(row[0]) for row in frozen_members],
        generation=generation + 1,
        supersedes_id=generation_id,
        pending_inputs=pending_inputs,
    )
    connection.execute(
        """
        UPDATE boundary_publication_corrections
        SET source_generation_id = %s
        WHERE source_generation_id = %s AND state IN ('queued', 'pending_inputs')
        """,
        (new_id, generation_id),
    )
    connection.execute(
        """
        INSERT INTO boundary_publication_corrections
            (boundary_at, source_generation_id, generation_id,
             affected_artifacts, state, started_at)
        VALUES (%s, %s, %s, ARRAY['snapshot', 'army'], 'active', clock_timestamp())
        """,
        (boundary_at, generation_id, new_id),
    )
    return new_id, new_generation


def _try_enqueue_boundary_artifacts(
    database, connection: Any, *, boundary_at: datetime, generation_id: int
) -> None:
    if boundary_held(connection, boundary_at):
        return
    _inherit_deferred_army_successor_snapshot(database, 
        connection, generation_id=generation_id
    )
    generation = connection.execute(
        """
        SELECT id, generation, sweep_id, snapshot_state, army_state,
               expected_population_count, affected_artifacts, target_at, target_rule,
               ordering_rule_version
        FROM boundary_publication_generations
        WHERE id = %s
        FOR UPDATE
        """,
        (generation_id,),
    ).fetchone()
    if generation is None:
        return
    generation_number = int(generation[1])
    # A past Reset starts no build in the quiet window.
    if past_reset_build_waits(connection, boundary_at):
        return
    sweep_id = int(generation[2]) if generation[2] is not None else None
    if sweep_id is None:
        return

    classifications = connection.execute(
        """
        SELECT count(*) AS member_count,
               count(*) FILTER (WHERE snapshot_status = 'pending') AS snapshot_pending,
               count(*) FILTER (WHERE army_status = 'pending') AS army_pending,
               count(*) FILTER (WHERE snapshot_status NOT IN
                   ('complete','partial','failed','missing','unavailable','inconsistent','malformed','pending')) AS bad_snapshot,
               count(*) FILTER (WHERE army_status NOT IN
                   ('complete','partial','failed','missing','unavailable','inconsistent','malformed','pending')) AS bad_army
        FROM boundary_publication_generation_members
        WHERE generation_id = %s
        """,
        (generation_id,),
    ).fetchone()
    assert classifications is not None
    member_count, snapshot_pending, army_pending, bad_snapshot, bad_army = (
        int(value) for value in classifications
    )
    if member_count != int(generation[5]) or bad_snapshot or bad_army:
        return
    boundary_text = boundary_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if _text_value(generation[8]) != "boundary-delay-v1":
        return
    target = generation[7]
    now = connection.execute("SELECT clock_timestamp()").fetchone()[0]
    if now < target:
        return
    affected_artifacts = [_text_value(value) for value in (generation[6] or [])]
    if snapshot_pending == 0 and _text_value(generation[3]) == "pending":
        connection.execute(
            "UPDATE boundary_publication_generations SET snapshot_state = 'ready', updated_at = clock_timestamp() WHERE id = %s AND snapshot_state = 'pending'",
            (generation_id,),
        )
    if army_pending == 0 and _text_value(generation[4]) == "pending":
        connection.execute(
            "UPDATE boundary_publication_generations SET army_state = 'ready', updated_at = clock_timestamp() WHERE id = %s AND army_state = 'pending'",
            (generation_id,),
        )
    snapshot = connection.execute(
        "SELECT snapshot_state, snapshot_manifest_id FROM boundary_publication_generations WHERE id = %s FOR UPDATE",
        (generation_id,),
    ).fetchone()
    if (
        snapshot_pending == 0
        and snapshot is not None
        and _text_value(snapshot[0]) == "ready"
        and (not affected_artifacts or "snapshot" in affected_artifacts)
    ):
        # A board built under an older ordering rule, such as an army-only
        # replacement of one that gains a board rebuild, freezes its inputs
        # under the current rule in a replacement; a generation's rule
        # cannot change once its membership is captured.
        if _text_value(generation[9]) != SNAPSHOT_ORDERING_RULE_VERSION:
            new_id, _ = _supersede_generation(
                database,
                connection,
                boundary_at=boundary_at,
                sweep_id=sweep_id,
                generation_id=generation_id,
                generation=generation_number,
            )
            _try_enqueue_boundary_artifacts(
                database, connection, boundary_at=boundary_at, generation_id=new_id
            )
            return
        manifest = _freeze_boundary_manifest(database, 
            connection, generation_id=generation_id, artifact_kind="snapshot"
        )
        assert manifest is not None
        connection.execute(
            """
            INSERT INTO python_processing_jobs_worker (
                observation_id, work_type, deduplication_key, input_json,
                state, due_at, parser_version, processing_version,
                domain_rule_version, analytics_rule_version, priority
            ) VALUES (NULL, 'build_snapshot', %s, %s, 'pending', %s, %s, %s, %s, %s, %s)
            ON CONFLICT (deduplication_key) DO NOTHING
            """,
            (
                f"build_snapshot:boundary:{boundary_text}:gen:{generation_number}:manifest:{manifest[1]}",
                Jsonb(
                    {
                        "boundary_at": boundary_text,
                        "generation": generation_number,
                        "manifest_id": manifest[0],
                        "manifest_digest": manifest[1],
                    }
                ),
                target,
                DEFAULT_PARSER_VERSION,
                PROCESSING_VERSION,
                DOMAIN_RULE_VERSION,
                ANALYTICS_RULE_VERSION,
                # The board goes before the slower army build.
                ended_day_priority(boundary_at - timedelta(days=1)),
            ),
        )
    army = connection.execute(
        "SELECT army_state, army_manifest_id FROM boundary_publication_generations WHERE id = %s FOR UPDATE",
        (generation_id,),
    ).fetchone()
    if (
        army_pending == 0
        and army is not None
        and _text_value(army[0]) == "ready"
        and (not affected_artifacts or "army" in affected_artifacts)
    ):
        manifest = _freeze_boundary_manifest(database, 
            connection, generation_id=generation_id, artifact_kind="army"
        )
        assert manifest is not None
        connection.execute(
            """
            INSERT INTO python_processing_jobs_worker (
                observation_id, work_type, deduplication_key, input_json,
                state, due_at, parser_version, processing_version,
                domain_rule_version, analytics_rule_version
            ) VALUES (NULL, 'build_army_analytics', %s, %s, 'pending', clock_timestamp(), %s, %s, %s, %s)
            ON CONFLICT (deduplication_key) DO NOTHING
            """,
            (
                f"build_army_analytics:boundary:{boundary_text}:gen:{generation_number}:manifest:{manifest[1]}",
                Jsonb(
                    {
                        "boundary_at": boundary_text,
                        "generation": generation_number,
                        "manifest_id": manifest[0],
                        "manifest_digest": manifest[1],
                    }
                ),
                DEFAULT_PARSER_VERSION,
                PROCESSING_VERSION,
                DOMAIN_RULE_VERSION,
                ARMY_ANALYTICS_RULE_VERSION,
            ),
        )


def _boundary_snapshot_status(
    connection: Any,
    *,
    player_id: int,
    ranked_day_version_id: int,
    boundary_at: datetime | None = None,
) -> str:
    ranked = connection.execute(
        "SELECT state FROM ranked_day_versions WHERE id = %s AND player_id = %s",
        (ranked_day_version_id, player_id),
    ).fetchone()
    status = {
        "Complete": "complete",
        "Partial": "partial",
        "Inconsistent": "inconsistent",
        "Malformed": "malformed",
    }.get(_text_value(ranked[0]) if ranked else "", "pending")
    if status != "complete":
        return status
    profile = connection.execute(
        """
        SELECT 1
        FROM player_profile_versions AS profile
        LEFT JOIN player_profile_effects AS effect
          ON effect.profile_version_id = profile.id
        WHERE profile.player_id = %s
          AND profile.source_contract_state = 'accepted'
          AND (%s::timestamptz IS NULL OR COALESCE(effect.observed_at, profile.observed_at) <= %s)
        ORDER BY COALESCE(effect.observed_at, profile.observed_at) DESC,
                 COALESCE(effect.id, profile.id) DESC
        LIMIT 1
        """,
        (player_id, boundary_at, boundary_at),
    ).fetchone()
    return "complete" if profile is not None else "missing"


def _army_decode_selection(
    connection: Any, battles: Any
) -> tuple[list[int], list[int], dict[tuple[int, str], int]]:
    """A daily log's listed battles, the decodes its army inputs freeze and
    the listed sides 0057 moved.

    The decodes are every listed battle's active ones, both sides, plus each
    moved side's on the battle it is on now.
    """
    sides = [
        (int(event["battle_id"]), event.get("lens"))
        for event in (battles if isinstance(battles, list) else [])
        if isinstance(event, dict) and str(event.get("battle_id", "")).isdigit()
    ]
    battle_ids = [battle_id for battle_id, _lens in sides]
    if not battle_ids:
        return battle_ids, [], {}
    moved = _moved_sides(connection, sides)
    decode_ids = sorted(
        {
            *(
                int(row[0])
                for row in connection.execute(
                    """
                    SELECT id FROM battle_army_decodes
                    WHERE battle_id = ANY(%s::bigint[]) AND is_active
                      AND decoder_version = %s AND catalog_version = %s
                    """,
                    (battle_ids, DECODER_VERSION, CATALOG_VERSION),
                ).fetchall()
            ),
            *_moved_decode_ids(connection, moved),
        }
    )
    return battle_ids, decode_ids, moved


def _moved_sides(
    connection: Any, sides: list[tuple[int, Any]]
) -> dict[tuple[int, str], int]:
    """Map each listed (battle, lens) 0057 moved to the battle it is on now."""
    listed = sorted({battle_id for battle_id, _lens in sides})
    if not listed:
        return {}
    moved, _ = battle_day_repair.merged_battles(
        connection,
        listed,
        [
            int(row[0])
            for row in connection.execute(
                "SELECT evidence_id FROM battle_perspectives"
                " WHERE battle_id = ANY(%s::bigint[])",
                (listed,),
            ).fetchall()
        ],
    )
    listed_sides = set(sides)
    return {side: to_id for side, to_id in moved.items() if side in listed_sides}


def _boundary_army_status(
    database: Database,
    connection: Any,
    *,
    player_id: int,
    ranked_day_version_id: int,
    snapshot_status: str,
) -> str:
    if snapshot_status in {"pending", "unavailable", "missing", "failed"}:
        return (
            "unavailable"
            if snapshot_status in {"unavailable", "missing", "failed"}
            else "pending"
        )
    daily_log = connection.execute(
        """
        SELECT battles, state, coverage
        FROM api_player_daily_logs
        WHERE player_id = %s AND ranked_day_version_id = %s
        ORDER BY version DESC LIMIT 1
        """,
        (player_id, ranked_day_version_id),
    ).fetchone()
    # Coordinated publication requires daily-log evidence before army
    # readiness; direct legacy reconciliation retains its old fallback.
    if daily_log is None:
        return (
            "pending"
            if getattr(database, "_supports_coordinator_contract", False)
            else snapshot_status
        )
    daily_state = _text_value(daily_log[1])
    daily_coverage = _text_value(daily_log[2])
    if daily_state == "Partial":
        return "partial"
    if daily_state != "Complete" or daily_coverage != "complete":
        return "pending"
    sides = [
        (int(event["battle_id"]), event.get("lens"))
        for event in (daily_log[0] if isinstance(daily_log[0], list) else [])
        if isinstance(event, dict) and str(event.get("battle_id", "")).isdigit()
    ]
    if not sides:
        return "complete"
    moved = _moved_sides(connection, sides)
    battle_ids = [moved.get(side, side[0]) for side in sides]
    decoded = connection.execute(
        """
        SELECT count(DISTINCT battle_id)
        FROM battle_army_decodes
        WHERE battle_id = ANY(%s::bigint[]) AND is_active
          AND decoder_version = %s AND catalog_version = %s
        """,
        (battle_ids, DECODER_VERSION, CATALOG_VERSION),
    ).fetchone()
    return (
        snapshot_status
        if decoded is not None and int(decoded[0]) == len(set(battle_ids))
        else "pending"
    )


def _record_boundary_generation(
    database: Database,
    connection: Any,
    *,
    boundary_at: datetime,
    player_id: int,
    ranked_day_version_id: int,
    ranked_day_input_hash: str,
    reset_lock_wait: str | None = None,
) -> bool:
    """Record one member result and enqueue each ready artifact once.

    The collector's sweep membership is the frozen population authority.
    A generation is reused until either artifact has frozen; after that a
    changed member starts one superseding generation and later corrections
    coalesce into it.
    """
    boundary_at = boundary_at.astimezone(UTC)
    # A sweep is saved with its members in one transaction and never changes,
    # so a Reset that has not happened yet is skipped without its lock.
    sweep = connection.execute(
        "SELECT id, member_ids FROM collector_reset_sweeps WHERE boundary_at = %s",
        (boundary_at,),
    ).fetchone()
    if sweep is None:
        return False
    shared = lock_boundary_members(connection, boundary_at, reset_lock_wait)
    sweep_id = int(sweep[0])
    member_ids = [int(value) for value in (sweep[1] or [])]
    if player_id not in member_ids:
        return True
    current = connection.execute(
        """
        SELECT id, generation, snapshot_state, army_state,
               correction_state, snapshot_manifest_id, army_manifest_id,
               affected_artifacts
        FROM boundary_publication_generations
        WHERE boundary_at = %s
          AND snapshot_state <> 'superseded'
          AND army_state <> 'superseded'
        ORDER BY generation DESC
        LIMIT 1
        """
        + ("" if shared else " FOR UPDATE"),
        (boundary_at,),
    ).fetchone()
    if shared:
        require_open_generation(current and current[2:7], boundary_at)
    if current is None:
        generation_id, generation = _create_boundary_generation(database, 
            connection,
            boundary_at=boundary_at,
            sweep_id=sweep_id,
            player_ids=member_ids,
            generation=1,
            supersedes_id=None,
        )
    else:
        generation_id, generation = int(current[0]), int(current[1])
        prior_member = connection.execute(
            """
            SELECT ranked_day_version_id, ranked_day_input_hash
            FROM boundary_publication_generation_members
            WHERE generation_id = %s AND player_id = %s
            FOR UPDATE
            """,
            (generation_id, player_id),
        ).fetchone()
        # A generation's expected membership is immutable. A player that
        # appears in the sweep after generation capture is not inserted,
        # including after a frozen publication.
        if prior_member is None:
            return True
        changed = (
            prior_member[0] is None
            or int(prior_member[0]) != int(ranked_day_version_id)
            or _text_value(prior_member[1]) != ranked_day_input_hash
        )
        frozen_artifacts = [
            kind
            for kind, manifest_id in (
                ("snapshot", current[5]),
                ("army", current[6]),
            )
            if manifest_id is not None
        ]
        correction_active = _text_value(current[4]) == "active"
        if correction_active and current[7] and not (changed and frozen_artifacts):
            affected = {_text_value(value) for value in current[7]}
            frozen_artifacts = [
                kind for kind in frozen_artifacts if kind in affected
            ]
        elif changed and frozen_artifacts:
            # A ranked-day source feeds both artifacts. Once either
            # manifest has frozen, reprocess both instead of inheriting an
            # army identity built from the previous source version.
            frozen_artifacts = ["snapshot", "army"]
        queued = (
            connection.execute(
                """
                SELECT id
                FROM boundary_publication_corrections
                WHERE boundary_at = %s AND source_generation_id = %s
                  AND state IN ('queued', 'pending_inputs')
                ORDER BY id DESC
                LIMIT 1
                FOR UPDATE
                """,
                (boundary_at, generation_id),
            ).fetchone()
            if changed
            else None
        )
        fully_published = (
            _text_value(current[2]) == "published"
            and _text_value(current[3]) == "published"
            # A correction already waiting, a repair campaign holding this
            # Reset, or a past Reset not yet due to rebuild keeps the change
            # queued, so waiting inputs start together.
            and not (
                changed
                and (
                    queued is not None
                    or boundary_held(connection, boundary_at)
                    or past_reset_correction_waits(connection, boundary_at)
                )
            )
        )
        active_target_correction = connection.execute(
            """
            SELECT id
            FROM boundary_publication_corrections
            WHERE generation_id = %s AND state = 'active'
            FOR UPDATE
            """,
            (generation_id,),
        ).fetchone()
        if (
            changed
            and active_target_correction is not None
            and not frozen_artifacts
        ):
            snapshot_status = _boundary_snapshot_status(
                connection,
                player_id=player_id,
                ranked_day_version_id=ranked_day_version_id,
                boundary_at=boundary_at,
            )
            army_status = _boundary_army_status(database, 
                connection,
                player_id=player_id,
                ranked_day_version_id=ranked_day_version_id,
                snapshot_status=snapshot_status,
            )
            connection.execute(
                """
                UPDATE boundary_publication_generation_members
                SET ranked_day_version_id = %s, ranked_day_input_hash = %s,
                    status = %s, snapshot_status = %s, army_status = %s,
                    updated_at = clock_timestamp()
                WHERE generation_id = %s AND player_id = %s
                """,
                (
                    ranked_day_version_id,
                    ranked_day_input_hash,
                    "terminal" if snapshot_status != "pending" else "pending",
                    snapshot_status,
                    army_status,
                    generation_id,
                    player_id,
                ),
            )
            connection.execute(
                """
                UPDATE boundary_publication_generations
                SET affected_artifacts = ARRAY(
                        SELECT DISTINCT unnest(affected_artifacts || ARRAY['snapshot']::text[])
                    ),
                    updated_at = clock_timestamp()
                WHERE id = %s
                """,
                (generation_id,),
            )
            connection.execute(
                """
                UPDATE boundary_publication_corrections
                SET affected_artifacts = ARRAY(
                        SELECT DISTINCT unnest(affected_artifacts || ARRAY['snapshot']::text[])
                    )
                WHERE id = %s
                """,
                (active_target_correction[0],),
            )
            _try_enqueue_boundary_artifacts(database, 
                connection, boundary_at=boundary_at, generation_id=generation_id
            )
            return True
        if (
            changed
            and frozen_artifacts
            and (correction_active or not fully_published)
        ):
            # Keep the changed input only in the correction queue. The
            # captured generation member remains immutable; the queued
            # successor applies this input before it seals inheritance.
            pending_input = {
                "player_id": player_id,
                "ranked_day_version_id": ranked_day_version_id,
                "input_hash": ranked_day_input_hash,
            }
            correction_artifacts = frozen_artifacts
            if queued is None:
                connection.execute(
                    """
                    INSERT INTO boundary_publication_corrections
                        (boundary_at, source_generation_id, affected_artifacts, pending_inputs)
                    VALUES (%s, %s, %s, %s)
                    """,
                    (
                        boundary_at,
                        generation_id,
                        correction_artifacts,
                        Jsonb([pending_input]),
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE boundary_publication_corrections
                    SET affected_artifacts = ARRAY(
                            SELECT DISTINCT unnest(affected_artifacts || %s::text[])
                        ),
                        pending_inputs = pending_inputs || %s::jsonb
                    WHERE id = %s
                    """,
                    (correction_artifacts, Jsonb([pending_input]), queued[0]),
                )
            return True
        if (
            changed
            and not correction_active
            and frozen_artifacts
            and fully_published
        ):
            generation_id, generation = _supersede_generation(
                database,
                connection,
                boundary_at=boundary_at,
                sweep_id=sweep_id,
                generation_id=generation_id,
                generation=generation,
                pending_inputs=[
                    {
                        "player_id": player_id,
                        "ranked_day_version_id": ranked_day_version_id,
                        "input_hash": ranked_day_input_hash,
                    }
                ],
            )
    snapshot_status = _boundary_snapshot_status(
        connection,
        player_id=player_id,
        ranked_day_version_id=ranked_day_version_id,
        boundary_at=boundary_at,
    )
    army_status = _boundary_army_status(database, 
        connection,
        player_id=player_id,
        ranked_day_version_id=ranked_day_version_id,
        snapshot_status=snapshot_status,
    )
    member_status = "terminal" if snapshot_status != "pending" else "pending"
    updated_member = connection.execute(
        """
        UPDATE boundary_publication_generation_members
        SET ranked_day_version_id = %s, ranked_day_input_hash = %s,
            status = %s, snapshot_status = %s, army_status = %s,
            updated_at = clock_timestamp()
        WHERE generation_id = %s AND player_id = %s
        """,
        (
            ranked_day_version_id,
            ranked_day_input_hash,
            member_status,
            snapshot_status,
            army_status,
            generation_id,
            player_id,
        ),
    )
    if updated_member.rowcount == 0:
        connection.execute(
            """
            INSERT INTO boundary_publication_generation_members (
                generation_id, player_id, ranked_day_version_id,
                ranked_day_input_hash, status, snapshot_status, army_status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                generation_id,
                player_id,
                ranked_day_version_id,
                ranked_day_input_hash,
                member_status,
                snapshot_status,
                army_status,
            ),
        )
    if not shared:
        _try_enqueue_boundary_artifacts(database, 
            connection, boundary_at=boundary_at, generation_id=generation_id
        )
    return True


def queue_board_rebuilds(
    database: Database, season_id: str, *, queue: bool
) -> dict[str, Any]:
    """Find, and with ``queue`` rebuild, each of the Season's Reset boards
    whose frozen input still ranks a reading taken before the player's
    profile answered "player not found", whose saved entries differ from
    the trophies at the Reset ``reset_trophies`` now gives, or its mark, or
    that an older ordering rule ordered. On 5
    to 7 October 2026 that was 24 players on Day 1 and 34 on Day 2, two of
    them first and second on Day 2, and 290 Day 2 entries missing battles
    after their readings; on 8 October, 3 Day 3 entries marked proven but
    missing attacks before them.

    Each board gets one queued correction of both its leaderboard and army
    records, started as any other: once its build is published, outside a
    repair campaign and past-Reset pacing. Each Reset's newest board is read
    under its publication lock, so a correction is never queued against a
    board a worker has already replaced. A rebuilt board ranks no such
    reading and saves the battles after each one, so a later run lists
    nothing for it; one still queued is listed again and not queued twice.
    Resets of other Seasons are never read.
    """
    season_start = datetime.fromtimestamp(int(season_id), UTC)
    if not is_season_boundary(season_start):
        raise ValueError(f"{season_id} is not a Season's start")
    boards: list[dict[str, Any]] = []
    with database.pool.connection() as connection:
        with connection.transaction():
            resets = connection.execute(
                """
                SELECT DISTINCT boundary_at
                FROM boundary_publication_generations
                WHERE boundary_at > %s AND boundary_at <= %s
                ORDER BY boundary_at
                """,
                (season_start, season_start + SEASON_DURATION),
            ).fetchall()
        for (boundary_at,) in resets:
            with connection.transaction():
                lock_boundary_publication(connection, boundary_at)
                # An army-only replacement not yet holding its board ranks
                # the board it inherits.
                current = connection.execute(
                    """
                    SELECT generation.id, generation.generation,
                           COALESCE(generation.snapshot_manifest_id,
                                    source.snapshot_manifest_id),
                           COALESCE(generation.snapshot_id, source.snapshot_id)
                    FROM boundary_publication_generations AS generation
                    LEFT JOIN boundary_publication_generations AS source
                      ON source.id = generation.source_generation_id
                     AND generation.snapshot_state = 'pending'
                     AND generation.affected_artifacts = ARRAY['army']::text[]
                    WHERE generation.boundary_at = %s
                      AND generation.snapshot_state <> 'superseded'
                      AND generation.army_state <> 'superseded'
                    ORDER BY generation.generation DESC
                    LIMIT 1
                    """,
                    (boundary_at,),
                ).fetchone()
                # A board not frozen yet is built under the current rule.
                if current is None or current[2] is None:
                    continue
                generation_id, generation, manifest_id, snapshot_id = current
                rows = connection.execute(
                    """
                    SELECT player_id,
                           input_identity->'profile_snapshot'->>'observed_at',
                           ranked_day_version_id,
                           input_identity->'profile_snapshot'->>'observation_id',
                           (input_identity->'profile_snapshot'->>'trophies')::integer
                    FROM boundary_publication_manifest_entries(%s)
                    WHERE input_identity->>'snapshot_quality' = 'eligible'
                    """,
                    (manifest_id,),
                ).fetchall()
                readings = {
                    int(row[0]): datetime.fromisoformat(str(row[1])) for row in rows
                }
                not_found = profiles_not_found(connection, boundary_at, readings)
                # Saved entries whose value or mark the current rule changes.
                # A board not built yet saves them already.
                at_reset = reset_trophies(
                    connection,
                    boundary_at,
                    {
                        int(row[0]): (
                            int(row[2]), int(row[3]), readings[int(row[0])],
                            int(row[4]),
                        )
                        for row in rows
                        if row[2] is not None
                    },
                )
                expected = {
                    int(row[0]): at_reset.get(int(row[0]), (int(row[4]), False))
                    for row in rows
                }
                entries = connection.execute(
                    """
                    SELECT player_id, trophies, confidence
                    FROM leaderboard_snapshot_entries
                    WHERE snapshot_id = %s
                    """,
                    (snapshot_id,),
                ).fetchall()
                late_battles = sum(
                    (trophies, _text_value(confidence) == "confirmed")
                    != expected.get(int(player_id))
                    for player_id, trophies, confidence in entries
                )
                # A board ordered by an older rule, such as equal trophies
                # by tag hash alone before the shared tie order.
                rule = connection.execute(
                    "SELECT ordering_rule_version FROM leaderboard_snapshots WHERE id = %s",
                    (snapshot_id,),
                ).fetchone()
                reordered = (
                    rule is not None
                    and _text_value(rule[0]) != SNAPSHOT_ORDERING_RULE_VERSION
                )
                if not not_found and not late_battles and not reordered:
                    continue
                queued = connection.execute(
                    """
                    SELECT id FROM boundary_publication_corrections
                    WHERE boundary_at = %s AND source_generation_id = %s
                      AND state IN ('queued', 'pending_inputs')
                    ORDER BY id DESC LIMIT 1
                    FOR UPDATE
                    """,
                    (boundary_at, generation_id),
                ).fetchone()
                if queue and queued is not None:
                    connection.execute(
                        """
                        UPDATE boundary_publication_corrections
                        SET affected_artifacts = ARRAY(
                                SELECT DISTINCT unnest(
                                    affected_artifacts || ARRAY['snapshot', 'army'])
                            )
                        WHERE id = %s
                        """,
                        (queued[0],),
                    )
                elif queue:
                    connection.execute(
                        """
                        INSERT INTO boundary_publication_corrections
                            (boundary_at, source_generation_id,
                             affected_artifacts, pending_inputs)
                        VALUES (%s, %s, ARRAY['snapshot', 'army'], '[]'::jsonb)
                        """,
                        (boundary_at, generation_id),
                    )
                boards.append(
                    {
                        "boundary_at": boundary_at.astimezone(UTC).isoformat(),
                        "generation": int(generation),
                        "profile_not_found": len(not_found),
                        "late_battles": late_battles,
                        "reordered": reordered,
                        "correction": (
                            "already_queued" if queued is not None
                            else "queued" if queue
                            else "not_queued"
                        ),
                    }
                )
    return {"season_id": season_id, "queue": queue, "boards": boards}


def _boundary_population_hash(player_ids: list[int]) -> str:
    return hashlib.sha256(
        json.dumps(sorted(player_ids), separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _create_boundary_artifact_identity(
    connection: Any,
    *,
    generation_id: int,
    artifact_kind: str,
    manifest_id: int,
    input_hash: str,
    source_identity: dict[str, Any],
) -> int:
    row = connection.execute(
        """
        INSERT INTO boundary_publication_artifact_identities
            (generation_id, artifact_kind, manifest_id, input_hash, source_identity)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (generation_id, artifact_kind, manifest_id) DO NOTHING
        RETURNING id
        """,
        (
            generation_id,
            artifact_kind,
            manifest_id,
            input_hash,
            Jsonb(source_identity),
        ),
    ).fetchone()
    if row is None:
        row = connection.execute(
            """
            SELECT id, input_hash, source_identity
            FROM boundary_publication_artifact_identities
            WHERE generation_id = %s AND artifact_kind = %s AND manifest_id = %s
            FOR UPDATE
            """,
            (generation_id, artifact_kind, manifest_id),
        ).fetchone()
        if (
            row is None
            or _text_value(row[1]) != input_hash
            or dict(row[2]) != source_identity
        ):
            raise ValueError("boundary publication artifact identity conflict")
    return int(row[0])


