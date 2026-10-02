"""A battle saved after its Legend day was published is added to that day.

Once per Reset, after the Reset sweep has finished and every response fetched
before it finished has been processed, the sweep queues the existing
recalculation for that day, then for each later saved day of the player once
the day before it has finished.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from domain_test_support import domain_database, store_observation, text
from test_domain_processing_postgres import (
    LIVE_BATTLE_PARSER_VERSION,
    _live_battle_row,
    _processor,
    _seed_battle_anchor,
    _WorkerRoleDatabase,
)
from test_league_history import _entry, _payload

from clashlens import job_outcomes, late_battle_sweep, reconciliation_db
from clashlens.db import DOMAIN_RULE_VERSION, PROCESSING_VERSION
from clashlens.late_battle_sweep import LateBattleSweep, sweep_late_battles
from clashlens.league_history import LEAGUE_HISTORY_PARSER_VERSION

ANCHOR = datetime(2026, 8, 3, 5, tzinfo=UTC)
DAY = ANCHOR + timedelta(days=1)
TAG = "#8PP"


def _on_time_defense() -> dict:
    return _live_battle_row(
        attack=False,
        battle_timestamp=DAY + timedelta(hours=1),
        opponent_tag="#QPP",
        opponent_name="Earlier Attacker",
    )


def _late_defense() -> dict:
    # 0 stars at 49%: the attacker gains trophies, the defender loses none.
    return _live_battle_row(
        attack=False,
        battle_timestamp=DAY + timedelta(days=1, seconds=-10),
        opponent_tag="#2PP",
        opponent_name="Zero Star Attacker",
        stars=0,
        destruction_percentage=49,
    )


def _store_log(connection_info, archive_server, *, key, rows, observed_at):
    return store_observation(
        connection_info,
        archive_server,
        occurrence_key=key,
        endpoint="battle_log",
        body=json.dumps({"items": rows}).encode(),
        observed_at=observed_at,
        normalized_tag=TAG,
        parser_version=LIVE_BATTLE_PARSER_VERSION,
    )[1]


def _process(processor, *job_ids: int) -> None:
    for job_id in job_ids:
        result = processor.process_job(job_id, owner=f"test-{job_id}")
        assert result is not None and result.outcome == "processed"


def _save_log(connection_info, archive_server, processor, *, key, rows, observed_at):
    _process(
        processor,
        _store_log(
            connection_info,
            archive_server,
            key=key,
            rows=rows,
            observed_at=observed_at,
        ),
    )


def _publish(database, processor, day: datetime) -> None:
    _process(
        processor,
        reconciliation_db.enqueue_reconciliation(
            database,
            player_tag=TAG,
            day_start=day,
            now=day,
            request_key="published-before-late-battle",
        ),
    )


def _finish_reset_sweep(connection_info: str, boundary: datetime) -> None:
    with psycopg.connect(connection_info) as connection:
        connection.execute(
            "INSERT INTO collector_reset_sweeps (boundary_at) VALUES (%s)",
            (boundary,),
        )
        connection.commit()


def _correct(database, processor, now: datetime) -> None:
    """Run the sweep and its queued jobs until no correction is in progress."""
    for _round in range(10):
        result = sweep_late_battles(database, now=now)
        assert result is not None
        job_ids, players_in_progress = result
        if players_in_progress == 0:
            return
        # Each player has one recalculation queued at a time.
        assert len(job_ids) == players_in_progress
        _process(processor, *job_ids)
    raise AssertionError("corrections did not finish")


def _published(connection_info: str, day: datetime) -> tuple[list, int] | None:
    """The defenses on the player's latest published log, and its version id."""
    with psycopg.connect(connection_info) as connection:
        row = connection.execute(
            """
            SELECT log.battles, log.ranked_day_version_id
            FROM api_player_daily_logs AS log
            JOIN players AS player ON player.id = log.player_id
            WHERE player.normalized_tag = %s AND log.ranked_day_start = %s
            ORDER BY log.version DESC
            LIMIT 1
            """,
            (TAG, day),
        ).fetchone()
    if row is None:
        return None
    defenses = sorted(
        (item["opponent_tag"], item["stars"], item["trophy_change"])
        for item in row[0]
        if item["lens"] == "defense"
    )
    return defenses, int(row[1])


