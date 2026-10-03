"""Extra saved copies of an ended Legend day are deleted once its Reset is done.

Each battle saves a new copy of the player's day result. Only the newest is
shown, so the others go, except a copy a publication or the following day
still points at. Pages, the leaderboard, recalculation and late corrections
read the same results afterwards.
"""

from __future__ import annotations

import json
from datetime import timedelta

import psycopg
import pytest
from domain_test_support import as_api_role, domain_database, store_observation
from test_bookkeeping_storage_postgres import _visible
from test_domain_processing_postgres import (
    LIVE_BATTLE_PARSER_VERSION,
    _role_connection,
)
from test_late_battle_sweep_postgres import (
    ANCHOR,
    DAY,
    OPPONENT,
    TAG,
    _finish_reset_sweep,
    _late_defense,
    _live_battle_row,
    _previous_day_version,
    _process,
    _published,
    _save_log,
    _saved_disagreement,
    _seed_battle_anchor,
)
from test_late_battle_sweep_postgres import _processor as _sweep_processor
from test_reconciliation_postgres import (
    BATTLE_FIXTURE,
    DAY_END,
    DAY_START,
    _processor,
    _store_baseline_pair,
)

from clashlens import reconciliation_db
from clashlens.api_db import ApiDatabase
from clashlens.late_battle_sweep import sweep_late_battles
from clashlens.ranked_day_compaction import compact


def _copies(connection: psycopg.Connection, tag: str, day) -> list[tuple]:
    """Each saved copy of the player's day, oldest first: id, replaced id, log count."""
    return connection.execute(
        """
        SELECT version.id, version.replaces_version_id,
               (SELECT count(*) FROM api_player_daily_logs AS log
                WHERE log.ranked_day_version_id = version.id)
        FROM ranked_day_versions AS version
        JOIN players AS player ON player.id = version.player_id
        WHERE player.normalized_tag = %s AND version.ranked_day_start = %s
        ORDER BY version.version
        """,
        (tag, day),
    ).fetchall()


def _compact(connection_info: str, now, **options) -> dict:
    with psycopg.connect(connection_info, autocommit=True) as connection:
        return compact(connection, now=now, pause_seconds=0, **options)


def _ended_day_with_copies(
    connection_info: str, archive_server, database, processor
) -> None:
    """Save #2PP's day from four fetches and both Reset baselines."""
    items = json.loads(BATTLE_FIXTURE.read_bytes())["items"]
    first = items[0]
    second = dict(first, opponentPlayerTag="#9PP", stars=2,
                  destructionPercentage=80, battleTimestamp="20260804T130000.000Z")
    third = dict(first, opponentPlayerTag="#7PP", stars=1,
                 destructionPercentage=60, battleTimestamp="20260804T150000.000Z")
    jobs = list(_store_baseline_pair(
        connection_info, archive_server, key="copies-start", boundary=DAY_START,
        trophies=6000, empty_battle_log=True,
    )[2:])
    for index, fetch in enumerate(
        [[first], [first, items[1]], [second, first, items[1]],
         [third, second, first, items[1]]]
    ):
        jobs.append(store_observation(
            connection_info, archive_server,
            occurrence_key=f"copies-fetch-{index}", endpoint="battle_log",
            body=json.dumps({"items": fetch}).encode(), normalized_tag="#2PP",
            observed_at=DAY_START + timedelta(hours=8 + index * 2),
        )[1])
        for job in jobs:
            # A day recalculated before its end Reset has gaps.
            assert processor.process_job(job, owner="copies").outcome in {
                "processed", "processed_with_gaps"
            }
        # Recalculating after each fetch saves another copy, as a battle does.
        jobs = [reconciliation_db.enqueue_reconciliation(
            database, player_tag="#2PP", day_start=DAY_START, now=DAY_START,
            request_key=f"copies-{index}",
        )]
    jobs.extend(_store_baseline_pair(
        connection_info, archive_server, key="copies-end", boundary=DAY_END,
        trophies=6090, empty_battle_log=True,
    )[2:])
    for job in jobs:
        assert processor.process_job(job, owner="copies").outcome == "processed"
    while processor.process_once(owner="copies-follow-up") is not None:
        pass


