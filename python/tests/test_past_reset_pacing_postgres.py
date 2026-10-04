from __future__ import annotations

from datetime import UTC, datetime, timedelta

from domain_test_support import domain_database

from clashlens import boundary, boundary_publication, past_reset_pacing
from clashlens.db import Database

PAST = datetime(2026, 8, 4, 5, tzinfo=UTC)
LIVE = PAST + timedelta(days=1)
# Midday, outside the 04:30-07:00 quiet window.
NOON = datetime(2026, 8, 10, 12, tzinfo=UTC)


class Clock:
    def __init__(self, monkeypatch, now: datetime) -> None:
        self.now = now
        monkeypatch.setattr(past_reset_pacing, "_now", lambda _connection: self.now)


def _version(
    connection, player_id: int, boundary_at: datetime, version: int, state: str
) -> int:
    input_hash = format(version, "x") * 64
    return int(
        connection.execute(
            """
            INSERT INTO ranked_day_versions (
                player_id, ranked_day_start, ranked_day_end, official_season_id,
                season_day_number, season_anchor_rule_version,
                reconciliation_rule_version, result_hash, version, state, confidence,
                input_hash, evidence_complete, coverage_complete
            ) VALUES (
                %s, %s, %s, 'test-season', 1, 'test-anchor', 'test-rules',
                %s, %s, %s, 'exact', %s, true, true
            ) RETURNING id
            """,
            (
                player_id,
                boundary_at - timedelta(days=1),
                boundary_at,
                input_hash,
                version,
                state,
                input_hash,
            ),
        ).fetchone()[0]
    )


def _record(
    database,
    connection,
    player_id: int,
    boundary_at: datetime,
    version: int,
    state: str = "Complete",
):
    version_id = _version(connection, player_id, boundary_at, version, state)
    boundary._record_boundary_generation(
        database,
        connection,
        boundary_at=boundary_at,
        player_id=player_id,
        ranked_day_version_id=version_id,
        ranked_day_input_hash=format(version, "x") * 64,
    )
    return version_id


def _publish(database, connection, generation_id: int, boundary_at: datetime) -> None:
    input_hash = format(generation_id, "x").rjust(64, "b")
    snapshot_manifest = boundary._freeze_boundary_manifest(
        database, connection, generation_id=generation_id, artifact_kind="snapshot"
    )
    army_manifest = boundary._freeze_boundary_manifest(
        database, connection, generation_id=generation_id, artifact_kind="army"
    )
    snapshot_id = connection.execute(
        """
        INSERT INTO leaderboard_snapshots (
            snapshot_kind, boundary_at, version,
            ordering_rule_version, freshness_rule_version, state,
            measured_coverage, stale_entry_count,
            eligible_population_count, included_entry_count,
            fresh_entry_count, input_hash
        ) VALUES ('frozen', %s,
                  (SELECT count(*) + 1 FROM leaderboard_snapshots WHERE boundary_at = %s),
                  'order', 'freshness', 'published', 1, 0, 1, 1, 1, %s)
        RETURNING id
        """,
        (boundary_at, boundary_at, input_hash),
    ).fetchone()[0]
    snapshot_identity = boundary._create_boundary_artifact_identity(
        connection,
        generation_id=generation_id,
        artifact_kind="analytics",
        manifest_id=int(snapshot_manifest[0]),
        input_hash=input_hash,
        source_identity={"snapshot_id": int(snapshot_id)},
    )
    army_identity = boundary._create_boundary_artifact_identity(
        connection,
        generation_id=generation_id,
        artifact_kind="army",
        manifest_id=int(army_manifest[0]),
        input_hash="c" * 64,
        source_identity={"generation": generation_id},
    )
    connection.execute(
        """
        UPDATE boundary_publication_generations
        SET snapshot_state = 'published', snapshot_id = %s,
            snapshot_input_hash = %s, snapshot_manifest_id = %s,
            snapshot_analytics_publication_id = %s,
            army_state = 'published', army_input_hash = repeat('c', 64),
            army_manifest_id = %s, army_publication_id = %s
        WHERE id = %s
        """,
        (
            snapshot_id,
            input_hash,
            snapshot_manifest[0],
            snapshot_identity,
            army_manifest[0],
            army_identity,
            generation_id,
        ),
    )
    boundary_publication._maybe_emit_boundary_signal(
        database, connection, generation_id=generation_id
    )