def _queued_days(connection_info: str) -> list[str]:
    with psycopg.connect(connection_info) as connection:
        return [
            text(row[0])
            for row in connection.execute(
                """
                SELECT input_json ->> 'ranked_day_start'
                FROM python_processing_jobs
                WHERE input_json ->> 'trigger' = 'late_battle_sweep'
                ORDER BY id
                """
            ).fetchall()
        ]


def _previous_day_version(connection_info: str, day: datetime) -> int:
    with psycopg.connect(connection_info) as connection:
        return int(
            connection.execute(
                """
                SELECT input_evidence -> 'previous_day' ->> 'version_id'
                FROM ranked_day_versions AS version
                JOIN players AS player ON player.id = version.player_id
                WHERE player.normalized_tag = %s AND version.ranked_day_start = %s
                ORDER BY version.version DESC
                LIMIT 1
                """,
                (TAG, day),
            ).fetchone()[0]
        )


def _published_with_late_battle(connection_info, archive_server, processor, database):
    """Publish DAY and the day after, then save DAY's late battle."""
    _seed_battle_anchor(connection_info, ANCHOR)
    _save_log(
        connection_info,
        archive_server,
        processor,
        key="on-time-log",
        rows=[_on_time_defense()],
        observed_at=DAY + timedelta(hours=2),
    )
    _publish(database, processor, DAY)
    _publish(database, processor, DAY + timedelta(days=1))
    _save_log(
        connection_info,
        archive_server,
        processor,
        key="late-log",
        rows=[_late_defense(), _on_time_defense()],
        observed_at=DAY + timedelta(days=1, minutes=20),
    )


