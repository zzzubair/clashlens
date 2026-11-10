from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg.errors import LockNotAvailable
from psycopg.types.json import Jsonb

from . import board_deadline, boundary_members
from .analytics import (
    SNAPSHOT_ORDERING_RULE_VERSION,
    follow_official_ties,
    season_end_placements,
)
from .army_decoder import CURRENT_DECODES, DECODER_VERSION
from .boundary_manifest import (
    freeze_boundary_manifest as _freeze_boundary_manifest,
)
from .boundary_manifest import (
    profiles_not_found,
    reset_trophies,
    unplayed_profiles,
)
from .db import (
    ANALYTICS_RULE_VERSION,
    ARMY_ANALYTICS_RULE_VERSION,
    DEFAULT_PARSER_VERSION,
    DOMAIN_RULE_VERSION,
    PROCESSING_VERSION,
    PYTHON_BACKFILL_PRIORITY,
    PYTHON_LIVE_PRIORITY,
    RESET_LOCK_WAIT,
    Database,
    ended_day_priority,
    lock_wait,
)
from .db_rows import text_value
from .domain import RANKED_DAY_DURATION, SEASON_DURATION, is_season_boundary
from .past_reset_pacing import (
    ARRIVAL,
    OPERATOR_CORRECTION,
    REPAIR_CORRECTION,
    correction_waits,
    operator_correction_waits,
    operator_generation,
    past_reset_build_waits,
)
from .publication_states import MEMBER_STATUSES_SQL, ArtifactState, boundary_held