def _published_resets(database, connection) -> int:
    """Publish the past and live Resets, each with one member; return it."""
    player_id = int(
        connection.execute(
            "INSERT INTO players (normalized_tag, active) VALUES ('#PACING', true) RETURNING id"
        ).fetchone()[0]
    )
    for boundary_at in (PAST, LIVE):
        connection.execute(
            """
            INSERT INTO collector_reset_sweeps
                (boundary_at, member_ids, membership_captured_at)
            VALUES (%s, %s, clock_timestamp())
            """,
            (boundary_at, [player_id]),
        )
        _record(database, connection, player_id, boundary_at, 1)
        _publish(database, connection, _latest(connection, boundary_at)[0], boundary_at)
    return player_id


def _latest(connection, boundary_at: datetime) -> tuple[int, int, int | None]:
    """The newest generation's id, number and the member's day version."""
    row = connection.execute(
        """
        SELECT generation.id, generation.generation, member.ranked_day_version_id
        FROM boundary_publication_generations AS generation
        JOIN boundary_publication_generation_members AS member
          ON member.generation_id = generation.id
        WHERE generation.boundary_at = %s
        ORDER BY generation.generation DESC
        LIMIT 1
        """,
        (boundary_at,),
    ).fetchone()
    return int(row[0]), int(row[1]), row[2]


def _created(connection, boundary_at: datetime, at: datetime) -> None:
    connection.execute(
        "UPDATE boundary_publication_generations SET created_at = %s WHERE boundary_at = %s",
        (at, boundary_at),
    )


def test_past_reset_corrections_within_six_hours_start_one_generation_later(
    database_url: str, monkeypatch
) -> None:
    clock = Clock(monkeypatch, NOON)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id = _published_resets(database, connection)
                _created(connection, PAST, NOON - timedelta(hours=10))
                # The first correction in 10 hours rebuilds at once.
                _record(database, connection, player_id, PAST, 2)
                rebuilt_id, rebuilt, _version_id = _latest(connection, PAST)
                assert rebuilt == 2
                _publish(database, connection, rebuilt_id, PAST)
                _created(connection, PAST, NOON)

                clock.now = NOON + timedelta(hours=1)
                _record(database, connection, player_id, PAST, 3)
                latest_version = _record(database, connection, player_id, PAST, 4)
                connection.commit()
                boundary_publication.reevaluate_boundary_publications(database)
                assert _latest(connection, PAST)[1] == 2

                clock.now = NOON + timedelta(hours=6)
                boundary_publication.reevaluate_boundary_publications(database)
                # Both corrections start together, with the newest input.
                assert _latest(connection, PAST)[1:] == (3, latest_version)
                boundary_publication.reevaluate_boundary_publications(database)
                assert _latest(connection, PAST)[1] == 3
        finally:
            database.close()


def test_past_reset_correction_waits_out_the_quiet_window(
    database_url: str, monkeypatch
) -> None:
    clock = Clock(monkeypatch, NOON)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id = _published_resets(database, connection)
                _created(connection, PAST, NOON - timedelta(hours=10))
                clock.now = datetime(2026, 8, 11, 4, 30, tzinfo=UTC)
                published_id = _latest(connection, PAST)[0]
                boundary_publication._queue_boundary_army_correction(
                    database, connection, boundary_at=PAST, generation_id=published_id
                )
                corrected_version = _record(database, connection, player_id, PAST, 2)
                connection.commit()
                clock.now = datetime(2026, 8, 11, 6, 59, tzinfo=UTC)
                boundary_publication.reevaluate_boundary_publications(database)
                assert _latest(connection, PAST)[1] == 1

                clock.now = datetime(2026, 8, 11, 7, tzinfo=UTC)
                boundary_publication.reevaluate_boundary_publications(database)
                assert _latest(connection, PAST)[1:] == (2, corrected_version)
                affected = connection.execute(
                    """
                    SELECT affected_artifacts FROM boundary_publication_generations
                    WHERE boundary_at = %s AND generation = 2
                    """,
                    (PAST,),
                ).fetchone()[0]
                assert sorted(affected) == ["army", "snapshot"]
        finally:
            database.close()


def _build_jobs(connection, boundary_at: datetime, generation: int) -> list[str]:
    key = boundary_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    return sorted(
        row[0]
        for row in connection.execute(
            """
            SELECT work_type FROM python_processing_jobs_worker
            WHERE deduplication_key LIKE %s
            """,
            (f"%:boundary:{key}:gen:{generation}:%",),
        ).fetchall()
    )


