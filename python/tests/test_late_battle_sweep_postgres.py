"""A battle saved after its Legend day was published is added to that day.

Once per Reset, after the Reset sweep has finished and every response fetched
before it finished has been processed, the sweep recalculates that day and
every later saved day of the player, in order, in one transaction.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from domain_test_support import domain_database, store_observation
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
from clashlens.reconciliation import RECONCILIATION_RULE_VERSION

ANCHOR = datetime(2026, 8, 3, 5, tzinfo=UTC)
DAY = ANCHOR + timedelta(days=1)
TAG = "#8PP"
OPPONENT = "#2PP"


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


def _published(
    connection_info: str, day: datetime, tag: str = TAG
) -> tuple[list, int] | None:
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
            (tag, day),
        ).fetchone()
    if row is None:
        return None
    defenses = sorted(
        (item["opponent_tag"], item["stars"], item["trophy_change"])
        for item in row[0]
        if item["lens"] == "defense"
    )
    return defenses, int(row[1])


def _previous_day_version(connection_info: str, day: datetime, tag: str = TAG) -> int:
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
                (tag, day),
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


def _reconcile_job_count(connection_info: str) -> int:
    with psycopg.connect(connection_info) as connection:
        return int(
            connection.execute(
                "SELECT count(*) FROM python_processing_jobs "
                "WHERE work_type = 'reconcile_ranked_day'"
            ).fetchone()[0]
        )


def _sweep_lines(capsys) -> list[dict]:
    return [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if '"late_battle_sweep"' in line
    ]


def _saved_disagreement(connection_info: str, tag: str) -> list[bool]:
    """The disagreement flag of each battle on the player's latest saved result."""
    with psycopg.connect(connection_info) as connection:
        row = connection.execute(
            """
            SELECT version.contribution_evidence
            FROM api_player_daily_logs AS log
            JOIN players AS player ON player.id = log.player_id
            JOIN ranked_day_versions AS version
              ON version.id = log.ranked_day_version_id
            WHERE player.normalized_tag = %s AND log.ranked_day_start = %s
            ORDER BY log.version DESC
            LIMIT 1
            """,
            (tag, DAY),
        ).fetchone()
    return [item["disagreement"] for item in row[0]]