def test_extra_copies_go_once_the_reset_is_done_and_what_players_see_stays(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        api = ApiDatabase(as_api_role(connection_info))
        worker = _role_connection(connection_info, "clashlens_python_worker")
        try:
            _ended_day_with_copies(connection_info, archive_server, database, processor)
            with database.pool.connection() as connection:
                copies = _copies(connection, "#2PP", DAY_START)
                assert len(copies) >= 4
                newest = copies[-1][0]
                # A publication points at an older copy, so it stays.
                published_copy = copies[1][0]
                generation = connection.execute(
                    """
                    INSERT INTO boundary_publication_generations (
                        boundary_at, target_at, generation, ordering_rule_version,
                        freshness_rule_version, expected_population_count,
                        expected_population_hash
                    ) SELECT %s, %s, coalesce(max(generation), 0) + 100, 'test',
                             'test', 1, %s
                    FROM boundary_publication_generations
                    RETURNING id
                    """,
                    (DAY_END, DAY_END, "0" * 64),
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO boundary_publication_generation_members (
                        generation_id, player_id, ranked_day_version_id
                    ) SELECT %s, player_id, id FROM ranked_day_versions WHERE id = %s
                    """,
                    (generation, published_copy),
                )
                # The coordinator's own publications also point at copies.
                pointed_at = {row[0] for row in connection.execute(
                    "SELECT ranked_day_version_id FROM boundary_publication_generation_members"
                )}
                kept = [row[0] for row in copies if row[0] in pointed_at | {newest}]
                assert len(kept) < len(copies)
                connection.commit()
            visible = _visible(api)

            # Late battles can still arrive in the first 30 minutes after the
            # Reset, as for the late-battle sweep, so the day waits.
            _compact(connection_info, DAY_END + timedelta(minutes=10))
            with database.pool.connection() as connection:
                assert len(_copies(connection, "#2PP", DAY_START)) == len(copies)

            after_reset = DAY_END + timedelta(minutes=40)

            # The worker's own role runs the cleanup, one player per batch,
            # but cannot delete a saved copy itself.
            result = compact(worker, now=after_reset, pause_seconds=0,
                             players_per_batch=1)
            assert result["status"] == "idle" and result["batches"] > 1
            assert result["deleted_versions"] >= len(copies) - len(kept)
            assert result["deleted_logs"] == result["deleted_versions"]
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                worker.execute("DELETE FROM ranked_day_versions")
            worker.rollback()

            with database.pool.connection() as connection:
                # Each kept copy now names the kept copy before it, if any.
                assert _copies(connection, "#2PP", DAY_START) == [
                    (copy, kept[index - 1] if index else None, 1)
                    for index, copy in enumerate(kept)
                ]
                # Recalculating the ended day finds its result unchanged.
                player_id, versions = connection.execute(
                    """
                    SELECT player_id, ARRAY[parser_version, processing_version,
                                            domain_rule_version, analytics_rule_version]
                    FROM ranked_day_versions WHERE id = %s
                    """,
                    (newest,),
                ).fetchone()
                reconciliation_db.recalculate_ranked_day(
                    database, connection, player_id=player_id, day_start=DAY_START,
                    parser_version=versions[0], processing_version=versions[1],
                    domain_rule_version=versions[2], analytics_rule_version=versions[3],
                )
                connection.commit()
                assert _copies(connection, "#2PP", DAY_START)[-1][0] == newest
            assert _visible(api) == visible
            # Nothing is left to clean until a newer copy is saved.
            assert _compact(connection_info, after_reset)["batches"] == 0
        finally:
            worker.close()
            api.close()
            database.close()


def _published_agreeing_days(connection_info, archive_server, database, processor):
    """Publish DAY and the day after with a late defense both players agree on."""
    _seed_battle_anchor(connection_info, ANCHOR)
    battle_time = DAY + timedelta(days=1, seconds=-10)
    _process(processor, store_observation(
        connection_info, archive_server,
        occurrence_key="attacker-log", endpoint="battle_log",
        body=json.dumps({"items": [_live_battle_row(
            attack=True, battle_timestamp=battle_time, opponent_tag=TAG,
            opponent_name="Defender", stars=0, destruction_percentage=49,
        )]}).encode(),
        observed_at=battle_time + timedelta(seconds=5),
        normalized_tag=OPPONENT,
        parser_version=LIVE_BATTLE_PARSER_VERSION,
    )[1])
    agreeing = {**_late_defense(), "armyShareCode": "u1x0-2x1"}
    _save_log(connection_info, archive_server, processor, key="agreeing-log",
              rows=[agreeing], observed_at=battle_time + timedelta(seconds=5))
    for day in (DAY, DAY + timedelta(days=1)):
        _process(processor, reconciliation_db.enqueue_reconciliation(
            database, player_tag=OPPONENT, day_start=day, now=day,
            request_key=f"published-{day.isoformat()}",
        ))
    return agreeing


def test_late_corrections_after_cleanup_save_the_same_results(
    database_url: str, archive_server
) -> None:
    # The scenario of the late-battle sweep's agreement test, with the cleanup
    # run after each correction: the first result's copy is deleted before
    # the inputs return to it, and it must still become current again.
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _sweep_processor(connection_info, archive_server)
        try:
            agreeing = _published_agreeing_days(
                connection_info, archive_server, database, processor
            )
            next_day = DAY + timedelta(days=1)
            first = _published(connection_info, DAY, OPPONENT)[1]
            with psycopg.connect(connection_info) as connection:
                first_input = connection.execute(
                    "SELECT input_hash FROM ranked_day_versions WHERE id = %s", (first,)
                ).fetchone()[0]
            boundary = DAY + timedelta(days=2)
            _finish_reset_sweep(connection_info, boundary)

            _save_log(connection_info, archive_server, processor,
                      key="disagreeing-log",
                      rows=[{**agreeing, "armyShareCode": "u3x0-2x1"}],
                      observed_at=DAY + timedelta(days=1, minutes=20))
            assert sweep_late_battles(
                database, now=boundary + timedelta(minutes=31)
            ) == (1, 0)
            disputed = _published(connection_info, DAY, OPPONENT)[1]
            assert _compact(
                connection_info, boundary + timedelta(minutes=32)
            )["deleted_versions"] >= 2
            with psycopg.connect(connection_info) as connection:
                assert [row[0] for row in _copies(connection, OPPONENT, DAY)] == [disputed]
                assert len(_copies(connection, OPPONENT, next_day)) == 1

            _save_log(connection_info, archive_server, processor,
                      key="agreeing-again-log", rows=[agreeing],
                      observed_at=DAY + timedelta(days=1, minutes=40))
            assert sweep_late_battles(
                database, now=boundary + timedelta(minutes=41)
            ) == (1, 0)
            assert _saved_disagreement(connection_info, OPPONENT) == [False]
            restored = _published(connection_info, DAY, OPPONENT)[1]
            assert restored not in (first, disputed)
            with psycopg.connect(connection_info) as connection:
                # The same inputs as the deleted first result, saved again.
                assert connection.execute(
                    "SELECT input_hash FROM ranked_day_versions WHERE id = %s",
                    (restored,),
                ).fetchone()[0] == first_input
            assert _previous_day_version(connection_info, next_day, OPPONENT) == restored
            assert sweep_late_battles(
                database, now=boundary + timedelta(minutes=51)
            ) == (0, 0)

            # The correction's replaced copies are cleaned in turn.
            _compact(connection_info, boundary + timedelta(minutes=52))
            with psycopg.connect(connection_info) as connection:
                assert [row[0] for row in _copies(connection, OPPONENT, DAY)] == [restored]
                assert len(_copies(connection, OPPONENT, next_day)) == 1
            assert _published(connection_info, DAY, OPPONENT)[1] == restored
            assert _previous_day_version(connection_info, next_day, OPPONENT) == restored
        finally:
            database.close()


def test_a_result_made_current_again_before_cleanup_stays_unchanged_after_it(
    database_url: str, archive_server
) -> None:
    # The day's result goes A, B, then back to A before any cleanup, so the
    # last copy is saved as A made current again over B. After the day and
    # the day after it are cleaned, recalculating with the same inputs must
    # find that result unchanged.
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _sweep_processor(connection_info, archive_server)
        try:
            agreeing = _published_agreeing_days(
                connection_info, archive_server, database, processor
            )
            next_day = DAY + timedelta(days=1)
            first = _published(connection_info, DAY, OPPONENT)[1]
            boundary = DAY + timedelta(days=2)
            _finish_reset_sweep(connection_info, boundary)
            _save_log(connection_info, archive_server, processor,
                      key="disagreeing-log",
                      rows=[{**agreeing, "armyShareCode": "u3x0-2x1"}],
                      observed_at=DAY + timedelta(days=1, minutes=20))
            assert sweep_late_battles(
                database, now=boundary + timedelta(minutes=31)
            ) == (1, 0)
            disputed = _published(connection_info, DAY, OPPONENT)[1]
            _save_log(connection_info, archive_server, processor,
                      key="agreeing-again-log", rows=[agreeing],
                      observed_at=DAY + timedelta(days=1, minutes=40))
            assert sweep_late_battles(
                database, now=boundary + timedelta(minutes=41)
            ) == (1, 0)
            restored = _published(connection_info, DAY, OPPONENT)[1]
            assert len({first, disputed, restored}) == 3

            assert _compact(
                connection_info, boundary + timedelta(minutes=52)
            )["deleted_versions"] >= 1
            with psycopg.connect(connection_info) as connection:
                kept = _copies(connection, OPPONENT, DAY)
                assert [row[0] for row in kept] == [first, disputed, restored]
                assert kept[-1][1] == disputed
                next_day_copies = _copies(connection, OPPONENT, next_day)
                corrections = connection.execute(
                    "SELECT count(*) FROM boundary_publication_corrections"
                ).fetchone()[0]

            _process(processor, reconciliation_db.enqueue_reconciliation(
                database, player_tag=OPPONENT, day_start=DAY,
                now=boundary + timedelta(minutes=53), request_key="late-same-inputs",
            ))
            with psycopg.connect(connection_info) as connection:
                assert _copies(connection, OPPONENT, DAY) == kept
                assert _copies(connection, OPPONENT, next_day) == next_day_copies
                assert connection.execute(
                    "SELECT count(*) FROM boundary_publication_corrections"
                ).fetchone()[0] == corrections
            assert _published(connection_info, DAY, OPPONENT)[1] == restored
        finally:
            database.close()
