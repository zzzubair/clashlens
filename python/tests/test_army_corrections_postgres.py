from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from domain_test_support import domain_database, store_observation, text
from test_army_analytics_publication_postgres import (
    DAY_START,
    FIXTURE_CODE,
    SEASON_ID,
    _army_job,
    _event,
    _processor,
    _publish_day,
    _row,
)

from clashlens import boundary, past_reset_pacing

FIRST_RESET = DAY_START + timedelta(days=1)
SECOND_RESET = DAY_START + timedelta(days=2)


def test_army_corrections_queue_once_for_resets_that_left_out_a_saved_army(
    database_url: str, archive_server, monkeypatch
) -> None:
    """A Reset frozen during the unit catalogue v3 re-read could publish a
    side as unread although its v2 army was saved. Only such Resets get an
    army rebuild, once, and none is queued from 04:00 to 07:00 UTC. A side
    whose newest saved army failed to read restores nothing, so it is not
    counted even though an older reading was saved."""
    with domain_database(database_url, include_coordinator=True) as ci:
        ts1 = DAY_START + timedelta(hours=1)
        ts2 = FIRST_RESET + timedelta(hours=1)
        jobs = [
            store_observation(
                ci, archive_server, occurrence_key=key, endpoint="battle_log",
                body=json.dumps({"items": [_row(True, opponent, FIXTURE_CODE, ts, 3, 100)]}).encode(),
                observed_at=ts + timedelta(minutes=1), normalized_tag="#2PP",
            )[1]
            for key, opponent, ts in (("fix-a1", "#8PP", ts1), ("fix-a2", "#9PP", ts2))
        ]
        database, processor = _processor(ci, archive_server, monkeypatch)
        try:
            for index, job in enumerate(jobs):
                assert processor.process_job(job, owner=f"ingest-{index}").outcome in (
                    "processed",
                    "processed_with_gaps",
                )
            with database.pool.connection() as connection:
                battles = {
                    text(tag): int(battle_id)
                    for tag, battle_id in connection.execute(
                        """
                        SELECT def.normalized_tag, b.id FROM legend_battles AS b
                        JOIN players AS def ON def.id = b.defender_player_id
                        """
                    ).fetchall()
                }
            _publish_day(database, "#2PP", [_event(battles["#8PP"], "offense", ts1, 3, 100, 40)])
            assert processor.process_job(_army_job(database), owner="army-1").outcome == "processed"
            _publish_day(
                database, "#2PP", [_event(battles["#9PP"], "offense", ts2, 3, 100, 40)],
                day_start=FIRST_RESET, day_number=24,
            )
            assert processor.process_job(_army_job(database), owner="army-2").outcome == "processed"
            with database.pool.connection() as connection:
                connection.execute("SET LOCAL session_replication_role = replica")
                # Both days published their attack as unread; only the first
                # day's attack has a readable newest army saved.
                connection.execute(
                    "UPDATE army_analytics_battle_facts SET army_state = 'decode_missing'"
                    " WHERE is_current"
                )
                connection.execute(
                    """
                    UPDATE battle_army_decodes SET catalog_version = 'unit-catalog-v2'
                    WHERE battle_id = %s AND perspective = 'attacker' AND is_active
                    """,
                    (battles["#9PP"],),
                )
                connection.execute(
                    """
                    INSERT INTO battle_army_decodes (
                        battle_id, evidence_id, perspective, raw_code,
                        decoder_version, catalog_version, catalog_hash, status,
                        failure_category
                    )
                    SELECT battle_id, evidence_id, perspective, raw_code,
                           decoder_version, 'unit-catalog-test', catalog_hash,
                           'failed', 'malformed'
                    FROM battle_army_decodes
                    WHERE battle_id = %s AND perspective = 'attacker' AND is_active
                    """,
                    (battles["#9PP"],),
                )
                for reset in (FIRST_RESET, SECOND_RESET):
                    connection.execute(
                        """
                        INSERT INTO boundary_publication_generations (
                            boundary_at, target_at, generation, ordering_rule_version,
                            freshness_rule_version, expected_population_count,
                            expected_population_hash, snapshot_state, army_state
                        ) VALUES (%s, %s, 1, 'test', 'test', 1, %s, 'published', 'published')
                        """,
                        (reset, reset, "0" * 64),
                    )

            def corrections() -> list[tuple]:
                with database.pool.connection() as connection:
                    return [
                        (boundary_at, [text(item) for item in affected], text(state))
                        for boundary_at, affected, state in connection.execute(
                            "SELECT boundary_at, affected_artifacts, state"
                            " FROM boundary_publication_corrections ORDER BY id"
                        ).fetchall()
                    ]

            now = [datetime(2026, 10, 9, 4, 10, tzinfo=UTC)]
            monkeypatch.setattr(past_reset_pacing, "_now", lambda _connection: now[0])
            refused = boundary.queue_army_corrections(
                database, SEASON_ID, queue=True, max_jobs=2
            )
            assert "refused" in refused
            assert corrections() == []

            now[0] = datetime(2026, 10, 9, 12, tzinfo=UTC)
            reports = [
                boundary.queue_army_corrections(
                    database, SEASON_ID, queue=queue, max_jobs=2
                )
                for queue in (False, True, True)
            ]
            assert [report["resets"] for report in reports] == [
                [
                    {
                        "boundary_at": FIRST_RESET.isoformat(),
                        "generation": 1,
                        "sides": 1,
                        "correction": correction,
                    }
                ]
                for correction in ("not_queued", "queued", "already_queued")
            ]
            assert [report["in_flight"] for report in reports] == [0, 1, 1]
            assert corrections() == [(FIRST_RESET, ["army"], "queued")]
        finally:
            database.close()