def test_past_reset_correction_joins_the_one_already_waiting(
    database_url: str, monkeypatch
) -> None:
    clock = Clock(monkeypatch, NOON)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id = _published_resets(database, connection)
                published_id = _latest(connection, PAST)[0]
                _created(connection, PAST, NOON)
                clock.now = NOON + timedelta(hours=1)
                boundary_publication._queue_boundary_army_correction(
                    database, connection, boundary_at=PAST, generation_id=published_id
                )
                _record(database, connection, player_id, PAST, 2)

                # Due again, a later correction arrives before the re-check.
                clock.now = NOON + timedelta(hours=6)
                latest_version = _record(database, connection, player_id, PAST, 3)
                assert _latest(connection, PAST)[1] == 1
                connection.commit()
                boundary_publication.reevaluate_boundary_publications(database)
                assert _latest(connection, PAST)[1:] == (2, latest_version)
                boundary_publication.reevaluate_boundary_publications(database)
                assert _latest(connection, PAST)[1] == 2
                waiting = connection.execute(
                    """
                    SELECT count(*) FROM boundary_publication_corrections
                    WHERE boundary_at = %s AND state IN ('queued', 'pending_inputs')
                    """,
                    (PAST,),
                ).fetchone()[0]
                assert waiting == 0
        finally:
            database.close()


def test_past_reset_build_ready_in_the_quiet_window_starts_after_it(
    database_url: str, monkeypatch
) -> None:
    clock = Clock(monkeypatch, datetime(2026, 8, 11, 4, 29, tzinfo=UTC))
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id = _published_resets(database, connection)
                _created(connection, PAST, NOON - timedelta(hours=10))
                _record(database, connection, player_id, PAST, 2, state="Live")
                assert _latest(connection, PAST)[1] == 2
                assert _build_jobs(connection, PAST, 2) == []

                clock.now = datetime(2026, 8, 11, 4, 31, tzinfo=UTC)
                _record(database, connection, player_id, PAST, 3)
                connection.commit()
                boundary_publication.reevaluate_boundary_publications(database)
                assert _build_jobs(connection, PAST, 2) == []

                clock.now = datetime(2026, 8, 11, 7, tzinfo=UTC)
                boundary_publication.reevaluate_boundary_publications(database)
                assert _build_jobs(connection, PAST, 2) == [
                    "build_army_analytics",
                    "build_snapshot",
                ]
        finally:
            database.close()


def test_past_reset_build_queued_before_the_quiet_window_starts_after_it(
    database_url: str, monkeypatch
) -> None:
    clock = Clock(monkeypatch, datetime(2026, 8, 11, 4, 29, tzinfo=UTC))
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id = _published_resets(database, connection)
                _created(connection, PAST, NOON - timedelta(hours=10))
                _record(database, connection, player_id, PAST, 2)
                assert _build_jobs(connection, PAST, 2) == [
                    "build_army_analytics",
                    "build_snapshot",
                ]
                clock.now = datetime(2026, 8, 11, 4, 31, tzinfo=UTC)
                _record(database, connection, player_id, LIVE, 2)
                connection.commit()

            def claimed() -> list[tuple[str, str]]:
                jobs = []
                while claim := database.claim_job(
                    owner="pacing",
                    work_types=["build_snapshot", "build_army_analytics"],
                ):
                    if claim.input_json["generation"] == 2:
                        jobs.append((claim.input_json["boundary_at"], claim.work_type))
                return sorted(jobs)

            live = LIVE.strftime("%Y-%m-%dT%H:%M:%SZ")
            past = PAST.strftime("%Y-%m-%dT%H:%M:%SZ")
            assert claimed() == [
                (live, "build_army_analytics"),
                (live, "build_snapshot"),
            ]
            with database.pool.connection() as connection:
                attempts = connection.execute(
                    """
                    SELECT state, attempt_count FROM python_processing_jobs_worker
                    WHERE input_json->>'boundary_at' = %s
                      AND input_json->>'generation' = '2'
                    """,
                    (past,),
                ).fetchall()
            assert [(str(state), count) for state, count in attempts] == [
                ("pending", 0),
                ("pending", 0),
            ]

            clock.now = datetime(2026, 8, 11, 7, tzinfo=UTC)
            assert claimed() == [
                (past, "build_army_analytics"),
                (past, "build_snapshot"),
            ]
        finally:
            database.close()


def test_live_reset_correction_rebuilds_at_once(database_url: str, monkeypatch) -> None:
    Clock(monkeypatch, datetime(2026, 8, 5, 5, 30, tzinfo=UTC))
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = Database(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id = _published_resets(database, connection)
                _created(connection, LIVE, datetime(2026, 8, 5, 5, 20, tzinfo=UTC))
                corrected_version = _record(database, connection, player_id, LIVE, 2)
                assert _latest(connection, LIVE)[1:] == (2, corrected_version)
        finally:
            database.close()
