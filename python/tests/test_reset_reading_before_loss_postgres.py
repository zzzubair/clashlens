"""A Reset reading taken before the game applies the automatic defense loss.

The game applies the previous day's automatic defense loss about 7 to 13
minutes after the 05:00 UTC Reset, and the Reset pair is usually read before
that, so the reading sits exactly the calculated loss above the day's end.
The ended day is still Complete, with the loss calculated, and the next day
starts from the reading less that loss. On 2 October 2026 this explained
589 ended days and 728 of the 733 next days a later reading could check.
"""

from __future__ import annotations

import json
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

from clashlens import first_battle_log, ranked_day_inputs, reconciliation_db
from clashlens.db import PYTHON_BACKFILL_PRIORITY, Database
from clashlens.domain import ranked_day_for

# Three ordinary days: Monday 3 August to Thursday 6 August 2026.
DAY_A, DAY_B, DAY_C = BOUNDARIES["monday"], DAY_START, BOUNDARIES["ordinary"]
DAY_D = DAY_C + timedelta(days=1)


def _latest_days(connection_info: str, days: tuple = (DAY_B, DAY_C)) -> list[tuple]:
    with psycopg.connect(connection_info) as connection:
        return connection.execute(
            """
            SELECT DISTINCT ON (ranked_day_start)
                   state, confidence, start_trophies, final_trophies_before_reset,
                   next_start_trophies, automatic_defense_loss,
                   automatic_defense_evidence_state, formula_components,
                   failure_reasons
            FROM ranked_day_versions
            WHERE ranked_day_start = ANY(%s)
            ORDER BY ranked_day_start, version DESC
            """,
            (list(days),),
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


def _early_reading_days(
    connection_info: str, archive_server, later_profile: bytes,
    *, processed_first: tuple[tuple[str, bytes, timedelta], ...] = (),
) -> tuple[int, int]:
    """As #P20G0CUJY on 6 October 2026: the Reset reading ending day B leaves
    out its attack gain, and ``later_profile`` is read 10 minutes after that
    Reset, before any new-day battle; ``processed_first`` responses, (endpoint,
    body, read after that Reset), are processed before it. Return day B's
    start and calculated end."""
    day_b = [(DAY_B + timedelta(hours=1), True)] + [
        (DAY_B + timedelta(hours=hour), False) for hour in range(2, 10)
    ]
    day_c = [(DAY_C + timedelta(hours=1), True)]
    start_b = 6000
    end_b = start_b + WIN - 8 * LOSS
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
    jobs = [
        store_observation(
            connection_info, archive_server, occurrence_key=f"first-{index}",
            endpoint=endpoint, body=body, observed_at=DAY_C + after,
            normalized_tag=TAG,
        )[1]
        for index, (endpoint, body, after) in enumerate(processed_first)
    ]
    jobs.append(store_observation(
        connection_info, archive_server, occurrence_key="later-profile",
        endpoint="profile", body=later_profile,
        observed_at=DAY_C + timedelta(minutes=10), normalized_tag=TAG,
    )[1])
    _process(connection_info, archive_server, jobs)
    return start_b, end_b


def _day_end_recheck(connection_info: str, archive_server) -> None:
    with psycopg.connect(connection_info) as connection:
        player_id = connection.execute(
            "SELECT id FROM players WHERE normalized_tag = %s", (TAG,)
        ).fetchone()[0]
        reconciliation_db._enqueue_day_end_reconciliation(
            connection, player_id, ranked_day_for(DAY_B)
        )
    _process(connection_info, archive_server, [])


def test_later_reading_settles_a_reset_reading_missing_an_attack(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        start_b, end_b = _early_reading_days(
            connection_info, archive_server, _profile(6000 + WIN - 8 * LOSS)
        )
        # The later reading alone recalculates nothing.
        assert [row[0] for row in _latest_days(connection_info)] == [
            "Inconsistent", "Inconsistent",
        ]
        # The recheck after the Reset settles day B, which changes its next
        # start, so day C, saved before, is calculated again too.
        _day_end_recheck(connection_info, archive_server)
        day_b_row, day_c_row = _latest_days(connection_info)

    assert day_b_row[:5] == ("Complete", "inferred", start_b, end_b, end_b)
    assert day_b_row[7]["next_start_reading_trophies"] == end_b - WIN
    assert day_b_row[7]["next_start_reading_correction"] == WIN
    assert day_b_row[8] == []
    assert day_c_row[:5] == ("Complete", "exact", end_b, end_b + WIN, end_b + WIN)
    assert day_c_row[7]["start_reading_correction"] == WIN


def test_reading_soon_after_the_reset_is_kept_when_a_newer_one_came_first(
    database_url: str, archive_server
) -> None:
    # A new-day defense at 05:15 and a 05:29 profile after it are processed
    # before the 05:10 profile, the only reading that can settle day B.
    end_b = 6000 + WIN - 8 * LOSS
    with domain_database(database_url, include_coordinator=True) as connection_info:
        start_b, _ = _early_reading_days(
            connection_info, archive_server, _profile(end_b),
            processed_first=(
                ("battle_log", _log((DAY_C + timedelta(minutes=15), False)),
                 timedelta(minutes=20)),
                ("profile", _profile(end_b - LOSS), timedelta(minutes=29)),
            ),
        )
        _day_end_recheck(connection_info, archive_server)
        day_b_row, _ = _latest_days(connection_info)

    assert day_b_row[:5] == ("Complete", "inferred", start_b, end_b, end_b)
    assert day_b_row[7]["next_start_reading_correction"] == WIN


def test_recheck_refreshes_saved_days_until_one_stays_the_same(
    database_url: str, archive_server
) -> None:
    # Day B's Reset reading misses its attack gain. Day C, 8 defenses, then
    # starts too low and is Inconsistent though its own end reading is right;
    # day D, 1 defense, has no automatic loss without a complete day C.
    day_b = [(DAY_B + timedelta(hours=1), True)] + [
        (DAY_B + timedelta(hours=hour), False) for hour in range(2, 10)
    ]
    day_c = [(DAY_C + timedelta(hours=hour), False) for hour in range(1, 9)]
    day_d = [(DAY_D + timedelta(hours=1), False)]
    end_b = 6000 + WIN - 8 * LOSS
    end_c = end_b - 8 * LOSS
    end_d = end_c - LOSS - 7 * LOSS
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(
            connection_info, archive_server, DAY_B, profile=_profile(6000), log=_log()
        )
        for boundary, trophies, battles in (
            (DAY_C, end_b - WIN, day_b),
            (DAY_D, end_c, day_c),
            (DAY_D + timedelta(days=1), end_d, day_d),
        ):
            jobs += _reset_work(
                connection_info, archive_server, boundary,
                profile=_profile(trophies), log=_log(*battles),
            )
        _process(connection_info, archive_server, jobs)
        before = [row[0] for row in _latest_days(connection_info, (DAY_C, DAY_D))]
        _, profile_job = store_observation(
            connection_info, archive_server, occurrence_key="later-profile",
            endpoint="profile", body=_profile(end_b),
            observed_at=DAY_C + timedelta(minutes=10), normalized_tag=TAG,
        )
        _process(connection_info, archive_server, [profile_job])
        _day_end_recheck(connection_info, archive_server)
        day_c_row, day_d_row = _latest_days(connection_info, (DAY_C, DAY_D))

    assert before == ["Inconsistent", "Partial"]
    # Day C's next start stays the same but it is now complete, so day D can
    # take its automatic loss.
    assert day_c_row[:5] == ("Complete", "exact", end_b, end_c, end_c)
    assert day_d_row[:7] == (
        "Complete", "exact", end_c, end_d, end_d, 7 * LOSS, "confirmed",
    )


def _save_as_live(connection_info: str, *days) -> None:
    """Mark each day's latest saved result Live, as saved before it ended."""
    with psycopg.connect(connection_info) as connection:
        for day in days:
            connection.execute(
                """
                UPDATE ranked_day_versions SET state = 'Live'
                WHERE id = (
                    SELECT id FROM ranked_day_versions
                    WHERE ranked_day_start = %s ORDER BY version DESC LIMIT 1
                )
                """,
                (day,),
            )


def test_finishing_a_day_saved_live_refreshes_the_following_day(
    database_url: str, archive_server
) -> None:
    # Day B is still saved Live, as when its Reset calculation is delayed,
    # while day C was already saved from B's early Reset reading.
    with domain_database(database_url, include_coordinator=True) as connection_info:
        start_b, end_b = _early_reading_days(
            connection_info, archive_server, _profile(6000 + WIN - 8 * LOSS)
        )
        _save_as_live(connection_info, DAY_B)
        _day_end_recheck(connection_info, archive_server)
        day_b_row, day_c_row = _latest_days(connection_info)

    assert day_b_row[:5] == ("Complete", "inferred", start_b, end_b, end_b)
    assert day_c_row[:5] == ("Complete", "exact", end_b, end_b + WIN, end_b + WIN)


def test_following_day_saved_live_starts_from_the_finished_days_later_reading(
    database_url: str, archive_server
) -> None:
    # Day C was saved Live from B's early Reset reading while day B itself
    # was not yet finished; finishing B with the later reading refreshes C.
    with domain_database(database_url, include_coordinator=True) as connection_info:
        start_b, end_b = _early_reading_days(
            connection_info, archive_server, _profile(6000 + WIN - 8 * LOSS)
        )
        _save_as_live(connection_info, DAY_B, DAY_C)
        before = [(row[0], row[2]) for row in _latest_days(connection_info)]
        _day_end_recheck(connection_info, archive_server)
        day_b_row, day_c_row = _latest_days(connection_info)

    assert before == [("Live", start_b), ("Live", end_b - WIN)]
    assert day_b_row[:5] == ("Complete", "inferred", start_b, end_b, end_b)
    assert day_c_row[:5] == ("Complete", "exact", end_b, end_b + WIN, end_b + WIN)
    assert day_c_row[7]["start_reading_correction"] == WIN


def test_mismatch_batch_settles_days_saved_before_the_later_reading_rule(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        start_b, end_b = _early_reading_days(
            connection_info, archive_server, _profile(6000 + WIN - 8 * LOSS)
        )
        season = ranked_day_for(DAY_B).official_season_id
        database = Database(connection_info)
        try:
            preview = first_battle_log.requeue_overlap_gap(
                database, season, queue=False, max_jobs=100,
                condition=ranked_day_inputs.LATER_READING_DAY_SQL, trigger="mismatch",
            )
            queued = first_battle_log.requeue_overlap_gap(
                database, season, queue=True, max_jobs=100,
                condition=ranked_day_inputs.LATER_READING_DAY_SQL, trigger="mismatch",
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
    assert day_c_row[:5] == ("Complete", "exact", end_b, end_b + WIN, end_b + WIN)


def test_rejected_later_reading_does_not_settle_the_day(
    database_url: str, archive_server
) -> None:
    # The later profile has the right trophies, but reports Season ID 0, so
    # it is rejected and proves nothing.
    payload = json.loads(_profile(6000 + WIN - 8 * LOSS))
    payload["currentLeagueSeasonId"] = 0
    with domain_database(database_url, include_coordinator=True) as connection_info:
        _early_reading_days(
            connection_info, archive_server, json.dumps(payload).encode()
        )
        _day_end_recheck(connection_info, archive_server)
        rows = _latest_days(connection_info)

    assert [row[0] for row in rows] == ["Inconsistent", "Inconsistent"]
    assert "next_start_reading_correction" not in rows[0][7]


def _zero_defense_day_read_early(
    connection_info: str, archive_server, *, new_day_log: bytes | None = None
) -> tuple[int, int]:
    """As #9R2LRYY8V on 6 October 2026, but read early: day A takes 8
    defenses, day B none, and the Reset reading ending day B still shows its
    start; a reading 10 minutes later shows 8 * LOSS less, then the day-end
    recheck runs. ``new_day_log`` is saved 9 minutes after that Reset. Return
    day B's start and that start less 8 * LOSS."""
    day_a = [(DAY_A + timedelta(hours=hour), False) for hour in range(1, 9)]
    day_c = [(DAY_C + timedelta(hours=1), True)]
    start_b = 6000 - 8 * LOSS
    end_b = start_b - 8 * LOSS
    jobs = _reset_work(
        connection_info, archive_server, DAY_A, profile=_profile(6000), log=_log()
    )
    jobs += _reset_work(
        connection_info, archive_server, DAY_B,
        profile=_profile(start_b), log=_log(*day_a),
    )
    jobs += _reset_work(
        connection_info, archive_server, DAY_C,
        profile=_profile(start_b), log=_log(),
    )
    jobs += _reset_work(
        connection_info, archive_server, DAY_D,
        profile=_profile(end_b + WIN), log=_log(*day_c),
    )
    _process(connection_info, archive_server, jobs)
    assert [row[0] for row in _latest_days(connection_info)] == [
        "Complete", "Inconsistent",
    ]
    later_jobs = []
    if new_day_log is not None:
        later_jobs.append(store_observation(
            connection_info, archive_server, occurrence_key="new-day-log",
            endpoint="battle_log", body=new_day_log,
            observed_at=DAY_C + timedelta(minutes=9), normalized_tag=TAG,
        )[1])
    later_jobs.append(store_observation(
        connection_info, archive_server, occurrence_key="later-profile",
        endpoint="profile", body=_profile(end_b),
        observed_at=DAY_C + timedelta(minutes=10), normalized_tag=TAG,
    )[1])
    _process(connection_info, archive_server, later_jobs)
    _day_end_recheck(connection_info, archive_server)
    return start_b, end_b


def test_zero_defense_day_read_before_its_loss_is_charged_by_the_recheck(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        start_b, end_b = _zero_defense_day_read_early(connection_info, archive_server)
        day_b_row, day_c_row = _latest_days(connection_info)

    assert day_b_row[:7] == (
        "Complete", "inferred", start_b, end_b, end_b, 8 * LOSS, "calculated",
    )
    assert day_b_row[7]["next_start_reading_trophies"] == start_b
    assert day_b_row[7]["unsettled_automatic_loss"] == 8 * LOSS
    assert day_c_row[:5] == ("Complete", "exact", end_b, end_b + WIN, end_b + WIN)
    assert day_c_row[7]["start_unsettled_automatic_loss"] == 8 * LOSS


def test_unreadable_new_day_battle_before_the_later_reading_charges_nothing(
    database_url: str, archive_server
) -> None:
    # A new-day defense 8 minutes after the Reset, saved without its
    # direction, could explain the later reading's drop by itself.
    payload = json.loads(_log((DAY_C + timedelta(minutes=8), False)))
    payload["items"][0].pop("attack")
    with domain_database(database_url, include_coordinator=True) as connection_info:
        start_b, _ = _zero_defense_day_read_early(
            connection_info, archive_server, new_day_log=json.dumps(payload).encode()
        )
        day_b_row, _ = _latest_days(connection_info)

    assert day_b_row[0] == "Complete"
    assert day_b_row[4:6] == (start_b, None)
    assert "unsettled_automatic_loss" not in day_b_row[7]


def test_reset_reading_before_the_last_attack_landed_settles_both_days(
    database_url: str, archive_server
) -> None:
    # As #2GL8CJL on 7 October 2026: the Reset profile is read at 05:00, and
    # the ended day's attack reported at 05:02 only reaches it afterwards.
    # Day A, Complete, proves day B's start.
    day_a = [(DAY_A + timedelta(hours=hour), False) for hour in range(1, 9)]
    day_b = [(DAY_B + timedelta(hours=hour), False) for hour in range(1, 9)]
    day_b.append((DAY_C + timedelta(minutes=2), True))
    day_c = [(DAY_C + timedelta(hours=1), True)]
    start_b = 6000
    end_b = start_b + WIN - 8 * LOSS
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(
            connection_info, archive_server, DAY_A,
            profile=_profile(start_b + 8 * LOSS), log=_log(),
        )
        jobs += _reset_work(
            connection_info, archive_server, DAY_B,
            profile=_profile(start_b), log=_log(*day_a),
        )
        jobs += _reset_work(
            connection_info, archive_server, DAY_C,
            profile=_profile(end_b - WIN), log=_log(*day_b),
            log_at=DAY_C + timedelta(minutes=3),
        )
        jobs += _reset_work(
            connection_info, archive_server, DAY_D,
            profile=_profile(end_b + WIN), log=_log(*day_c),
        )
        _process(connection_info, archive_server, jobs)
        day_b_row, day_c_row = _latest_days(connection_info)

    assert day_b_row[:5] == ("Complete", "inferred", start_b, end_b, end_b)
    assert day_b_row[7]["next_start_reading_correction"] == WIN
    assert len(day_b_row[7]["next_start_battles_after_reading"]) == 1
    assert day_c_row[:5] == ("Complete", "exact", end_b, end_b + WIN, end_b + WIN)