def test_late_pre_reset_battle_is_added_to_its_published_day_once(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        worker_database = _WorkerRoleDatabase(connection_info)
        try:
            _published_with_late_battle(
                connection_info, archive_server, processor, database
            )
            next_day = DAY + timedelta(days=1)
            boundary = DAY + timedelta(days=2)
            _finish_reset_sweep(connection_info, boundary)
            before = _published(connection_info, DAY)
            assert before is not None and before[0] == [("#QPP", 3, -40)]

            # The production worker role can read the evidence and queue the
            # existing job; the job table's own checks accept it.
            result = sweep_late_battles(
                worker_database, now=boundary + timedelta(minutes=31)
            )
            assert result is not None and len(result[0]) == 1
            assert _queued_days(connection_info) == ["2026-08-04T05:00:00Z"]
            # Running again before the worker gets to it changes nothing.
            assert sweep_late_battles(
                database, now=boundary + timedelta(minutes=41)
            ) == ([], 1)

            _process(processor, *result[0])
            _correct(database, processor, boundary + timedelta(minutes=51))

            assert _queued_days(connection_info) == [
                "2026-08-04T05:00:00Z",
                "2026-08-05T05:00:00Z",
            ]
            after = _published(connection_info, DAY)
            assert after is not None
            assert after[0] == [("#2PP", 0, 0), ("#QPP", 3, -40)]
            assert after[1] != before[1]
            # The next day is recalculated from the corrected day.
            assert _previous_day_version(connection_info, next_day) == after[1]

            # Once published, the late battle needs no more work.
            assert sweep_late_battles(
                database, now=boundary + timedelta(minutes=61)
            ) == ([], 0)
            assert len(_queued_days(connection_info)) == 2
            assert _published(connection_info, DAY) == after
        finally:
            worker_database.close()
            database.close()


def test_battle_saved_after_the_reset_but_already_published_needs_nothing(
    database_url: str, archive_server
) -> None:
    # The usual case: the day's Reset recalculation ran after the battle was
    # saved, so the saved result already lists it.
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _seed_battle_anchor(connection_info, ANCHOR)
            _save_log(
                connection_info,
                archive_server,
                processor,
                key="reset-log",
                rows=[_late_defense(), _on_time_defense()],
                observed_at=DAY + timedelta(days=1, minutes=1),
            )
            _publish(database, processor, DAY)
            boundary = DAY + timedelta(days=1)
            _finish_reset_sweep(connection_info, boundary)

            assert sweep_late_battles(
                database, now=boundary + timedelta(minutes=31)
            ) == ([], 0)
            assert _queued_days(connection_info) == []
        finally:
            database.close()


def test_later_saved_days_are_refreshed_and_missing_days_stay_missing(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _seed_battle_anchor(connection_info, ANCHOR)
            _save_log(
                connection_info,
                archive_server,
                processor,
                key="on-time-log",
                rows=[_on_time_defense()],
                observed_at=DAY + timedelta(hours=2),
            )
            _publish(database, processor, DAY)
            _publish(database, processor, DAY + timedelta(days=2))
            _save_log(
                connection_info,
                archive_server,
                processor,
                key="late-log",
                rows=[_late_defense(), _on_time_defense()],
                observed_at=DAY + timedelta(days=1, minutes=20),
            )
            boundary = DAY + timedelta(days=3)
            _finish_reset_sweep(connection_info, boundary)

            _correct(database, processor, boundary + timedelta(minutes=31))
            assert _queued_days(connection_info) == [
                "2026-08-04T05:00:00Z",
                "2026-08-06T05:00:00Z",
            ]

            corrected = _published(connection_info, DAY)
            assert corrected is not None
            assert corrected[0] == [("#2PP", 0, 0), ("#QPP", 3, -40)]
            assert _published(connection_info, DAY + timedelta(days=1)) is None
            with psycopg.connect(connection_info) as connection:
                missing_day_versions = connection.execute(
                    "SELECT count(*) FROM ranked_day_versions "
                    "WHERE ranked_day_start = %s",
                    (DAY + timedelta(days=1),),
                ).fetchone()[0]
            assert missing_day_versions == 0
        finally:
            database.close()


@pytest.mark.parametrize("waiting", ["league_history", "retried_battle_log"])
def test_sweep_waits_for_the_reset_and_every_response_fetched_before_it_finished(
    database_url: str, archive_server, waiting: str
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _published_with_late_battle(
                connection_info, archive_server, processor, database
            )
            boundary = DAY + timedelta(days=2)
            ready_at = boundary + timedelta(minutes=31)
            with psycopg.connect(connection_info) as connection:
                player_id = connection.execute(
                    "SELECT id FROM players WHERE normalized_tag = %s", (TAG,)
                ).fetchone()[0]
                sweep_id = connection.execute(
                    "INSERT INTO collector_reset_sweeps (boundary_at) "
                    "VALUES (%s) RETURNING id",
                    (boundary,),
                ).fetchone()[0]
                work_id = connection.execute(
                    """
                    INSERT INTO collector_work (
                        kind, lane, scope, player_id, normalized_tag, sweep_id,
                        due_at, coalescing_key
                    ) VALUES (
                        'reset_baseline', 'reset', 'player', %s, %s, %s, %s,
                        'reset-baseline-wait'
                    ) RETURNING id
                    """,
                    (player_id, TAG, sweep_id, boundary),
                ).fetchone()[0]
                connection.commit()
            # A response fetched before the Reset sweep finished is still
            # waiting: league history not yet processed, or a battle log
            # whose first attempt failed and is waiting for its retry.
            if waiting == "league_history":
                _observation, waiting_job = store_observation(
                    connection_info,
                    archive_server,
                    occurrence_key="league-history-before-finish",
                    endpoint="league_history",
                    body=_payload(_entry()),
                    observed_at=boundary + timedelta(minutes=2),
                    normalized_tag=TAG,
                    parser_version=LEAGUE_HISTORY_PARSER_VERSION,
                    processing_version=PROCESSING_VERSION,
                    domain_rule_version=DOMAIN_RULE_VERSION,
                )
            else:
                waiting_job = _store_log(
                    connection_info,
                    archive_server,
                    key="retried-log",
                    rows=[_on_time_defense()],
                    observed_at=boundary + timedelta(minutes=3),
                )
                claim = database.claim_job(owner="retry-test", job_id=waiting_job)
                assert claim is not None
                assert (
                    job_outcomes.fail_claim(
                        database,
                        claim,
                        category="test_failure",
                        detail="first attempt failed",
                        retryable=True,
                    )
                    == "waiting_retry"
                )

            assert (
                sweep_late_battles(database, now=boundary + timedelta(minutes=29))
                is None
            )
            # The Reset sweep itself has not finished.
            assert sweep_late_battles(database, now=ready_at) is None

            with psycopg.connect(connection_info) as connection:
                connection.execute(
                    """
                    UPDATE collector_work
                    SET status = 'complete', completed_at = clock_timestamp(),
                        updated_at = clock_timestamp()
                    WHERE id = %s
                    """,
                    (work_id,),
                )
                connection.commit()
            # A response fetched after the Reset sweep finished never holds
            # the sweep back.
            _store_log(
                connection_info,
                archive_server,
                key="after-finish-log",
                rows=[_on_time_defense()],
                observed_at=boundary + timedelta(minutes=12),
            )
            assert sweep_late_battles(database, now=ready_at) is None
            with psycopg.connect(connection_info) as connection:
                connection.execute(
                    "UPDATE python_processing_jobs SET due_at = clock_timestamp() "
                    "WHERE id = %s",
                    (waiting_job,),
                )
                connection.commit()
            _process(processor, waiting_job)

            result = sweep_late_battles(database, now=ready_at)
            assert result is not None and len(result[0]) == 1
        finally:
            database.close()


def test_worker_loop_checks_every_ten_minutes_until_corrections_finish(
    database_url: str, archive_server, monkeypatch, capsys
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _published_with_late_battle(
                connection_info, archive_server, processor, database
            )
            boundary = DAY + timedelta(days=2)
            _finish_reset_sweep(connection_info, boundary)
            clock = [0.0]
            monkeypatch.setattr(late_battle_sweep, "monotonic", lambda: clock[0])
            sweep = LateBattleSweep(database)

            def check(minutes: int, at: datetime = boundary) -> list[str]:
                clock[0] += 600
                sweep.run_when_due(now=at + timedelta(minutes=minutes))
                return _queued_days(connection_info)

            sweep.run_when_due(now=boundary + timedelta(minutes=25))
            assert _queued_days(connection_info) == []
            # Ready, but ten minutes have not passed since the last check.
            clock[0] += 300
            sweep.run_when_due(now=boundary + timedelta(minutes=35))
            assert _queued_days(connection_info) == []
            assert len(check(40)) == 1
            # The next day waits until the corrected day has been recalculated.
            assert len(check(50)) == 1
            _process(processor, _late_job_ids(connection_info)[-1])
            assert len(check(60)) == 2
            _process(processor, _late_job_ids(connection_info)[-1])
            assert len(check(70)) == 2

            # Every correction has finished, so a battle saved later waits for
            # the next Reset's sweep.
            _save_log(
                connection_info,
                archive_server,
                processor,
                key="later-late-log",
                rows=[
                    _live_battle_row(
                        attack=False,
                        battle_timestamp=DAY + timedelta(days=1, seconds=-30),
                        opponent_tag="#CPP",
                        opponent_name="Another Late Attacker",
                    ),
                    _late_defense(),
                    _on_time_defense(),
                ],
                observed_at=boundary + timedelta(minutes=75),
            )
            assert len(check(80)) == 2

            next_boundary = boundary + timedelta(days=1)
            _finish_reset_sweep(connection_info, next_boundary)
            assert len(check(30, next_boundary)) == 3
            statuses = [
                json.loads(line)["status"]
                for line in capsys.readouterr().out.splitlines()
                if '"late_battle_sweep"' in line
            ]
            assert statuses == ["running", "running", "running", "complete", "running"]
        finally:
            database.close()


def _late_job_ids(connection_info: str) -> list[int]:
    with psycopg.connect(connection_info) as connection:
        return [
            int(row[0])
            for row in connection.execute(
                "SELECT id FROM python_processing_jobs "
                "WHERE input_json ->> 'trigger' = 'late_battle_sweep' ORDER BY id"
            ).fetchall()
        ]


def test_later_day_waits_for_the_corrected_day_even_after_a_restart(
    database_url: str, archive_server
) -> None:
    # A worker that is behind, restarted or running several lanes must still
    # recalculate the later day from the corrected earlier day.
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _published_with_late_battle(
                connection_info, archive_server, processor, database
            )
            boundary = DAY + timedelta(days=2)
            _finish_reset_sweep(connection_info, boundary)
            for minutes in range(31, 91, 10):
                # A fresh sweep each time, as after a worker restart.
                LateBattleSweep(database).run_when_due(
                    now=boundary + timedelta(minutes=minutes)
                )
            assert _queued_days(connection_info) == ["2026-08-04T05:00:00Z"]

            for minutes in range(91, 200, 10):
                LateBattleSweep(database).run_when_due(
                    now=boundary + timedelta(minutes=minutes)
                )
                # Every queued job is due now; run the newest first, as a
                # second lane would.
                for job_id in reversed(_late_job_ids(connection_info)):
                    processor.process_job(job_id, owner=f"lane-{job_id}")

            assert _queued_days(connection_info) == [
                "2026-08-04T05:00:00Z",
                "2026-08-05T05:00:00Z",
            ]
            corrected = _published(connection_info, DAY)
            assert corrected is not None
            assert corrected[0] == [("#2PP", 0, 0), ("#QPP", 3, -40)]
            assert (
                _previous_day_version(connection_info, DAY + timedelta(days=1))
                == corrected[1]
            )
        finally:
            database.close()


def test_late_battle_older_than_the_window_is_logged_and_not_queued(
    database_url: str, archive_server, capsys
) -> None:
    # The worker was stopped for over a week after the late battle was saved.
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _published_with_late_battle(
                connection_info, archive_server, processor, database
            )
            boundary = DAY + timedelta(days=8)
            _finish_reset_sweep(connection_info, boundary)
            before = _published(connection_info, DAY)

            assert sweep_late_battles(
                database, now=boundary + timedelta(minutes=31)
            ) == ([], 0)

            assert _queued_days(connection_info) == []
            assert _published(connection_info, DAY) == before
            with psycopg.connect(connection_info) as connection:
                player_id = connection.execute(
                    "SELECT id FROM players WHERE normalized_tag = %s", (TAG,)
                ).fetchone()[0]
            events = [
                json.loads(line)
                for line in capsys.readouterr().out.splitlines()
                if '"late_battle_sweep_skipped"' in line
            ]
            assert [event["player_days"] for event in events] == [
                [f"{player_id}:2026-08-04T05:00:00+00:00"]
            ]
        finally:
            database.close()


OPPONENT = "#2PP"


def _lists_the_battle(connection_info: str, tag: str) -> bool:
    """Whether the player's latest saved result lists the shared battle.

    A battle whose two reports disagree is left off the public battle list.
    """
    with psycopg.connect(connection_info) as connection:
        row = connection.execute(
            """
            SELECT log.battles
            FROM api_player_daily_logs AS log
            JOIN players AS player ON player.id = log.player_id
            WHERE player.normalized_tag = %s AND log.ranked_day_start = %s
            ORDER BY log.version DESC
            LIMIT 1
            """,
            (tag, DAY),
        ).fetchone()
    return any(item["opponent_tag"] in (TAG, OPPONENT) for item in row[0])


@pytest.mark.parametrize("late_army", ["u2x0-2x1", "u1x0-2x1"])
def test_other_players_day_is_corrected_when_a_late_report_changes_agreement(
    database_url: str, archive_server, late_army: str
) -> None:
    # The attacker's report was published before the Reset; the defender's
    # report arrives late. A different army code makes the reports disagree.
    # The same army code, after an earlier disagreeing report, makes them agree.
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _seed_battle_anchor(connection_info, ANCHOR)
            battle_time = DAY + timedelta(days=1, seconds=-10)
            _process(
                processor,
                store_observation(
                    connection_info,
                    archive_server,
                    occurrence_key="attacker-log",
                    endpoint="battle_log",
                    body=json.dumps(
                        {
                            "items": [
                                _live_battle_row(
                                    attack=True,
                                    battle_timestamp=battle_time,
                                    opponent_tag=TAG,
                                    opponent_name="Defender",
                                    stars=0,
                                    destruction_percentage=49,
                                )
                            ]
                        }
                    ).encode(),
                    observed_at=battle_time + timedelta(seconds=5),
                    normalized_tag=OPPONENT,
                    parser_version=LIVE_BATTLE_PARSER_VERSION,
                )[1],
            )
            defense = _late_defense()
            if late_army == "u1x0-2x1":
                # An earlier defender report disagreed with the attacker.
                _save_log(
                    connection_info,
                    archive_server,
                    processor,
                    key="disagreeing-log",
                    rows=[{**defense, "armyShareCode": "u3x0-2x1"}],
                    observed_at=battle_time + timedelta(seconds=5),
                )
            for tag in (TAG, OPPONENT):
                _process(
                    processor,
                    reconciliation_db.enqueue_reconciliation(
                        database,
                        player_tag=tag,
                        day_start=DAY,
                        now=DAY,
                        request_key=f"published-{tag}",
                    ),
                )
            before = _lists_the_battle(connection_info, OPPONENT)
            _save_log(
                connection_info,
                archive_server,
                processor,
                key="late-defender-log",
                rows=[{**defense, "armyShareCode": late_army}],
                observed_at=DAY + timedelta(days=1, minutes=20),
            )
            boundary = DAY + timedelta(days=1)
            _finish_reset_sweep(connection_info, boundary)

            _correct(database, processor, boundary + timedelta(minutes=31))

            agreed = late_army == "u1x0-2x1"
            assert before is not agreed
            assert _lists_the_battle(connection_info, OPPONENT) is agreed
            assert _lists_the_battle(connection_info, TAG) is agreed
        finally:
            database.close()
