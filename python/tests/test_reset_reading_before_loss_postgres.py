"""A Reset reading taken before the game applies the automatic defense loss.

The game applies the previous day's automatic defense loss about 7 to 13
minutes after the 05:00 UTC Reset, and the Reset pair is usually read before
that, so the reading sits exactly the calculated loss above the day's end.
The ended day is still Complete, with the loss calculated, and the next day
starts from the reading less that loss. On 2 October 2026 this explained
589 ended days and 728 of the 733 next days a later reading could check.
"""

from __future__ import annotations

from datetime import timedelta

import psycopg
from domain_test_support import domain_database, store_observation
from test_first_battle_log_postgres import LOSS, WIN, _log, _queued_priorities
from test_reconciliation_postgres import DAY_START, _profile
from test_reset_settlement_state_postgres import (
    BOUNDARIES,
    TAG,
    _process,
    _reset_work,
)

from clashlens import first_battle_log, reconciliation_db
from clashlens.db import PYTHON_BACKFILL_PRIORITY, Database
from clashlens.domain import ranked_day_for

# Three ordinary days: Monday 3 August to Thursday 6 August 2026.
DAY_A, DAY_B, DAY_C = BOUNDARIES["monday"], DAY_START, BOUNDARIES["ordinary"]
DAY_D = DAY_C + timedelta(days=1)


def _latest_days(connection_info: str) -> list[tuple]:
    with psycopg.connect(connection_info) as connection:
        return connection.execute(
            """
            SELECT DISTINCT ON (ranked_day_start)
                   state, confidence, start_trophies, final_trophies_before_reset,
                   next_start_trophies, automatic_defense_loss,
                   automatic_defense_evidence_state, formula_components,
                   failure_reasons
            FROM ranked_day_versions
            WHERE ranked_day_start IN (%s, %s)
            ORDER BY ranked_day_start, version DESC
            """,
            (DAY_B, DAY_C),
        ).fetchall()


def test_reading_before_the_loss_completes_the_day_and_settles_the_next_start(
    database_url: str, archive_server
) -> None:
    # Day A takes 8 defenses, so day B's missing defenses average LOSS each.
    day_a = [(DAY_A + timedelta(hours=hour), False) for hour in range(1, 9)]
    # Day B: 2 attacks and 1 defense, so 7 missing defenses cost 7 * LOSS.
    day_b = [
        (DAY_B + timedelta(hours=1), True),
        (DAY_B + timedelta(hours=2), True),
        (DAY_B + timedelta(hours=3), False),
    ]
    # Day C: one attack, no defenses, so no automatic loss of its own.
    day_c = [(DAY_C + timedelta(hours=1), True)]
    start_b = 6000 - 8 * LOSS
    end_b = start_b + 2 * WIN - LOSS - 7 * LOSS
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(
            connection_info, archive_server, DAY_A, profile=_profile(6000), log=_log()
        )
        jobs += _reset_work(
            connection_info, archive_server, DAY_B,
            profile=_profile(start_b), log=_log(*day_a),
        )
        # The Reset ending day B is read before the game takes the 7 * LOSS.
        jobs += _reset_work(
            connection_info, archive_server, DAY_C,
            profile=_profile(end_b + 7 * LOSS), log=_log(*day_b),
        )
        # By the next Reset the loss has landed and day C's attack is added.
        jobs += _reset_work(
            connection_info, archive_server, DAY_D,
            profile=_profile(end_b + WIN), log=_log(*day_c),
        )
        _process(connection_info, archive_server, jobs)
        day_b_row, day_c_row = _latest_days(connection_info)

    # Day B's saved next start is the reading less the loss it had not yet
    # applied, which is where day C starts.
    assert day_b_row[:7] == (
        "Complete", "inferred", start_b, end_b, end_b, 7 * LOSS, "calculated",
    )
    assert day_b_row[7]["next_start_reading_trophies"] == end_b + 7 * LOSS
    assert day_b_row[7]["unsettled_automatic_loss"] == 7 * LOSS
    assert day_b_row[8] == []
    # Day C starts from the reading less the loss it had not yet applied, and
    # its own end reading then proves it exactly.
    assert day_c_row[:5] == ("Complete", "exact", end_b, end_b + WIN, end_b + WIN)
    assert day_c_row[6] == "not_applicable"
    assert day_c_row[7]["start_reading_trophies"] == end_b + 7 * LOSS
    assert day_c_row[7]["start_unsettled_automatic_loss"] == 7 * LOSS
    assert day_c_row[8] == []