def lock_boundary_publication(
    connection: Any, boundary_at: datetime, wait: str | None = None
) -> None:
    """Take a Reset's publication lock for the rest of the transaction,
    waiting at most ``wait`` for it when given.

    Lock order everywhere: a player-day lock or army battle locks
    (army_ingestion._upsert_army_decodes), then this lock for the Reset
    ending that day, then its generation rows; several Resets
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
# A caller limiting its wait waits this long for it: on 14 Oct 2026 the last
# 95 day results gave up after 250 ms and came back 37 to 99 s later.
TAIL_LOCK_WAIT = "5s"


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
        if open_generation and wait is not None:
            wait = TAIL_LOCK_WAIT
        lock_boundary_publication(connection, boundary_at, wait)
        return False
    with lock_wait(connection, wait):
        connection.execute(
            "SELECT pg_advisory_xact_lock_shared(hashtextextended(%s, 0))", (key,)
        )
    return True


def require_open_generation(row: Any, boundary_at: datetime) -> None:
    """Under the shared lock, refuse a generation row's (snapshot_state,
    army_state, correction_state, snapshot_manifest_id, army_manifest_id)
    that is not open.

    The lock is shared only while it is open, so this guards against a
    change made without the full lock: LockNotAvailable rolls the work back
    to retry, rather than writing to a frozen generation.
    """
    if row is None or (
        text_value(row[0]), text_value(row[1]), text_value(row[2]), row[3], row[4]
    ) != ("pending", "pending", "none", None, None):
        raise LockNotAvailable(
            f"Reset {boundary_at.isoformat()} has no open generation for shared results"
        )


def _inherit_deferred_army_successor_snapshot(
    connection: Any, *, generation_id: int
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
    if target is None or text_value(target[1]) != "pending":
        return False
    if [text_value(value) for value in (target[3] or [])] != ["army"]:
        return False
    if target[0] is None or target[2] is not None:
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
        or text_value(source[0]) not in {"published", "superseded"}
        or any(value is None for value in source[1:5])
    ):
        return False
    boundary_members.inherit_artifact(connection, generation_id, int(target[0]),
                                      "snapshot", published=True, touch=True)
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
    under the current rules and the board's members now
    (``boundary_members``), started as the Reset's active correction.
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
    new_id, new_generation = boundary_members.create_generation(
        database,
        connection,
        boundary_at=boundary_at,
        sweep_id=sweep_id,
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
    database, connection: Any, *, boundary_at: datetime, generation_id: int, army: bool = False
) -> None:
    # A repair holds no Reset's first board and army results past 06:00.
    if boundary_held(connection, boundary_at) and not board_deadline.overdue(connection, boundary_at):
        return
    _inherit_deferred_army_successor_snapshot(connection, generation_id=generation_id)
    generation = connection.execute(
        """
        SELECT generation, sweep_id, snapshot_state, army_state,
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
    generation_number = int(generation[0])
    # A past Reset starts no build in the quiet window.
    if past_reset_build_waits(connection, boundary_at):
        return
    sweep_id = int(generation[1]) if generation[1] is not None else None
    if sweep_id is None:
        return

    classifications = connection.execute(
        f"""
        SELECT count(*) AS member_count,
               count(*) FILTER (WHERE snapshot_status = 'pending') AS snapshot_pending,
               count(*) FILTER (WHERE army_status = 'pending') AS army_pending,
               count(*) FILTER (WHERE snapshot_status NOT IN ({MEMBER_STATUSES_SQL})) AS bad_snapshot,
               count(*) FILTER (WHERE army_status NOT IN ({MEMBER_STATUSES_SQL})) AS bad_army
        FROM boundary_publication_generation_members
        WHERE generation_id = %s
        """,
        (generation_id,),
    ).fetchone()
    assert classifications is not None
    member_count, snapshot_pending, army_pending, bad_snapshot, bad_army = (
        int(value) for value in classifications
    )
    if member_count != int(generation[4]) or bad_snapshot or bad_army:
        return
    boundary_text = boundary_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if text_value(generation[7]) != "boundary-delay-v1":
        return
    target = generation[6]
    now = connection.execute("SELECT clock_timestamp()").fetchone()[0]
    if now < target:
        return
    affected_artifacts = [text_value(value) for value in (generation[5] or [])]
    # Settled members move each artifact PENDING to READY.
    for column, pending, state in (("snapshot_state", snapshot_pending, generation[2]),
                                   ("army_state", army_pending, generation[3])):
        if pending == 0 and text_value(state) == ArtifactState.PENDING:
            connection.execute(
                f"UPDATE boundary_publication_generations SET {column} = %s, updated_at = clock_timestamp() WHERE id = %s AND {column} = %s",
                (ArtifactState.READY.value, generation_id, ArtifactState.PENDING.value),
            )
    snapshot = connection.execute(
        "SELECT snapshot_state, snapshot_manifest_id FROM boundary_publication_generations WHERE id = %s FOR UPDATE",
        (generation_id,),
    ).fetchone()
    if (
        snapshot_pending == 0
        and snapshot is not None
        and text_value(snapshot[0]) == "ready"
        and (not affected_artifacts or "snapshot" in affected_artifacts)
    ):
        # A board built under an older ordering rule, such as an army-only
        # replacement of one that gains a board rebuild, freezes its inputs
        # under the current rule in a replacement; a generation's rule
        # cannot change once its membership is captured.
        if text_value(generation[8]) != SNAPSHOT_ORDERING_RULE_VERSION:
            new_id, _ = _supersede_generation(
                database,
                connection,
                boundary_at=boundary_at,
                sweep_id=sweep_id,
                generation_id=generation_id,
                generation=generation_number,
            )
            _try_enqueue_boundary_artifacts(
                database, connection, boundary_at=boundary_at, generation_id=new_id, army=army
            )
            return
        manifest = _freeze_boundary_manifest(
            connection, generation_id=generation_id, artifact_kind="snapshot"
        )
        if manifest is None:
            return
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
                PYTHON_BACKFILL_PRIORITY
                if operator_generation(connection, boundary_at, generation_number)
                else ended_day_priority(boundary_at - timedelta(days=1)),
            ),
        )
    army_state, snapshot_state = connection.execute(
        "SELECT army_state, snapshot_state FROM boundary_publication_generations WHERE id = %s FOR UPDATE",
        (generation_id,),
    ).fetchone()
    # The army's inputs freeze in a later transaction, once the board's build
    # has published it, whether or not its statistics ever do. Frozen with
    # the board's, on 14 Oct 2026 they took 2 min 58 s, growing each Season
    # day, and the board's build job stayed unseen until they finished. Only
    # the background check (``army``) freezes them, never a job's response slot.
    board_first = (
        not affected_artifacts or "snapshot" in affected_artifacts
    ) and text_value(snapshot_state) == "ready"
    if (
        army_pending == 0
        and text_value(army_state) == "ready"
        and (not affected_artifacts or "army" in affected_artifacts)
        and army and not board_first
    ):
        manifest = _freeze_boundary_manifest(
            connection, generation_id=generation_id, artifact_kind="army"
        )
        if manifest is None:
            return
        key = base = f"build_army_analytics:boundary:{boundary_text}:gen:{generation_number}:manifest:{manifest[1]}"
        # A build that gave up while members were still pending runs again,
        # under a new key, once they have all settled; it stays failed.
        while (
            failed := connection.execute(
                """
                SELECT id FROM python_processing_jobs_worker
                WHERE deduplication_key = %s AND state = 'failed'
                  AND failure_detail = %s
                """,
                (key, ARMY_MEMBERS_PENDING),
            ).fetchone()
        ) is not None:
            key = f"{base}:retry:{failed[0]}"
        connection.execute(
            """
            INSERT INTO python_processing_jobs_worker (
                observation_id, work_type, deduplication_key, input_json,
                state, due_at, parser_version, processing_version,
                domain_rule_version, analytics_rule_version, priority
            ) VALUES (NULL, 'build_army_analytics', %s, %s, 'pending', clock_timestamp(), %s, %s, %s, %s, %s)
            ON CONFLICT (deduplication_key) DO NOTHING
            """,
            (
                key,
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
                PYTHON_BACKFILL_PRIORITY
                if operator_generation(connection, boundary_at, generation_number)
                else PYTHON_LIVE_PRIORITY,
            ),
        )