def test_late_battle_corrects_its_day_then_later_days_in_order(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        worker_database = _WorkerRoleDatabase(connection_info)
        # The role database skips Database.__init__, so give it the schema
        # features the production worker detects at startup.
        for name, value in vars(database).items():
            if name.startswith(("_supports_", "_contract_version")):
                setattr(worker_database, name, value)
        try:
            _published_with_late_battle(
                connection_info, archive_server, processor, database
            )
            next_day = DAY + timedelta(days=1)
            boundary = DAY + timedelta(days=2)
            _finish_reset_sweep(connection_info, boundary)
            before = _published(connection_info, DAY)
            assert before is not None and before[0] == [("#QPP", 3, -40)]
            jobs_before = _reconcile_job_count(connection_info)

            # The production worker role can read the evidence and run the
            # existing recalculation directly; no job is queued.
            assert sweep_late_battles(
                worker_database, now=boundary + timedelta(minutes=31)
            ) == (1, 0)
            assert _reconcile_job_count(connection_info) == jobs_before

            after = _published(connection_info, DAY)
            assert after is not None
            assert after[0] == [("#2PP", 0, 0), ("#QPP", 3, -40)]
            assert after[1] != before[1]
            # The next day is recalculated from the corrected day.
            assert _previous_day_version(connection_info, next_day) == after[1]

            # Once published, the late battle needs no more work.
            assert sweep_late_battles(
                database, now=boundary + timedelta(minutes=41)
            ) == (0, 0)
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
            before = _published(connection_info, DAY)

            assert sweep_late_battles(
                database, now=boundary + timedelta(minutes=31)
            ) == (0, 0)
            assert _published(connection_info, DAY) == before
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

            assert sweep_late_battles(
                database, now=boundary + timedelta(minutes=31)
            ) == (1, 0)

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

            assert sweep_late_battles(database, now=ready_at) == (1, 0)
        finally:
            database.close()


def test_worker_loop_checks_every_ten_minutes_and_corrects_once_per_reset(
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

            sweep.run_when_due(now=boundary + timedelta(minutes=25))
            # Ready, but ten minutes have not passed since the last check.
            clock[0] += 300
            sweep.run_when_due(now=boundary + timedelta(minutes=35))
            assert _sweep_lines(capsys) == []
            clock[0] += 600
            sweep.run_when_due(now=boundary + timedelta(minutes=45))
            assert [
                (line["status"], line["corrected_players"])
                for line in _sweep_lines(capsys)
            ] == [("complete", 1)]

            # It has run for this Reset, so a battle saved later waits for the
            # next Reset's sweep.
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
                observed_at=boundary + timedelta(minutes=50),
            )
            clock[0] += 600
            sweep.run_when_due(now=boundary + timedelta(minutes=55))
            assert _sweep_lines(capsys) == []
            assert ("#CPP", 3, -40) not in _published(connection_info, DAY)[0]

            next_boundary = boundary + timedelta(days=1)
            _finish_reset_sweep(connection_info, next_boundary)
            clock[0] += 600
            sweep.run_when_due(now=next_boundary + timedelta(minutes=30))
            assert [line["status"] for line in _sweep_lines(capsys)] == ["complete"]
            assert ("#CPP", 3, -40) in _published(connection_info, DAY)[0]
        finally:
            database.close()


def test_failed_player_is_rolled_back_logged_and_retried_next_run(
    database_url: str, archive_server, monkeypatch, capsys
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _published_with_late_battle(
                connection_info, archive_server, processor, database
            )
            next_day = DAY + timedelta(days=1)
            boundary = DAY + timedelta(days=2)
            _finish_reset_sweep(connection_info, boundary)
            before = _published(connection_info, DAY)
            recalculate = reconciliation_db.recalculate_ranked_day

            def fail_on_next_day(*args, day_start, **kwargs):
                if day_start == next_day:
                    raise RuntimeError("next day failed")
                recalculate(*args, day_start=day_start, **kwargs)

            monkeypatch.setattr(
                reconciliation_db, "recalculate_ranked_day", fail_on_next_day
            )
            clock = [0.0]
            monkeypatch.setattr(late_battle_sweep, "monotonic", lambda: clock[0])
            sweep = LateBattleSweep(database)
            sweep.run_when_due(now=boundary + timedelta(minutes=31))

            # The corrected first day is rolled back with the failed day.
            assert _published(connection_info, DAY) == before
            lines = _sweep_lines(capsys)
            assert [line["status"] for line in lines] == ["player_failed", "retrying"]
            assert "next day failed" in lines[0]["error"]

            monkeypatch.setattr(
                reconciliation_db, "recalculate_ranked_day", recalculate
            )
            clock[0] += 600
            sweep.run_when_due(now=boundary + timedelta(minutes=41))
            assert [line["status"] for line in _sweep_lines(capsys)] == ["complete"]
            after = _published(connection_info, DAY)
            assert after is not None
            assert after[0] == [("#2PP", 0, 0), ("#QPP", 3, -40)]
            assert _previous_day_version(connection_info, next_day) == after[1]
        finally:
            database.close()


@pytest.mark.parametrize("next_day_is_today", [False, True])
def test_later_day_is_retried_after_an_existing_job_fixes_only_the_first_day(
    database_url: str, archive_server, monkeypatch, next_day_is_today: bool
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _published_with_late_battle(
                connection_info, archive_server, processor, database
            )
            next_day = DAY + timedelta(days=1)
            # Today's saved Live result is retried like an ended day.
            boundary = next_day if next_day_is_today else DAY + timedelta(days=2)
            _finish_reset_sweep(connection_info, boundary)
            existing_job = reconciliation_db.enqueue_reconciliation(
                database,
                player_tag=TAG,
                day_start=DAY,
                now=boundary,
                request_key="existing-first-day-job",
            )
            recalculate = reconciliation_db.recalculate_ranked_day

            def fail_on_next_day(*args, day_start, **kwargs):
                if day_start == next_day:
                    raise RuntimeError("next day failed")
                recalculate(*args, day_start=day_start, **kwargs)

            monkeypatch.setattr(
                reconciliation_db, "recalculate_ranked_day", fail_on_next_day
            )
            assert sweep_late_battles(
                database, now=boundary + timedelta(minutes=31)
            ) == (0, 1)
            monkeypatch.setattr(
                reconciliation_db, "recalculate_ranked_day", recalculate
            )

            # The existing job adds the late battle to the first day only.
            _process(processor, existing_job)
            first = _published(connection_info, DAY)
            assert first is not None
            assert first[0] == [("#2PP", 0, 0), ("#QPP", 3, -40)]
            assert _previous_day_version(connection_info, next_day) != first[1]

            assert sweep_late_battles(
                database, now=boundary + timedelta(minutes=41)
            ) == (1, 0)
            assert _published(connection_info, DAY) == first
            assert _previous_day_version(connection_info, next_day) == first[1]
        finally:
            database.close()


def test_correction_finishes_in_one_pass_before_the_window_slides(
    database_url: str, archive_server, capsys
) -> None:
    # The late battle's day is the oldest day in the window: it and every
    # later saved day are corrected in the same pass, so nothing is left for
    # the next Reset, when that day has left the window.
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database, processor = _processor(connection_info, archive_server)
        try:
            _published_with_late_battle(
                connection_info, archive_server, processor, database
            )
            boundary = DAY + timedelta(days=7)
            _finish_reset_sweep(connection_info, boundary)

            assert sweep_late_battles(
                database, now=boundary + timedelta(minutes=31)
            ) == (1, 0)
            corrected = _published(connection_info, DAY)
            assert corrected is not None
            assert corrected[0] == [("#2PP", 0, 0), ("#QPP", 3, -40)]
            assert (
                _previous_day_version(connection_info, DAY + timedelta(days=1))
                == corrected[1]
            )

            next_boundary = boundary + timedelta(days=1)
            _finish_reset_sweep(connection_info, next_boundary)
            assert sweep_late_battles(
                database, now=next_boundary + timedelta(minutes=31)
            ) == (0, 0)
            assert _sweep_lines(capsys) == []
        finally:
            database.close()


def test_late_battle_older_than_the_window_is_not_corrected(
    database_url: str, archive_server
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
            ) == (0, 0)

            assert _published(connection_info, DAY) == before
        finally:
            database.close()


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
            agreed = late_army == "u1x0-2x1"
            if agreed:
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
            assert _saved_disagreement(connection_info, OPPONENT) == [agreed]
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

            assert sweep_late_battles(
                database, now=boundary + timedelta(minutes=31)
            ) == (2, 0)

            assert _saved_disagreement(connection_info, OPPONENT) == [not agreed]
            assert _saved_disagreement(connection_info, TAG) == [not agreed]
        finally:
            database.close()


def test_agreement_restored_by_a_later_report_makes_the_first_result_current_again(
    database_url: str, archive_server
) -> None:
    # The attacker's day is published while both reports agree. A late defender
    # report disagrees, then a later one agrees again, so the attacker's day
    # returns to its first result and the next day must read it again.
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
            agreeing = {**_late_defense(), "armyShareCode": "u1x0-2x1"}
            _save_log(
                connection_info,
                archive_server,
                processor,
                key="agreeing-log",
                rows=[agreeing],
                observed_at=battle_time + timedelta(seconds=5),
            )
            next_day = DAY + timedelta(days=1)
            for day in (DAY, next_day):
                _process(
                    processor,
                    reconciliation_db.enqueue_reconciliation(
                        database,
                        player_tag=OPPONENT,
                        day_start=day,
                        now=day,
                        request_key=f"published-{day.isoformat()}",
                    ),
                )
            assert _saved_disagreement(connection_info, OPPONENT) == [False]
            first = _published(connection_info, DAY, OPPONENT)[1]
            boundary = DAY + timedelta(days=2)
            _finish_reset_sweep(connection_info, boundary)

            _save_log(
                connection_info,
                archive_server,
                processor,
                key="disagreeing-log",
                rows=[{**agreeing, "armyShareCode": "u3x0-2x1"}],
                observed_at=DAY + timedelta(days=1, minutes=20),
            )
            assert sweep_late_battles(
                database, now=boundary + timedelta(minutes=31)
            ) == (1, 0)
            assert _saved_disagreement(connection_info, OPPONENT) == [True]
            disputed = _published(connection_info, DAY, OPPONENT)[1]
            assert disputed != first
            assert _previous_day_version(connection_info, next_day, OPPONENT) == disputed

            _save_log(
                connection_info,
                archive_server,
                processor,
                key="agreeing-again-log",
                rows=[agreeing],
                observed_at=DAY + timedelta(days=1, minutes=40),
            )
            assert sweep_late_battles(
                database, now=boundary + timedelta(minutes=41)
            ) == (1, 0)
            assert _saved_disagreement(connection_info, OPPONENT) == [False]
            restored = _published(connection_info, DAY, OPPONENT)[1]
            assert restored != disputed
            with psycopg.connect(connection_info) as connection:
                # The same inputs as the first result, published again.
                assert connection.execute(
                    "SELECT count(DISTINCT input_hash) FROM ranked_day_versions "
                    "WHERE id = ANY(%s)",
                    ([first, restored],),
                ).fetchone()[0] == 1
            assert _previous_day_version(connection_info, next_day, OPPONENT) == restored

            # The restored result is current, so nothing is left to correct.
            assert sweep_late_battles(
                database, now=boundary + timedelta(minutes=51)
            ) == (0, 0)
            assert _published(connection_info, DAY, OPPONENT)[1] == restored
        finally:
            database.close()


# The selection before it was limited to the window and read from indexes.
_UNINDEXED_STALE_DAYS = """
WITH late_battle AS (
    SELECT battle.id, battle.ranked_day_start,
           battle.attacker_player_id, battle.defender_player_id,
           battle.disagreement_state = 'disagreement' AS disagreement
    FROM legend_battles AS battle
    JOIN battle_perspectives AS perspective ON perspective.battle_id = battle.id
    JOIN battle_evidence AS evidence ON evidence.id = perspective.evidence_id
    WHERE battle.ranked_day_start < %(boundary)s
      AND evidence.created_at
          >= battle.ranked_day_start + interval '24 hours' - interval '5 minutes'
    GROUP BY battle.id
), pair AS (
    SELECT CASE perspective.perspective
               WHEN 'attacker' THEN battle.attacker_player_id
               ELSE battle.defender_player_id
           END AS player_id,
           battle.ranked_day_start,
           jsonb_agg(jsonb_build_object(
               'source_evidence_id', perspective.evidence_id,
               'disagreement', battle.disagreement
           )) AS expected
    FROM late_battle AS battle
    JOIN battle_perspectives AS perspective ON perspective.battle_id = battle.id
    GROUP BY 1, 2
)
SELECT pair.player_id, pair.ranked_day_start
FROM pair
CROSS JOIN LATERAL (
    SELECT log.ranked_day_version_id
    FROM api_player_daily_logs AS log
    WHERE log.player_id = pair.player_id
      AND log.ranked_day_start = pair.ranked_day_start
    ORDER BY log.version DESC
    LIMIT 1
) AS published
LEFT JOIN ranked_day_versions AS version
  ON version.id = published.ranked_day_version_id
WHERE NOT coalesce(version.contribution_evidence @> pair.expected, false)
  AND NOT EXISTS (
      SELECT 1 FROM season_detail_retirements AS retirement
      WHERE retirement.status IN ('finalized', 'retired')
        AND (
            retirement.official_season_id = version.official_season_id
            OR (pair.ranked_day_start >= retirement.season_start
                AND pair.ranked_day_start < retirement.season_end)
        )
  )
ORDER BY pair.player_id, pair.ranked_day_start
"""

_UNINDEXED_OUTDATED_DAYS = """
SELECT saved.player_id, saved.ranked_day_start
FROM (
    SELECT DISTINCT log.player_id, log.ranked_day_start
    FROM api_player_daily_logs AS log
    WHERE log.ranked_day_start >= %(window_start)s
) AS saved
CROSS JOIN LATERAL (
    SELECT version.input_evidence -> 'previous_day' ->> 'version_id' AS previous_id
    FROM ranked_day_versions AS version
    WHERE version.player_id = saved.player_id
      AND version.ranked_day_start = saved.ranked_day_start
      AND version.reconciliation_rule_version = %(rule)s
    ORDER BY version.version DESC, version.id DESC
    LIMIT 1
) AS built
CROSS JOIN LATERAL (
    SELECT version.id
    FROM ranked_day_versions AS version
    WHERE version.player_id = saved.player_id
      AND version.ranked_day_start = saved.ranked_day_start - interval '24 hours'
      AND version.reconciliation_rule_version = %(rule)s
    ORDER BY version.version DESC, version.id DESC
    LIMIT 1
) AS previous
WHERE built.previous_id IS DISTINCT FROM previous.id::text
"""


def _selections(connection_info: str) -> tuple[dict, dict]:
    """Each Reset's selected player-days, by the old and the indexed queries."""
    unindexed: dict = {}
    indexed: dict = {}
    with psycopg.connect(connection_info) as connection:
        for days in range(1, 10):
            boundary = DAY + timedelta(days=days)
            parameters = {
                "boundary": boundary,
                "window_start": boundary - late_battle_sweep.WINDOW,
                "rule": RECONCILIATION_RULE_VERSION,
            }
            stale = connection.execute(_UNINDEXED_STALE_DAYS, parameters)
            outdated = connection.execute(_UNINDEXED_OUTDATED_DAYS, parameters)
            unindexed[boundary] = (
                [row for row in stale if row[1] >= parameters["window_start"]],
                sorted(outdated),
            )
            stale = connection.execute(late_battle_sweep._STALE_DAYS, parameters)
            outdated = connection.execute(late_battle_sweep._OUTDATED_DAYS, parameters)
            indexed[boundary] = (stale.fetchall(), sorted(outdated))
    return unindexed, indexed


def test_indexed_selection_picks_the_same_player_days_as_before(
    database_url: str, archive_server
) -> None:
    # Two players' late and on-time reports, a late report that changes their
    # agreement, and a later day built from an outdated previous day, checked
    # at every Reset from the late battle's day until it has left the window.
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
            _process(
                processor,
                reconciliation_db.enqueue_reconciliation(
                    database,
                    player_tag=OPPONENT,
                    day_start=DAY,
                    now=DAY,
                    request_key="published-opponent",
                ),
            )
            _save_log(
                connection_info,
                archive_server,
                processor,
                key="late-log",
                rows=[
                    {**_late_defense(), "armyShareCode": "u3x0-2x1"},
                    _on_time_defense(),
                ],
                observed_at=DAY + timedelta(days=1, minutes=20),
            )
            # Reports are saved when they are fetched. The battle's reports are
            # moved to just before, at, and after the 5-minute margin.
            with psycopg.connect(connection_info) as connection:
                connection.execute(
                    "UPDATE battle_evidence SET created_at = source_observed_at"
                )
            day_end = DAY + timedelta(days=1)
            for saved_at, late in [
                (day_end - timedelta(minutes=5, microseconds=1), False),
                (day_end - timedelta(minutes=5), True),
                (day_end + timedelta(minutes=20), True),
            ]:
                with psycopg.connect(connection_info) as connection:
                    connection.execute(
                        "UPDATE battle_evidence SET created_at = %s "
                        "WHERE battle_timestamp = %s",
                        (saved_at, battle_time),
                    )
                unindexed, indexed = _selections(connection_info)
                assert indexed == unindexed
                # Both players' late day is selected until it leaves the window.
                assert [len(stale) for stale, _ in indexed.values()] == (
                    [2] * 7 + [0] * 2 if late else [0] * 9
                )

            # An existing job corrects only the late day, so the next day is
            # left built from the late day's outdated version.
            _process(
                processor,
                reconciliation_db.enqueue_reconciliation(
                    database,
                    player_tag=TAG,
                    day_start=DAY,
                    now=DAY + timedelta(days=2),
                    request_key="existing-first-day-job",
                ),
            )
            unindexed, indexed = _selections(connection_info)
            assert indexed == unindexed
            assert [len(old) for _, old in indexed.values()] == [1] * 8 + [0]
        finally:
            database.close()