def test_later_reading_settles_a_reset_reading_missing_an_attack(
    database_url: str, archive_server
) -> None:
    # As #P20G0CUJY on 6 October 2026: the Reset reading leaves out the
    # ended day's attack gain, and a reading before any new-day battle has it.
    day_b = [(DAY_B + timedelta(hours=1), True)] + [
        (DAY_B + timedelta(hours=hour), False) for hour in range(2, 10)
    ]
    day_c = [(DAY_C + timedelta(hours=1), True)]
    start_b = 6000
    end_b = start_b + WIN - 8 * LOSS
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(
            connection_info, archive_server, DAY_B, profile=_profile(start_b), log=_log()
        )
        jobs += _reset_work(
            connection_info, archive_server, DAY_C,
            profile=_profile(end_b - WIN), log=_log(*day_b),
        )
        jobs += _reset_work(
            connection_info, archive_server, DAY_D,
            profile=_profile(end_b + WIN), log=_log(*day_c),
        )
        _process(connection_info, archive_server, jobs)
        assert [row[0] for row in _latest_days(connection_info)] == [
            "Inconsistent", "Inconsistent",
        ]

        # The later reading arrives; the recheck after the Reset uses it.
        _, profile_job = store_observation(
            connection_info, archive_server, occurrence_key="later-profile",
            endpoint="profile", body=_profile(end_b),
            observed_at=DAY_C + timedelta(minutes=10), normalized_tag=TAG,
        )
        with psycopg.connect(connection_info) as connection:
            player_id = connection.execute(
                "SELECT id FROM players WHERE normalized_tag = %s", (TAG,)
            ).fetchone()[0]
            reconciliation_db._enqueue_day_end_reconciliation(
                connection, player_id, ranked_day_for(DAY_B)
            )
        _process(connection_info, archive_server, [profile_job])
        assert [row[0] for row in _latest_days(connection_info)] == [
            "Complete", "Inconsistent",
        ]

        # Day C, saved before day B settled, needs the --mismatch batch.
        season = ranked_day_for(DAY_B).official_season_id
        database = Database(connection_info)
        try:
            preview = first_battle_log.requeue_overlap_gap(
                database, season, queue=False, max_jobs=100,
                reason="trophy_equation_mismatch", trigger="mismatch",
            )
            queued = first_battle_log.requeue_overlap_gap(
                database, season, queue=True, max_jobs=100,
                reason="trophy_equation_mismatch", trigger="mismatch",
            )
        finally:
            database.close()
        priorities = _queued_priorities(connection_info, "reconcile:mismatch:")
        _process(connection_info, archive_server, [])
        day_b_row, day_c_row = _latest_days(connection_info)

    assert (preview["players"], preview["queued"], preview["left_to_queue"]) == (1, 0, 1)
    assert (queued["queued"], queued["left_to_queue"]) == (1, 0)
    assert priorities == {PYTHON_BACKFILL_PRIORITY}

    assert day_b_row[:5] == ("Complete", "inferred", start_b, end_b, end_b)
    assert day_b_row[7]["next_start_reading_trophies"] == end_b - WIN
    assert day_b_row[7]["next_start_reading_correction"] == WIN
    assert day_b_row[8] == []
    assert day_c_row[:5] == ("Complete", "exact", end_b, end_b + WIN, end_b + WIN)
    assert day_c_row[7]["start_reading_correction"] == WIN