# Why an army build fails while a member's army status is still pending.
ARMY_MEMBERS_PENDING = "boundary army publication dependency is not terminal"


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

    The board's members follow the weekly league record
    (``boundary_members``): a player whose day is saved after the members
    were taken joins, at once while nothing froze, else as a changed member.
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
    if not boundary_members.board_members(connection, boundary_at, member_ids, only=player_id):
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
        generation_id, generation = boundary_members.create_generation(
            database,
            connection,
            boundary_at=boundary_at,
            sweep_id=sweep_id,
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
        # A generation's members never change. A player whose day is saved
        # after they were taken joins the board at its next rebuild, which
        # takes the members again; before the record, never.
        joins = prior_member is None
        if joins:
            if not boundary_members.joins_late(boundary_at):
                return True
            if shared:
                raise LockNotAvailable(
                    f"Reset {boundary_at.isoformat()} queues a new member only under its full lock"
                )
            prior_member = (None, None)
        changed = (
            prior_member[0] is None
            or int(prior_member[0]) != int(ranked_day_version_id)
            or text_value(prior_member[1]) != ranked_day_input_hash
        )
        frozen_artifacts = [
            kind
            for kind, manifest_id in (
                ("snapshot", current[5]),
                ("army", current[6]),
            )
            if manifest_id is not None
        ]
        correction_active = text_value(current[4]) == "active"
        if correction_active and current[7] and not (changed and frozen_artifacts):
            affected = {text_value(value) for value in current[7]}
            frozen_artifacts = [
                kind for kind in frozen_artifacts if kind in affected
            ]
        elif changed and (frozen_artifacts or joins):
            # A ranked-day source feeds both artifacts. Once either
            # manifest has frozen, reprocess both instead of inheriting an
            # army identity built from the previous source version. A new
            # member needs both rebuilt, frozen or not.
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
            text_value(current[2]) == "published"
            and text_value(current[3]) == "published"
            # A correction already waiting, a repair campaign holding this
            # Reset, or a Reset not yet due to rebuild keeps the change
            # queued, so waiting inputs start together.
            and not (
                changed
                and (
                    queued is not None
                    or boundary_held(connection, boundary_at)
                    or correction_waits(connection, boundary_at, prior_member[0] is None)
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
            boundary_members.record_day(
                database, connection, generation_id=generation_id, boundary_at=boundary_at,
                player_id=player_id, ranked_day_version_id=ranked_day_version_id,
                input_hash=ranked_day_input_hash)
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
                **(ARRIVAL if prior_member[0] is None else {}),
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
                        )
                    WHERE id = %s
                    """,
                    (correction_artifacts, queued[0]),
                )
                # One row per added input: adding to the list itself saved
                # the whole list again (migration 0124).
                connection.execute(
                    """
                    INSERT INTO boundary_publication_correction_inputs
                        (correction_id, input)
                    VALUES (%s, %s)
                    """,
                    (queued[0], Jsonb(pending_input)),
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
    boundary_members.record_day(
        database, connection, generation_id=generation_id, boundary_at=boundary_at,
        player_id=player_id, ranked_day_version_id=ranked_day_version_id,
        input_hash=ranked_day_input_hash, insert_missing=True)
    if not shared:
        _try_enqueue_boundary_artifacts(database,
            connection, boundary_at=boundary_at, generation_id=generation_id
        )
    return True


def queue_board_rebuilds(
    database: Database, season_id: str, *, queue: bool, repair: bool = False
) -> dict[str, Any]:
    """Find, and with ``queue`` rebuild, each of the Season's Reset boards
    whose frozen input still ranks a reading taken before the player's
    profile answered "player not found", whose saved entries differ from
    the trophies at the Reset ``reset_trophies`` now gives, or its mark,
    that an older ordering rule ordered, that is a Season's last board
    ranking a tie out of the game's final order (``official_order``), or
    whose members the weekly league record now changes
    (``boundary_members``). On 5
    to 7 October 2026 that was 24 players on Day 1 and 34 on Day 2, two of
    them first and second on Day 2, and 290 Day 2 entries missing battles
    after their readings; on 8 October, 3 Day 3 entries marked proven but
    missing attacks before them.

    Each board gets one queued correction of both its leaderboard and army
    records, started as any other: once its build is published, outside a
    repair campaign and past-Reset pacing. With ``repair``, as the Season
    repair's boards step calls it, that correction, or the one already
    waiting, is marked ``REPAIR_CORRECTION`` and skips the interval between
    rebuilds outside the quiet window. Each Reset's newest board is read
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
                    database,
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
                    ORDER BY position
                    """,
                    (snapshot_id,),
                ).fetchall()
                late_battles = sum(
                    (trophies, text_value(confidence) == "confirmed")
                    != expected.get(int(player_id))
                    for player_id, trophies, confidence in entries
                )
                # Players a Season's last board ranks out of the game's order
                # inside a tie (analytics.follow_official_ties).
                saved = [{"player_id": int(row[0]), "trophies": int(row[1])} for row in entries]
                official_order = sum(entry is not kept for entry, kept in zip(
                    follow_official_ties(saved, season_end_placements(
                        connection, boundary_at,
                        {entry["player_id"]: entry["trophies"] for entry in saved},
                    )), saved, strict=True))
                # A board ordered by an older rule, such as equal trophies
                # by tag hash alone before the shared tie order.
                rule = connection.execute(
                    "SELECT ordering_rule_version FROM leaderboard_snapshots WHERE id = %s",
                    (snapshot_id,),
                ).fetchone()
                reordered = (
                    rule is not None
                    and text_value(rule[0]) != SNAPSHOT_ORDERING_RULE_VERSION
                )
                left_off = connection.execute(
                    "SELECT player_id, ranked_day_version_id, input_identity->>'snapshot_quality'"
                    " FROM boundary_publication_manifest_entries(%s)"
                    " WHERE input_identity->>'snapshot_quality' <> 'eligible'",
                    (manifest_id,),
                ).fetchall()
                # Players left out whom the board now ranks at 5,000, unless
                # "player not found" since keeps them out of the rebuild too.
                added = unplayed_profiles(connection, boundary_at, [
                    int(row[0]) for row in left_off])
                # Members left out, not for their league, whom their Complete
                # day now ranks, named by a reading after the Reset.
                late = len(boundary_members.first_readings(connection, boundary_at, {
                    int(row[0]): int(row[1]) for row in left_off
                    if row[1] is not None
                    and row[2] not in ("below_legend_i", "league_not_confirmed")
                }))
                unplayed = len(added) - len(profiles_not_found(
                    connection, boundary_at,
                    {player_id: row[2] for player_id, row in added.items()},
                ))
                # Members the weekly league record now adds or takes off.
                members = {int(row[0]) for row in connection.execute(
                    "SELECT player_id FROM boundary_publication_manifest_entries(%s)",
                    (manifest_id,),
                )}
                moved_members = len(members.symmetric_difference(boundary_members.board_members(
                    connection, boundary_at, [int(value) for value in connection.execute(
                        """
                        SELECT sweep.member_ids FROM boundary_publication_generations AS generation
                        JOIN collector_reset_sweeps AS sweep ON sweep.id = generation.sweep_id
                        WHERE generation.id = %s
                        """,
                        (generation_id,),
                    ).fetchone()[0] or []],
                )))
                if not (not_found or late_battles or reordered or unplayed or late
                        or moved_members or official_order):
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
                    if repair:
                        connection.execute(
                            """
                            INSERT INTO boundary_publication_correction_inputs
                                (correction_id, input)
                            SELECT correction.id, %s
                            FROM boundary_publication_corrections AS correction
                            WHERE correction.id = %s
                              AND NOT clashlens_correction_inputs(correction) @> %s::jsonb
                            """,
                            (Jsonb(REPAIR_CORRECTION), queued[0],
                             Jsonb([REPAIR_CORRECTION])),
                        )
                elif queue:
                    connection.execute(
                        """
                        INSERT INTO boundary_publication_corrections
                            (boundary_at, source_generation_id,
                             affected_artifacts, pending_inputs)
                        VALUES (%s, %s, ARRAY['snapshot', 'army'], %s)
                        """,
                        (boundary_at, generation_id,
                         Jsonb([REPAIR_CORRECTION] if repair else [])),
                    )
                boards.append(
                    {
                        "boundary_at": boundary_at.astimezone(UTC).isoformat(),
                        "generation": int(generation),
                        "profile_not_found": len(not_found),
                        "late_battles": late_battles,
                        "reordered": reordered,
                        "members_changed": moved_members,
                        "official_order": official_order,
                        "correction": (
                            "already_queued" if queued is not None
                            else "queued" if queue
                            else "not_queued"
                        ),
                    }
                )
    return {"season_id": season_id, "queue": queue, "boards": boards}


def queue_army_corrections(
    database: Database, season_id: str, *, queue: bool, max_jobs: int
) -> dict[str, Any]:
    """Find, and with ``queue`` rebuild the army records of, each of the
    Season's Resets that published a battle side as unread
    (``decode_missing``) although an army is now saved for that side.

    While unit catalogue v3 re-read the Season's armies, a Reset could freeze
    a side whose only saved army was under v2 before frozen builds read any
    saved army. ``sides`` counts such battle sides per Reset whose newest
    saved army was read; a failed reading restores nothing.

    Each Reset gets one army-only operator correction (OPERATOR_CORRECTION),
    started as any other: once its build is published, outside a repair
    campaign and past-Reset pacing. It is not queued or started, nor any of
    its leaderboard, statistics or army builds claimed, from 04:00 to 07:00
    UTC, the newest Reset's included; those builds run at background
    priority, and the army build takes no battle lock. At most
    ``max_jobs`` corrections of the Season wait or build at once. A rebuilt Reset
    lists no such side, so a later run lists nothing for it; one still
    queued or rebuilding is listed again and not queued twice.
    """
    season_start = datetime.fromtimestamp(int(season_id), UTC)
    if not is_season_boundary(season_start):
        raise ValueError(f"{season_id} is not a Season's start")
    resets: list[dict[str, Any]] = []
    with database.pool.connection() as connection:
        with connection.transaction():
            if queue and operator_correction_waits(connection):
                return {
                    "season_id": season_id,
                    "queue": queue,
                    "refused": "no correction is queued from 04:00 to 07:00 UTC",
                }
            unread = """
                fact.is_current AND fact.army_state = 'decode_missing'
                AND fact.ranked_day_start >= %s AND fact.ranked_day_start < %s
            """
            days = (season_start, season_start + SEASON_DURATION)
            battle_ids = [
                int(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT fact.battle_id"
                    f" FROM army_analytics_battle_facts AS fact WHERE {unread}",
                    days,
                ).fetchall()
            ]
            # A side counts only if its newest saved army is readable.
            affected = connection.execute(
                f"""
                SELECT fact.ranked_day_start + interval '1 day', count(*)
                FROM {CURRENT_DECODES} AS decode
                JOIN army_analytics_battle_facts AS fact
                  ON fact.battle_id = decode.battle_id
                 AND fact.lens = CASE decode.perspective
                     WHEN 'attacker' THEN 'offense' ELSE 'defense' END
                WHERE decode.status IN ('decoded', 'partial')
                  AND {unread}
                GROUP BY 1 ORDER BY 1
                """,
                (DECODER_VERSION, battle_ids, *days),
            ).fetchall()
            in_flight = connection.execute(
                """
                SELECT count(*) FROM boundary_publication_corrections
                WHERE boundary_at > %s AND boundary_at <= %s
                  AND state NOT IN ('finalized', 'terminal')
                """,
                (season_start, season_start + SEASON_DURATION),
            ).fetchone()[0]
        for boundary_at, sides in affected:
            with connection.transaction():
                lock_boundary_publication(connection, boundary_at)
                current = connection.execute(
                    """
                    SELECT id, generation, army_state
                    FROM boundary_publication_generations
                    WHERE boundary_at = %s AND army_state <> 'superseded'
                    ORDER BY generation DESC
                    LIMIT 1
                    """,
                    (boundary_at,),
                ).fetchone()
                if current is None:
                    continue
                generation_id, generation, army_state = current
                queued = connection.execute(
                    """
                    SELECT id FROM boundary_publication_corrections
                    WHERE boundary_at = %s AND source_generation_id = %s
                      AND state IN ('queued', 'pending_inputs')
                    """,
                    (boundary_at, generation_id),
                ).fetchone()
                # A build not yet published reads the saved armies itself.
                correction = (
                    "rebuilding" if text_value(army_state) != "published"
                    else "already_queued" if queued is not None
                    else "not_queued"
                )
                if queue and correction == "not_queued" and in_flight < max_jobs:
                    connection.execute(
                        """
                        INSERT INTO boundary_publication_corrections
                            (boundary_at, source_generation_id,
                             affected_artifacts, pending_inputs)
                        VALUES (%s, %s, ARRAY['army'], %s)
                        """,
                        (boundary_at, generation_id, Jsonb([OPERATOR_CORRECTION])),
                    )
                    in_flight += 1
                    correction = "queued"
                resets.append(
                    {
                        "boundary_at": boundary_at.astimezone(UTC).isoformat(),
                        "generation": int(generation),
                        "sides": int(sides),
                        "correction": correction,
                    }
                )
    return {
        "season_id": season_id,
        "queue": queue,
        "in_flight": int(in_flight),
        "resets": resets,
    }


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
            or text_value(row[1]) != input_hash
            or dict(row[2]) != source_identity
        ):
            raise ValueError("boundary publication artifact identity conflict")
    return int(row[0])


def remove_league_members(database: Database, connection: Any, player_id: int, days: list[datetime]) -> None:
    """The player's week holding ``days`` left Legend I: take them off the
    board of each Reset ending one where they are a member. A generation's
    members never change, so a board not frozen yet stops waiting for their
    day and ranks them nowhere (``boundary_members.leagues``), and a frozen one gets a queued
    correction, whose replacement takes the members again. The caller holds
    the player's week rows. The Reset's lock is taken as a day result takes
    it (``boundary.lock_boundary_members``), shared while many members wait,
    so this never waits behind them; a Reset busy past ``RESET_LOCK_WAIT``
    (``TAIL_LOCK_WAIT`` while the last members wait), as while its board
    freezes, raises LockNotAvailable, and the decider tries the player again
    after the rest, ``league_decider.BUSY_SECONDS`` later."""
    # The Reset's newest generation, and the player's member row in it.
    newest = """
        SELECT generation.id, generation.snapshot_manifest_id,
               generation.army_manifest_id, member.status
        FROM boundary_publication_generations AS generation
        LEFT JOIN boundary_publication_generation_members AS member
          ON member.generation_id = generation.id AND member.player_id = %s
        WHERE generation.boundary_at = %s AND generation.snapshot_state <> 'superseded'
          AND generation.army_state <> 'superseded'
        ORDER BY generation.generation DESC LIMIT 1
    """
    for day in sorted(set(days)):
        boundary_at = day + RANKED_DAY_DURATION
        current = connection.execute(newest, (player_id, boundary_at)).fetchone()
        if current is None or current[3] is None:
            continue
        shared = lock_boundary_members(connection, boundary_at, RESET_LOCK_WAIT)
        current = connection.execute(
            newest + ("" if shared else " FOR UPDATE OF generation"), (player_id, boundary_at)
        ).fetchone()
        if current is None or current[3] is None:
            continue
        if current[1] is None and current[2] is None:
            if text_value(current[3]) == "pending":
                boundary_members.write_member_result(
                    connection, int(current[0]), player_id, "unavailable", "unavailable",
                    status="terminal")
            if not shared:
                _try_enqueue_boundary_artifacts(
                    database, connection, boundary_at=boundary_at, generation_id=int(current[0]))
            continue
        queued = connection.execute(
            """
            SELECT id FROM boundary_publication_corrections
            WHERE boundary_at = %s AND source_generation_id = %s
              AND state IN ('queued', 'pending_inputs')
            ORDER BY id DESC LIMIT 1
            FOR UPDATE
            """,
            (boundary_at, current[0]),
        ).fetchone()
        if queued is not None:
            connection.execute(
                """
                UPDATE boundary_publication_corrections
                SET affected_artifacts = ARRAY(SELECT DISTINCT unnest(
                        affected_artifacts || ARRAY['snapshot', 'army']))
                WHERE id = %s
                """,
                (queued[0],),
            )
        else:
            connection.execute(
                """
                INSERT INTO boundary_publication_corrections
                    (boundary_at, source_generation_id, affected_artifacts, pending_inputs)
                VALUES (%s, %s, ARRAY['snapshot', 'army'], '[]'::jsonb)
                """,
                (boundary_at, current[0]),
            )
