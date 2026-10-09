"""New evidence queues the ended days it can change (``queue_refresh``)."""

from __future__ import annotations

from datetime import timedelta

import psycopg
from domain_test_support import domain_database, store_observation
from test_collector_db_postgres import NOW, _handoff, _hash, _player
from test_first_battle_log_postgres import _log
from test_reconciliation_postgres import _profile
from test_reset_reading_before_loss_postgres import DAY_B, DAY_C, DAY_D
from test_reset_settlement_state_postgres import TAG, _process, _reset_work

from clashlens.collector_db import CollectorDatabase


def _queued(connection_info: str, prefix: str) -> list[tuple[str, str]]:
    with psycopg.connect(connection_info) as connection:
        return sorted(
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT input_json ->> 'player_id', input_json ->> 'ranked_day_start'"
                " FROM python_processing_jobs WHERE deduplication_key LIKE %s",
                (prefix + "%",),
            )
        )


def test_unchanged_battle_log_check_after_a_profile_read_queues_the_day(
    database_url: str,
) -> None:
    # An unchanged check saves no response, yet covers a profile read since
    # the last check: the day that ended at the latest Reset is queued once.
    with domain_database(database_url, include_coordinator=True) as connection_info:
        player_id = _player(connection_info)
        database = CollectorDatabase(connection_info)
        for key, response_hash, endpoint, minutes in (
            ("log-1", _hash("a"), "battle_log", 0),
            ("profile-1", _hash("b"), "profile", 5),
            ("log-2", _hash("a"), "battle_log", 10),
            ("log-3", _hash("a"), "battle_log", 15),
        ):
            database.record_response(_handoff(
                occurrence_key=key, response_hash=response_hash, player_id=player_id,
                endpoint=endpoint, completed_at=NOW + timedelta(minutes=minutes),
            ))
        queued = _queued(connection_info, "reconcile:check:")

    assert queued == [(str(player_id), "2020-09-09T05:00:00Z")]


def test_only_new_battle_reports_queue_their_day_and_the_day_before(
    database_url: str, archive_server
) -> None:
    # Days B and C are saved. A log repeating day C's attack unchanged queues
    # nothing; one adding a day C defense queues day C and day B.
    day_b = [(DAY_B + timedelta(hours=hour), False) for hour in range(1, 9)]
    attack = (DAY_C + timedelta(hours=1), True)
    late_defense = (DAY_C + timedelta(hours=20), False)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(
            connection_info, archive_server, DAY_B, profile=_profile(6000), log=_log()
        )
        jobs += _reset_work(
            connection_info, archive_server, DAY_C, profile=_profile(5680),
            log=_log(*day_b),
        )
        jobs += _reset_work(
            connection_info, archive_server, DAY_D, profile=_profile(5720),
            log=_log(attack),
        )
        _process(connection_info, archive_server, jobs)
        queued = []
        for minutes, battles in ((60, (attack,)), (70, (attack, late_defense))):
            _, log_job = store_observation(
                connection_info, archive_server, occurrence_key=f"log-{minutes}",
                endpoint="battle_log", body=_log(*battles),
                observed_at=DAY_D + timedelta(minutes=minutes), normalized_tag=TAG,
            )
            _process(connection_info, archive_server, [log_job])
            queued.append(_queued(connection_info, "reconcile:report:"))
        with psycopg.connect(connection_info) as connection:
            player_id = str(connection.execute(
                "SELECT id FROM players WHERE normalized_tag = %s", (TAG,)
            ).fetchone()[0])

    assert queued[0] == []
    assert queued[1] == [
        (player_id, f"{DAY_B:%Y-%m-%dT%H:%M:%SZ}"),
        (player_id, f"{DAY_C:%Y-%m-%dT%H:%M:%SZ}"),
    ]
