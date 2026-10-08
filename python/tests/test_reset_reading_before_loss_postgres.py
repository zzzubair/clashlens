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
from datetime import datetime, timedelta

import psycopg
import pytest
from domain_test_support import domain_database, repair_season, store_observation
from test_first_battle_log_postgres import LOSS, WIN, _log, _queued_priorities
from test_reconciliation_postgres import BATTLE_FIXTURE, DAY_START, _processor, _profile
from test_reset_settlement_state_postgres import (
    BOUNDARIES,
    TAG,
    _process,
    _reset_work,
)

from clashlens import ranked_day_inputs, reconciliation_db, reset_settlement
from clashlens.boundary_manifest import board_proof_facts, reset_trophies
from clashlens.db import PYTHON_BACKFILL_PRIORITY
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
    body, read after that Reset), are processed before it. They are processed
    alone: what they queue runs with the next work. Return day B's start and
    calculated end."""
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
    database, processor = _processor(connection_info, archive_server)
    try:
        for job in jobs:
            assert processor.process_job(job, owner=f"job-{job}") is not None
    finally:
        database.close()
    return start_b, end_b


def _queue_day_end_recheck(connection_info: str) -> None:
    with psycopg.connect(connection_info) as connection:
        player_id = connection.execute(
            "SELECT id FROM players WHERE normalized_tag = %s", (TAG,)
        ).fetchone()[0]
        reconciliation_db._enqueue_day_end_reconciliation(
            connection, player_id, ranked_day_for(DAY_B)
        )


def _day_end_recheck(connection_info: str, archive_server) -> None:
    _queue_day_end_recheck(connection_info)
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


def test_season_repair_settles_days_saved_before_the_later_reading_rule(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        start_b, end_b = _early_reading_days(
            connection_info, archive_server, _profile(6000 + WIN - 8 * LOSS)
        )
        season = ranked_day_for(DAY_B).official_season_id
        preview, queued = repair_season(connection_info, season)
        priorities = _queued_priorities(connection_info, "reconcile:season-repair:")
        _process(connection_info, archive_server, [])
        day_b_row, day_c_row = _latest_days(connection_info)

    assert (preview["players"], preview["left_to_queue"]) == (1, 1)
    assert (queued["queued"], queued["left_to_queue"]) == (1, 0)
    assert priorities == {PYTHON_BACKFILL_PRIORITY}
    assert day_b_row[:5] == ("Complete", "inferred", start_b, end_b, end_b)
    assert day_c_row[:5] == ("Complete", "exact", end_b, end_b + WIN, end_b + WIN)


@pytest.mark.parametrize(
    ("later_gain", "saved_late", "day_b_state", "day_c_state", "new_versions",
     "late_jobs"),
    [
        (WIN, False, "Inconsistent", "Partial", 2, 0),
        (0, False, "Complete", "Complete", 0, 0),
        (WIN, True, "Inconsistent", "Partial", 2, 1),
    ],
)
def test_later_reading_against_a_balanced_day_ends_its_proof(
    database_url: str, archive_server, later_gain, saved_late, day_b_state,
    day_c_state, new_versions, late_jobs,
) -> None:
    """Day B balances on its two Reset readings, but a profile read 10
    minutes after its end Reset, before any battle of day C, shows an attack
    more: both readings missed the same delayed credit. The recheck after
    the Reset makes day B Inconsistent, so day C, with one defense, no
    longer takes its automatic loss from day B's defenses. Saved only after
    that recheck ran, the profile queues one recalculation of day B, at
    backfill priority, with the same result. A later reading showing day
    B's end changes nothing and saves nothing new."""
    day_b = [(DAY_B + timedelta(hours=1), True)] + [
        (DAY_B + timedelta(hours=hour), False) for hour in range(2, 10)
    ]
    day_c = [(DAY_C + timedelta(hours=1), False)]
    end_b = 6000 + WIN - 8 * LOSS
    end_c = end_b - LOSS - 7 * LOSS
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(
            connection_info, archive_server, DAY_B, profile=_profile(6000), log=_log()
        )
        jobs += _reset_work(
            connection_info, archive_server, DAY_C,
            profile=_profile(end_b), log=_log(*day_b),
        )
        jobs += _reset_work(
            connection_info, archive_server, DAY_D,
            profile=_profile(end_c), log=_log(*day_c),
        )
        _process(connection_info, archive_server, jobs)
        before = _latest_days(connection_info)
        count = (
            "SELECT count(*) FROM ranked_day_versions WHERE ranked_day_start = ANY(%s)"
        )
        with psycopg.connect(connection_info) as connection:
            saved = connection.execute(count, ([DAY_B, DAY_C],)).fetchone()[0]
        if saved_late:
            _day_end_recheck(connection_info, archive_server)
        else:
            # Runs right after the profile below is processed.
            _queue_day_end_recheck(connection_info)
        _, profile_job = store_observation(
            connection_info, archive_server, occurrence_key="later-profile",
            endpoint="profile", body=_profile(end_b + later_gain),
            observed_at=DAY_C + timedelta(minutes=10), normalized_tag=TAG,
        )
        _process(connection_info, archive_server, [profile_job])
        with psycopg.connect(connection_info) as connection:
            added = connection.execute(count, ([DAY_B, DAY_C],)).fetchone()[0] - saved
            queued = connection.execute(
                "SELECT count(*) FROM python_processing_jobs"
                " WHERE deduplication_key LIKE 'reconcile:later-reading:%'"
            ).fetchone()[0]
        priorities = _queued_priorities(connection_info, "reconcile:later-reading:")
        day_b_row, day_c_row = _latest_days(connection_info)

    assert [row[:5] for row in before] == [
        ("Complete", "exact", 6000, end_b, end_b),
        ("Complete", "exact", end_b, end_c, end_c),
    ]
    assert (day_b_row[0], day_c_row[0], added) == (day_b_state, day_c_state, new_versions)
    assert queued == late_jobs
    assert priorities == ({PYTHON_BACKFILL_PRIORITY} if late_jobs else set())
    if later_gain:
        assert day_b_row[8] == ["later_reading_contradicts"]
        assert day_c_row[6] == "unknown"


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


def test_battle_time_is_a_length_only_beside_a_battle_timestamp(
    database_url: str, archive_server
) -> None:
    # Older saved rows have no battleTimestamp and give the date in battleTime.
    day_b = [(DAY_B + timedelta(hours=hour), False) for hour in range(1, 4)]
    log = json.loads(_log(*day_b))
    for item, (at, _attack) in zip(log["items"][1:], day_b[1:], strict=True):
        del item["battleTimestamp"]
        item["battleTime"] = int(at.timestamp())
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(
            connection_info, archive_server, DAY_B, profile=_profile(6000), log=_log()
        )
        jobs += _reset_work(
            connection_info, archive_server, DAY_C,
            profile=_profile(6000 - 3 * LOSS), log=json.dumps(log).encode(),
        )
        _process(connection_info, archive_server, jobs)
        with psycopg.connect(connection_info) as connection:
            player_id = connection.execute(
                "SELECT id FROM players WHERE normalized_tag = %s", (TAG,)
            ).fetchone()[0]
            battles = ranked_day_inputs.load_contributions(
                connection, player_id, ranked_day_for(DAY_B)
            )

    assert sorted(
        (battle.battle_timestamp, battle.battle_seconds) for battle in battles
    ) == [(day_b[0][0], 120), (day_b[1][0], None), (day_b[2][0], None)]


def test_a_new_day_battle_saved_late_clears_a_later_readings_contradiction(
    database_url: str, archive_server
) -> None:
    """Day B balances on its Reset readings and passes its recheck. A
    recovered profile read 10 minutes after the Reset shows an attack more,
    so day B becomes Inconsistent. The battle log saved after it holds an
    attack of day C at 05:08: that profile came after day C's first battle,
    so it was never day B's later reading, and day B is Complete again."""
    day_b = [(DAY_B + timedelta(hours=1), True)] + [
        (DAY_B + timedelta(hours=hour), False) for hour in range(2, 10)
    ]
    end_b = 6000 + WIN - 8 * LOSS
    end_c = end_b - LOSS - 7 * LOSS
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(
            connection_info, archive_server, DAY_B, profile=_profile(6000), log=_log()
        )
        jobs += _reset_work(
            connection_info, archive_server, DAY_C,
            profile=_profile(end_b), log=_log(*day_b),
        )
        jobs += _reset_work(
            connection_info, archive_server, DAY_D,
            profile=_profile(end_c), log=_log((DAY_C + timedelta(hours=1), False)),
        )
        _process(connection_info, archive_server, jobs)
        _day_end_recheck(connection_info, archive_server)
        late = []
        for endpoint, body, after in (
            ("profile", _profile(end_b + WIN), timedelta(minutes=10)),
            ("battle_log", _log((DAY_C + timedelta(minutes=8), True)),
             timedelta(minutes=9)),
        ):
            _process(connection_info, archive_server, [store_observation(
                connection_info, archive_server, occurrence_key=f"late-{endpoint}",
                endpoint=endpoint, body=body, observed_at=DAY_C + after,
                normalized_tag=TAG,
            )[1]])
            late.append(_latest_days(connection_info)[0])
        with psycopg.connect(connection_info) as connection:
            queued = connection.execute(
                "SELECT count(*) FROM python_processing_jobs"
                " WHERE deduplication_key LIKE 'reconcile:later-reading:%'"
            ).fetchone()[0]

    assert [(row[0], row[8]) for row in late] == [
        ("Inconsistent", ["later_reading_contradicts"]), ("Complete", []),
    ]
    assert late[1][3] == end_b
    assert queued == 2


def _scored_log(*battles: tuple[datetime, bool, int, int]) -> bytes:
    """A battle log of Legend battles: (time, attack, stars, destruction)."""
    template = json.loads(BATTLE_FIXTURE.read_bytes())["items"][0]
    opponents = [f"#Q{a}{b}" for a in "28PY" for b in "28PYLGRJCUV"]
    return json.dumps({"items": [
        {**template, "attack": attack, "battleTime": 120,
         "battleTimestamp": at.strftime("%Y%m%dT%H%M%S.000Z"), "stars": stars,
         "destructionPercentage": destruction, "opponentPlayerTag": opponent}
        for (at, attack, stars, destruction), opponent in zip(battles, opponents)
    ]}).encode()


def test_season_repair_settles_an_early_reset_reading_and_its_board_entry(
    database_url: str, archive_server, monkeypatch
) -> None:
    """#GJ00QPR2Y on 7 October 2026, saved before later readings settled a
    day: it started at 5,088, gained 70 and lost 289, ending at 4,869, but
    its Reset reading of 4,839 at 05:02:11 came before its attack reported
    at 05:03:27 was credited. A reading at 05:11:33, before its first battle
    of the next day at 05:35:43, showed 4,869. The Season repair settles
    the day Complete, and its board entry is 4,869, proven."""
    day_b = [(DAY_B + timedelta(hours=1), True, 3, 100)] + [
        (DAY_B + timedelta(hours=hour), False, 3, 100) for hour in range(2, 9)
    ] + [
        (DAY_B + timedelta(hours=9), False, 1, 37),
        (DAY_C + timedelta(minutes=3, seconds=27), True, 2, 92),
    ]
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(
            connection_info, archive_server, DAY_B, profile=_profile(5088), log=_log()
        )
        jobs += _reset_work(
            connection_info, archive_server, DAY_C, profile=_profile(4839),
            log=_scored_log(*day_b), profile_at=DAY_C + timedelta(minutes=2, seconds=11),
            log_at=DAY_C + timedelta(minutes=4),
        )
        jobs += _reset_work(
            connection_info, archive_server, DAY_D, profile=_profile(4909),
            log=_scored_log((DAY_C + timedelta(minutes=35, seconds=43), True, 3, 100)),
        )
        _process(connection_info, archive_server, jobs)
        # Saved, as production's were, before a later reading settled a day.
        monkeypatch.setattr(reset_settlement, "profile_rechecks", lambda *_: ([], None))
        _process(connection_info, archive_server, [store_observation(
            connection_info, archive_server, occurrence_key="later-profile",
            endpoint="profile", body=_profile(4869),
            observed_at=DAY_C + timedelta(minutes=11, seconds=33), normalized_tag=TAG,
        )[1]])
        monkeypatch.undo()
        before = _latest_days(connection_info, (DAY_B,))[0]
        repair_season(connection_info, ranked_day_for(DAY_B).official_season_id)
        _process(connection_info, archive_server, [])
        after = _latest_days(connection_info, (DAY_B,))[0]
        with psycopg.connect(connection_info) as connection:
            player_id, version_id, reading_id, reading_at = connection.execute(
                """
                SELECT version.player_id, version.id, work.profile_observation_id,
                       observation.response_completed_at
                FROM ranked_day_versions AS version
                JOIN collector_work AS work ON work.player_id = version.player_id
                JOIN collector_reset_sweeps AS sweep
                  ON sweep.id = work.sweep_id AND sweep.boundary_at = %s
                JOIN collector_observations AS observation
                  ON observation.id = work.profile_observation_id
                WHERE version.ranked_day_start = %s
                ORDER BY version.version DESC LIMIT 1
                """,
                (DAY_C, DAY_B),
            ).fetchone()
            database, _ = _processor(connection_info, archive_server)
            try:
                board = reset_trophies(
                    connection, DAY_C,
                    {player_id: (version_id, reading_id, reading_at, 4839)},
                    board_proof_facts(database, connection, [version_id]),
                )
            finally:
                database.close()

    assert (before[0], before[8]) == ("Inconsistent", ["trophy_equation_mismatch"])
    assert after[:5] == ("Complete", "inferred", 5088, 4869, 4869)
    assert board == {player_id: (4869, True)}


def _balanced_days(connection_info: str, archive_server, day_c: list) -> int:
    """Day B, one attack and 8 defenses, balances on its Reset readings;
    ``day_c`` are day C's battles. Returns day B's end."""
    day_b = [(DAY_B + timedelta(hours=1), True)] + [
        (DAY_B + timedelta(hours=hour), False) for hour in range(2, 10)
    ]
    end_b = 6000 + WIN - 8 * LOSS
    jobs = _reset_work(
        connection_info, archive_server, DAY_B, profile=_profile(6000), log=_log()
    )
    jobs += _reset_work(
        connection_info, archive_server, DAY_C, profile=_profile(end_b), log=_log(*day_b),
    )
    jobs += _reset_work(
        connection_info, archive_server, DAY_D, profile=_profile(end_b), log=_log(*day_c),
    )
    _process(connection_info, archive_server, jobs)
    return end_b


def _board_entry(connection_info: str, archive_server, day: datetime) -> tuple[int, bool]:
    """The board entry of ``day``'s player from its end Reset reading, with
    the Reset proof as the evidence saved now gives it."""
    with psycopg.connect(connection_info) as connection:
        player_id, version_id, reading_id, reading_at, trophies = connection.execute(
            """
            SELECT version.player_id, version.id, work.profile_observation_id,
                   observation.response_completed_at,
                   (version.input_evidence ->> 'next_start_trophies')::integer
            FROM ranked_day_versions AS version
            JOIN collector_work AS work ON work.player_id = version.player_id
            JOIN collector_reset_sweeps AS sweep
              ON sweep.id = work.sweep_id AND sweep.boundary_at = version.ranked_day_end
            JOIN collector_observations AS observation
              ON observation.id = work.profile_observation_id
            WHERE version.ranked_day_start = %s
            ORDER BY version.version DESC LIMIT 1
            """,
            (day,),
        ).fetchone()
        database, _ = _processor(connection_info, archive_server)
        try:
            return reset_trophies(
                connection, day + timedelta(days=1),
                {player_id: (version_id, reading_id, reading_at, trophies)},
                board_proof_facts(database, connection, [version_id]),
            )[player_id]
        finally:
            database.close()


def test_a_profile_before_the_first_new_day_battle_is_never_skipped(
    database_url: str, archive_server
) -> None:
    """Day B balances and passes its recheck; day C's first battle is at
    06:30. A profile read at 06:40 is processed first, then a recovered one
    read at 06:00 showing an attack more than day B's end. The 06:40 reading
    came after that battle, so it cannot stand for the 06:00 one: the 06:00
    reading is applied, and day B and its board entry are no longer
    confirmed."""
    with domain_database(database_url, include_coordinator=True) as connection_info:
        end_b = _balanced_days(
            connection_info, archive_server, [(DAY_C + timedelta(minutes=90), False)]
        )
        _day_end_recheck(connection_info, archive_server)
        before = _board_entry(connection_info, archive_server, DAY_B)
        for minutes, trophies in ((100, end_b - LOSS), (60, end_b + WIN)):
            _process(connection_info, archive_server, [store_observation(
                connection_info, archive_server, occurrence_key=f"profile-{minutes}",
                endpoint="profile", body=_profile(trophies),
                observed_at=DAY_C + timedelta(minutes=minutes), normalized_tag=TAG,
            )[1]])
        day_b_row = _latest_days(connection_info, (DAY_B,))[0]
        after = _board_entry(connection_info, archive_server, DAY_B)

    assert before == (end_b, True)
    assert (day_b_row[0], day_b_row[8]) == ("Inconsistent", ["later_reading_contradicts"])
    assert after[1] is False


def test_a_late_battle_settles_a_day_by_its_later_reading(
    database_url: str, archive_server, monkeypatch
) -> None:
    """As #GJ00QPR2Y on 7 October 2026: day B is Complete from its Reset
    reading at 05:08. A battle log saved at 06:28 brings an attack of day B
    at 04:58 its earlier logs lacked; a reading at 05:11, before day C's
    first battle at 05:35, already showed it. The day stays Complete, and
    day C starts from its new end."""
    day_b = [(DAY_B + timedelta(hours=1), True)] + [
        (DAY_B + timedelta(hours=hour), False) for hour in range(2, 10)
    ]
    late = (DAY_C - timedelta(minutes=2), True)
    first_c = (DAY_C + timedelta(minutes=35), True)
    seen, end_b = 6000 + WIN - 8 * LOSS, 6000 + 2 * WIN - 8 * LOSS
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(
            connection_info, archive_server, DAY_B, profile=_profile(6000), log=_log()
        )
        jobs += _reset_work(
            connection_info, archive_server, DAY_C, profile=_profile(seen),
            log=_log(*day_b), profile_at=DAY_C + timedelta(minutes=8),
            log_at=DAY_C + timedelta(minutes=9),
        )
        _process(connection_info, archive_server, jobs)
        before = _latest_days(connection_info, (DAY_B,))[0]
        # Saved, as production's was, before a later reading settled a day.
        monkeypatch.setattr(reset_settlement, "profile_rechecks", lambda *_: ([], None))
        _process(connection_info, archive_server, [store_observation(
            connection_info, archive_server, occurrence_key="later-profile",
            endpoint="profile", body=_profile(end_b),
            observed_at=DAY_C + timedelta(minutes=11, seconds=33), normalized_tag=TAG,
        )[1]])
        monkeypatch.undo()
        _process(connection_info, archive_server, [store_observation(
            connection_info, archive_server, occurrence_key="late-log",
            endpoint="battle_log", body=_log(*day_b, late, first_c),
            observed_at=DAY_C + timedelta(hours=1, minutes=28), normalized_tag=TAG,
        )[1]])
        after = _latest_days(connection_info, (DAY_B,))[0]

    assert before[:5] == ("Complete", "exact", 6000, seen, seen)
    assert after[:5] == ("Complete", "inferred", 6000, end_b, end_b)


def test_a_changed_day_recalculates_its_following_days_in_order(
    database_url: str, archive_server
) -> None:
    """Day B's Reset reading missed its attack; days C and D were saved from
    it. A later reading settles day B, and its one recalculation saves days
    B, C and D, in that order. Two calculations of days B and C asked at
    once both finish."""
    import threading

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
        count = "SELECT coalesce(max(id), 0) FROM ranked_day_versions"
        with psycopg.connect(connection_info) as connection:
            saved = connection.execute(count).fetchone()[0]
        _process(connection_info, archive_server, [store_observation(
            connection_info, archive_server, occurrence_key="later-profile",
            endpoint="profile", body=_profile(end_b),
            observed_at=DAY_C + timedelta(minutes=10), normalized_tag=TAG,
        )[1]])
        with psycopg.connect(connection_info) as connection:
            order = [row[0] for row in connection.execute(
                "SELECT ranked_day_start FROM ranked_day_versions WHERE id > %s ORDER BY id",
                (saved,),
            ).fetchall()]
        pairs = [_processor(connection_info, archive_server) for _ in range(2)]
        try:
            asked = [
                reconciliation_db.enqueue_reconciliation(
                    pairs[0][0], player_tag=TAG, day_start=day, now=DAY_D,
                    request_key=f"both-{day.day}",
                )
                for day in (DAY_B, DAY_C)
            ]
            workers = [
                threading.Thread(
                    target=lambda processor=processor, job=job: processor.process_job(
                        job, owner=f"both-{job}"
                    )
                )
                for (_, processor), job in zip(pairs, asked)
            ]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=60)
            # A calculation that gave up waiting is retried, as the worker does.
            for job in asked:
                with psycopg.connect(connection_info) as connection:
                    retried = connection.execute(
                        "UPDATE python_processing_jobs SET due_at = clock_timestamp()"
                        " WHERE id = %s AND status <> 'complete' RETURNING id",
                        (job,),
                    ).fetchone()
                if retried:
                    pairs[0][1].process_job(job, owner=f"retry-{job}")
        finally:
            for database, _ in pairs:
                database.close()
        with psycopg.connect(connection_info) as connection:
            statuses = [row[0] for row in connection.execute(
                "SELECT status::text FROM python_processing_jobs WHERE id = ANY(%s)", (asked,),
            ).fetchall()]
        days = _latest_days(connection_info, (DAY_B, DAY_C, DAY_D))

    assert order == [DAY_B, DAY_C, DAY_D]
    assert statuses == ["complete", "complete"]
    assert [row[0] for row in days] == ["Complete", "Complete", "Complete"]


def _finishes_beside(connection_info, archive_server, job, first, then) -> None:
    """Process ``job`` while another transaction holds the lock ``first``
    takes, then, once the job waits, takes the one ``then`` takes, as a job
    holding the first would. Both finish."""
    import threading
    import time

    failures: list[BaseException] = []

    def run() -> None:
        try:
            _process(connection_info, archive_server, [job])
        except BaseException as error:  # noqa: BLE001
            failures.append(error)

    with psycopg.connect(connection_info) as other:
        first(other)
        worker = threading.Thread(target=run)
        worker.start()
        deadline = time.monotonic() + 30
        with psycopg.connect(connection_info, autocommit=True) as observer:
            while not observer.execute(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted"
            ).fetchone()[0]:
                assert time.monotonic() < deadline, "the job never waited"
                time.sleep(0.05)
        other.execute("SET LOCAL lock_timeout = '10s'")
        then(other)
        other.commit()
    worker.join(timeout=60)
    with psycopg.connect(connection_info) as connection:
        status = connection.execute(
            "SELECT status::text FROM python_processing_jobs WHERE id = %s", (job,)
        ).fetchone()[0]
    assert not worker.is_alive()
    assert failures == []
    assert status == "complete"


def test_a_profile_takes_the_resets_board_lock_before_its_proof_lock(
    database_url: str, archive_server
) -> None:
    """A profile read at 05:40, before day C's first battle, rechecks day B
    against the Reset's board and re-judges that Reset's settlement check.
    A battle log holding the Reset's publication lock and then taking that
    check's proof lock is not caught waiting on it: the profile takes the
    publication lock first, as every job does."""
    from clashlens.boundary import lock_boundary_publication

    with domain_database(database_url, include_coordinator=True) as connection_info:
        end_b = _balanced_days(
            connection_info, archive_server, [(DAY_C + timedelta(hours=1), False)]
        )
        player_id = _player_id(connection_info)
        _, profile_job = store_observation(
            connection_info, archive_server, occurrence_key="profile-0540",
            endpoint="profile", body=_profile(end_b),
            observed_at=DAY_C + timedelta(minutes=40), normalized_tag=TAG,
        )
        _finishes_beside(
            connection_info, archive_server, profile_job,
            lambda other: lock_boundary_publication(other, DAY_C),
            lambda other: reset_settlement._lock_reset(other, player_id, DAY_C),
        )


def test_a_days_recalculation_locks_every_day_before_any_board(
    database_url: str, archive_server
) -> None:
    """A recalculation of day B that changes it goes on to day C. A battle
    log holding day C's lock and then taking the publication lock of day
    B's Reset is not caught waiting on it: the recalculation takes both
    days' locks before any publication lock."""
    from clashlens.boundary import lock_boundary_publication

    with domain_database(database_url, include_coordinator=True) as connection_info:
        _early_reading_days(connection_info, archive_server, _profile(6000 + WIN - 8 * LOSS))
        player_id = _player_id(connection_info)
        database, _ = _processor(connection_info, archive_server)
        try:
            job = reconciliation_db.enqueue_reconciliation(
                database, player_tag=TAG, day_start=DAY_B, now=DAY_D,
                request_key="day-b",
            )
        finally:
            database.close()
        _finishes_beside(
            connection_info, archive_server, job,
            lambda other: ranked_day_inputs.lock_ranked_day(
                other, player_id, ranked_day_for(DAY_C)
            ),
            lambda other: lock_boundary_publication(other, DAY_C),
        )
        day_b_row = _latest_days(connection_info, (DAY_B,))[0]

    assert day_b_row[0] == "Complete"


def _player_id(connection_info: str) -> int:
    with psycopg.connect(connection_info) as connection:
        return connection.execute(
            "SELECT id FROM players WHERE normalized_tag = %s", (TAG,)
        ).fetchone()[0]


def test_an_older_official_total_never_blocks_the_saved_one(
    database_url: str, archive_server
) -> None:
    """A survivor's last day ends at its official total of F. A recovered
    older response naming F - 20 does not replace the saved history and
    queues nothing; a later correction to F - 20 is saved and the day ends
    at it."""
    from test_reset_settlement_state_postgres import (
        DAY_ROWS,
        NEW_SEASON,
        _battle_log,
        _rows,
        _season_profile,
    )

    from clashlens.league_history import LEAGUE_HISTORY_PARSER_VERSION

    boundary = BOUNDARIES["season"]
    last_day = boundary - timedelta(days=1)
    battles = [(last_day + timedelta(hours=1), True)] + [
        (last_day + timedelta(hours=hour), False) for hour in range(2, 10)
    ]
    final = 6000 + WIN - 8 * LOSS

    def history(hours: int, total: int) -> int:
        return store_observation(
            connection_info, archive_server, occurrence_key=f"league-history-{hours}",
            endpoint="league_history", normalized_tag=TAG,
            observed_at=boundary + timedelta(hours=hours),
            parser_version=LEAGUE_HISTORY_PARSER_VERSION,
            processing_version="clashlens-domain-processing-v1",
            domain_rule_version="clashlens-domain-rules-v1",
            body=json.dumps({"items": [{
                "leagueSeasonId": str(int(boundary.timestamp())),
                "leagueTrophies": total, "leagueTierId": 105000036,
                "placement": 10568, "attackWins": 1, "attackLosses": 0,
                "attackStars": 3, "defenseWins": 0, "defenseLosses": 8,
                "defenseStars": 16, "maxBattles": 8,
            }]}).encode(),
        )[1]

    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(connection_info, archive_server, last_day,
                           profile=_profile(6000), log=_battle_log(empty=True))
        jobs += _reset_work(connection_info, archive_server, boundary,
                            profile=_season_profile(5000, NEW_SEASON), log=_log(*battles))
        _process(connection_info, archive_server, jobs)
        ends = []
        for hours, total in ((8, final), (6, final - 20), (9, final - 20)):
            _process(connection_info, archive_server, [history(hours, total)])
            ends.append({row[0]: row for row in _rows(connection_info, DAY_ROWS)}[last_day])
        queued = _rows(
            connection_info,
            "SELECT count(*) FROM python_processing_jobs"
            " WHERE deduplication_key LIKE 'reconcile:official-final:%'",
        )[0][0]

    assert [(row[1], row[4]) for row in ends] == [
        ("Complete", final), ("Complete", final), ("Inconsistent", final - 20),
    ]
    assert queued == 2


def test_profiles_saved_before_the_log_of_the_first_new_day_battle_are_kept(
    database_url: str, archive_server
) -> None:
    """Day B balances and passes its recheck. Profiles read at 06:40 and
    06:00 are processed before the battle log showing day C's first battle
    at 06:30: until that battle is saved, neither may be skipped. The 06:00
    one, an attack above day B's end, is day B's later reading once the log
    arrives, so day B and its board entry are no longer confirmed."""
    with domain_database(database_url, include_coordinator=True) as connection_info:
        end_b = _balanced_days(connection_info, archive_server, [])
        _day_end_recheck(connection_info, archive_server)
        for minutes, trophies in ((100, end_b - LOSS), (60, end_b + WIN)):
            _process(connection_info, archive_server, [store_observation(
                connection_info, archive_server, occurrence_key=f"profile-{minutes}",
                endpoint="profile", body=_profile(trophies),
                observed_at=DAY_C + timedelta(minutes=minutes), normalized_tag=TAG,
            )[1]])
        _process(connection_info, archive_server, [store_observation(
            connection_info, archive_server, occurrence_key="day-c-log",
            endpoint="battle_log", body=_log((DAY_C + timedelta(minutes=90), False)),
            observed_at=DAY_C + timedelta(minutes=110), normalized_tag=TAG,
        )[1]])
        with psycopg.connect(connection_info) as connection:
            state, reasons, later = connection.execute(
                """
                SELECT state, failure_reasons,
                       (input_evidence -> 'later_next_start_reading' ->> 'trophies')::integer
                FROM ranked_day_versions WHERE ranked_day_start = %s
                ORDER BY version DESC LIMIT 1
                """,
                (DAY_B,),
            ).fetchone()
        board = _board_entry(connection_info, archive_server, DAY_B)

    assert (state, reasons, later) == (
        "Inconsistent", ["later_reading_contradicts"], end_b + WIN,
    )
    assert board[1] is False


def test_a_day_proven_by_two_readings_starts_the_next_day_after_its_loss(
    database_url: str, archive_server
) -> None:
    """Day B starts at Monday's raise to 5,000, which day A's 8 defenses
    ended below, so nothing proves day B's start; day B has an attack and a
    defense, so an automatic loss of 7 * LOSS, and is Inconsistent: its
    Reset reading at 05:01 is 40 above its calculated end before that loss.
    A reading at 05:25, before day C's first battle, is that Reset reading
    less the loss, so the two prove day B's end: the board shows the 05:01
    reading as proven, and day C, saved before the 05:25 reading, starts from
    that reading less the loss and balances. Day B stays Inconsistent, and
    the Season summary does not accept its end."""
    from clashlens.season_summaries import _project

    day_0 = DAY_A - timedelta(days=1)
    day_a = [(day_0 + timedelta(hours=hour), False) for hour in range(1, 9)]
    day_b = [(DAY_A + timedelta(hours=1), True), (DAY_A + timedelta(hours=3), False)]
    day_c = [(DAY_B + timedelta(hours=1), True)] + [
        (DAY_B + timedelta(hours=hour), False) for hour in range(2, 10)
    ]
    start_b = 5000
    reading = start_b + WIN - LOSS + 40
    proven = reading - 7 * LOSS
    end_c = proven + WIN - 8 * LOSS
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(
            connection_info, archive_server, day_0, profile=_profile(4900), log=_log()
        )
        jobs += _reset_work(
            connection_info, archive_server, DAY_A,
            profile=_profile(start_b), log=_log(*day_a),
        )
        jobs += _reset_work(
            connection_info, archive_server, DAY_B, profile=_profile(reading),
            log=_log(*day_b), profile_at=DAY_B + timedelta(minutes=1),
        )
        jobs += _reset_work(
            connection_info, archive_server, DAY_C, profile=_profile(end_c),
            log=_log(*day_c),
        )
        _process(connection_info, archive_server, jobs)
        before = _latest_days(connection_info, (DAY_B,))[0]
        _process(connection_info, archive_server, [store_observation(
            connection_info, archive_server, occurrence_key="later-profile",
            endpoint="profile", body=_profile(proven),
            observed_at=DAY_B + timedelta(minutes=25), normalized_tag=TAG,
        )[1]])
        day_b_row, day_c_row = _latest_days(connection_info, (DAY_A, DAY_B))
        board = _board_entry(connection_info, archive_server, DAY_A)
        with psycopg.connect(connection_info) as connection:
            summary = _project(
                _player_id(connection_info), ranked_day_for(DAY_A).official_season_id,
                connection,
            )

    assert (before[0], before[2]) == ("Inconsistent", reading)
    assert day_b_row[0] == "Inconsistent"
    assert board == (reading, True)
    assert (day_c_row[0], *day_c_row[2:5]) == ("Complete", proven, end_c, end_c)
    assert day_c_row[7]["start_reading_trophies"] == reading
    assert day_c_row[7]["start_unsettled_automatic_loss"] == 7 * LOSS
    assert {
        entry["season_day_number"]: entry["eod_state"]
        for entry in summary["daily_entries"]
    }[ranked_day_for(DAY_A).day_number] != "accepted"


def _check_on_the_day_before(
    connection_info: str, archive_server, readings: list[tuple[int, int]],
    recovered: tuple[str, bytes, int], *, day_end_waiting: bool = False,
) -> tuple[tuple, tuple, int]:
    """The settlement check of ``test_reset_settlement_proof_postgres``, with
    no settled Reset before it: the day before its ended day is Partial,
    with no start reading, 8 defenses and a Reset reading of 5,000 at 05:01.
    Profiles (minutes after that Reset, trophies) ``readings`` are saved
    before the check, the ``recovered`` (endpoint, body, minutes after that
    Reset) after it, while the day before's day-end calculation waits when
    ``day_end_waiting``. Return the check's verdict before and after
    ``recovered``, and its target."""
    from test_reset_settlement_proof_postgres import (
        ORDERS,
        RESET,
        START,
        _save,
        _scenario,
        _verdict,
    )
    from test_reset_settlement_proof_postgres import _process as _drain

    ended = RESET - timedelta(days=1)
    _drain(connection_info, archive_server, _reset_work(
        connection_info, archive_server, ended, profile=_profile(START),
        log=_log_before(ended), profile_at=ended + timedelta(minutes=1),
    ))
    _drain(connection_info, archive_server, [
        _save(connection_info, archive_server, "profile", _profile(trophies),
              ended + timedelta(minutes=minutes))[1]
        for minutes, trophies in readings
    ])
    scenario = _scenario(connection_info, archive_server, with_root=False)
    _drain(connection_info, archive_server,
           [scenario[job] for job in ORDERS["named_check_last"]])
    before = _verdict(connection_info)[:3]
    if day_end_waiting:
        with psycopg.connect(connection_info) as connection:
            reconciliation_db._enqueue_day_end_reconciliation(
                connection, scenario["player"], ranked_day_for(ended - timedelta(days=1))
            )
            assert connection.execute(
                "SELECT count(*) FROM python_processing_jobs_worker"
                " WHERE deduplication_key LIKE 'reconcile:day-end:%'"
                " AND state = 'pending'"
            ).fetchone()[0] == 1
    endpoint, body, minutes = recovered
    _drain(connection_info, archive_server, [
        _save(connection_info, archive_server, endpoint, body,
              ended + timedelta(minutes=minutes))[1]
    ])
    return before, _verdict(connection_info)[:3], scenario["target"]


def _log_before(at: datetime, *extra: dict) -> bytes:
    """The check's battle log as read before ``at``, with ``extra`` rows."""
    from test_reset_settlement_proof_postgres import RESET, _battles

    stamp = at.strftime("%Y%m%dT%H%M%S.000Z")
    return json.dumps({"items": [*extra, *(
        row for row in json.loads(_battles(RESET)[0])["items"]
        if row["battleTimestamp"] < stamp
    )]}).encode()


def test_a_recovered_reading_of_the_day_before_judges_the_check_again(
    database_url: str, archive_server, monkeypatch
) -> None:
    """A quiet reading of 5,000 at 05:25, recovered after the check found no
    start to root on, proves the Partial day before's end, and the check is
    judged again and settles on it. Saved before the check, it roots the
    check; a reading of 5,040 at 05:30, recovered after, takes that proof
    away, and the settled check is judged again and loses its root."""
    from test_reset_settlement_proof_postgres import START, SWITCH

    monkeypatch.setenv(SWITCH, "true")
    with domain_database(database_url, include_coordinator=True) as connection_info:
        missing, proven, target = _check_on_the_day_before(
            connection_info, archive_server, [], ("profile", _profile(START), 25)
        )
    with domain_database(database_url, include_coordinator=True) as connection_info:
        settled, withdrawn, _ = _check_on_the_day_before(
            connection_info, archive_server, [(25, START)],
            ("profile", _profile(START + 40), 30),
        )

    assert missing[0] == "unresolved" and "independent_root_missing" in missing[2]
    assert proven == ("settled", target, [])
    assert settled == ("settled", target, [])
    assert withdrawn[0] == "unresolved" and "independent_root_missing" in withdrawn[2]


@pytest.mark.parametrize("path", ["profile", "battle_log"])
def test_a_waiting_day_end_calculation_judges_the_check_again(
    database_url: str, archive_server, monkeypatch, path: str
) -> None:
    """The check settled on the Partial day before's two readings, 5,000 at
    05:01 and 05:25. While that day's day-end calculation waits, a recovered
    response takes the proof away: a reading of 5,040 at 05:30, or a battle
    log holding an attack at 05:20, before the 05:25 reading, which the
    check's own log lacked. The waiting calculation runs and judges the
    check again, which no longer settles: the reading leaves it no root."""
    from test_reset_settlement_proof_postgres import RESET, START, SWITCH, _row

    monkeypatch.setenv(SWITCH, "true")
    ended = RESET - timedelta(days=1)
    recovered = ("profile", _profile(START + 40), 30) if path == "profile" else (
        "battle_log",
        _log_before(ended + timedelta(minutes=21), _row(
            True, ended + timedelta(minutes=20), 3, 100, "#QYYYY",
        )),
        21,
    )
    with domain_database(database_url, include_coordinator=True) as connection_info:
        settled, withdrawn, target = _check_on_the_day_before(
            connection_info, archive_server, [(25, START)], recovered,
            day_end_waiting=True,
        )

    assert settled == ("settled", target, [])
    assert withdrawn[0] == "unresolved"
    if path == "profile":
        assert "independent_root_missing" in withdrawn[2]


def _day_c_entry(
    connection_info: str, archive_server, reading: int, day_c: list | None = None
) -> tuple[int, bool]:
    """Day C: eight defenses, then an attack stamped 04:34 that its 04:37
    reading of ``reading`` does not hold yet, unless ``day_c`` gives the
    battles its last log holds; its battle logs are continuous and its
    ending Reset profile failed. Return day C's board entry from that
    reading."""
    day_c = day_c if day_c is not None else [
        (DAY_C + timedelta(hours=hour), False) for hour in range(2, 10)
    ] + [(DAY_D - timedelta(minutes=26), True)]
    observation_id, job = store_observation(
        connection_info, archive_server, occurrence_key="board-reading",
        endpoint="profile", body=_profile(reading),
        observed_at=DAY_D - timedelta(minutes=23), normalized_tag=TAG,
    )
    _process(connection_info, archive_server, [
        *_reset_work(connection_info, archive_server, DAY_D, log=_log(*day_c)), job,
    ])
    with psycopg.connect(connection_info) as connection:
        player_id, version_id = connection.execute(
            "SELECT player_id, id FROM ranked_day_versions WHERE ranked_day_start = %s"
            " ORDER BY version DESC LIMIT 1",
            (DAY_C,),
        ).fetchone()
        read_at = connection.execute(
            "SELECT response_completed_at FROM collector_observations WHERE id = %s",
            (observation_id,),
        ).fetchone()[0]
        database, _ = _processor(connection_info, archive_server)
        try:
            return reset_trophies(
                connection, DAY_D,
                {player_id: (version_id, observation_id, read_at, reading)},
                board_proof_facts(database, connection, [version_id]),
            )[player_id]
        finally:
            database.close()


def test_a_reading_plus_its_battles_is_proven_only_from_a_proven_start(
    database_url: str, archive_server
) -> None:
    """Day B, with no start, has an attack at 04:58 its Reset reading of
    5,200 does not hold; a quiet reading at 06:00 shows it. Day B's end is
    not proven, so day C's start reading of 5,200 is not either: its 04:37
    reading, missing its own 04:34 attack, equals that start plus all its
    battles only because both miss an attack, and the entry stays
    uncertain. After a balanced day B, the same day C, read with every
    battle, is proven."""
    whole_c = WIN - 8 * LOSS
    with domain_database(database_url, include_coordinator=True) as connection_info:
        _process(connection_info, archive_server, _reset_work(
            connection_info, archive_server, DAY_C, profile=_profile(5200),
            log=_log((DAY_C - timedelta(minutes=2), True)),
        ))
        _process(connection_info, archive_server, [store_observation(
            connection_info, archive_server, occurrence_key="quiet-profile",
            endpoint="profile", body=_profile(5200 + WIN),
            observed_at=DAY_C + timedelta(hours=1), normalized_tag=TAG,
        )[1]])
        unproven = _day_c_entry(connection_info, archive_server, 5200 + whole_c)
    day_b = [(DAY_B + timedelta(hours=1), True)] + [
        (DAY_B + timedelta(hours=hour), False) for hour in range(2, 10)
    ]
    end_b = 6000 + WIN - 8 * LOSS
    with domain_database(database_url, include_coordinator=True) as connection_info:
        _process(connection_info, archive_server, [
            *_reset_work(
                connection_info, archive_server, DAY_B, profile=_profile(6000), log=_log()
            ),
            *_reset_work(
                connection_info, archive_server, DAY_C, profile=_profile(end_b),
                log=_log(*day_b),
            ),
        ])
        proven = _day_c_entry(connection_info, archive_server, end_b + whole_c)

    assert unproven == (5200 + whole_c, False)
    assert proven == (end_b + whole_c, True)


def _proven_partial_day_b(connection_info: str, archive_server, quiet: bool = True) -> list:
    """Day B, with no start reading, takes eight defenses, its battle logs
    reaching back before it; its Reset reading at 05:01 and, with ``quiet``,
    a quiet one at 05:25 both show 4,988, which proves its end. Return its
    battles."""
    day_b = [(DAY_B - timedelta(hours=1), False)] + [
        (DAY_B + timedelta(hours=hour), False) for hour in range(1, 9)
    ]
    _process(connection_info, archive_server, _reset_work(
        connection_info, archive_server, DAY_C, profile=_profile(4988),
        log=_log(*day_b), profile_at=DAY_C + timedelta(minutes=1),
    ))
    if quiet:
        _quiet_day_b(connection_info, archive_server)
    return day_b


def _quiet_day_b(connection_info: str, archive_server) -> None:
    _process(connection_info, archive_server, [store_observation(
        connection_info, archive_server, occurrence_key="quiet-day-b",
        endpoint="profile", body=_profile(4988),
        observed_at=DAY_C + timedelta(minutes=25), normalized_tag=TAG,
    )[1]])


def test_a_proven_partial_day_before_proves_the_boards_start(
    database_url: str, archive_server
) -> None:
    """Day C starts from Partial day B's proven end of 4,988, has no battle
    and no ending Reset reading, and reads 4,988 at 04:37: the board, which
    freezes day B's proof with its own, confirms it."""
    with domain_database(database_url, include_coordinator=True) as connection_info:
        day_b = _proven_partial_day_b(connection_info, archive_server)
        entry = _day_c_entry(connection_info, archive_server, 4988, day_b)
        day_b_row = _latest_days(connection_info, (DAY_B,))[0]

    assert day_b_row[0] == "Partial"
    assert entry == (4988, True)


@pytest.mark.parametrize("recovered", [False, True])
def test_a_proven_partial_day_before_lets_a_late_attack_settle_the_day(
    database_url: str, archive_server, recovered: bool
) -> None:
    """Day C starts from Partial day B's proven end of 4,988, takes eight
    defenses and an attack at 04:57 that its Reset reading at 05:00 does not
    hold yet. Its start is proven, so that attack settles the day: day C is
    Complete, not Inconsistent. Recovered after day C was saved Inconsistent
    from the same 4,988, B's quiet reading proves that start and calculates
    day C again, which records the proof its start read."""
    day_c = [(DAY_C + timedelta(hours=hour), False) for hour in range(2, 10)] + [
        (DAY_D - timedelta(minutes=3), True)
    ]
    end_c = 4988 + WIN - 8 * LOSS
    with domain_database(database_url, include_coordinator=True) as connection_info:
        _proven_partial_day_b(connection_info, archive_server, quiet=not recovered)
        _process(connection_info, archive_server, _reset_work(
            connection_info, archive_server, DAY_D, profile=_profile(end_c - WIN),
            log=_log(*day_c),
        ))
        if recovered:
            assert _latest_days(connection_info, (DAY_C,))[0][0] == "Inconsistent"
            _quiet_day_b(connection_info, archive_server)
        day_c_row = _latest_days(connection_info, (DAY_C,))[0]
        with psycopg.connect(connection_info) as connection:
            recorded = connection.execute(
                "SELECT input_evidence -> 'previous_day' ->> 'proven_end' FROM ranked_day_versions"
                " WHERE ranked_day_start = %s ORDER BY version DESC LIMIT 1",
                (DAY_C,),
            ).fetchone()[0]

    assert (day_c_row[0], *day_c_row[2:5]) == ("Complete", 4988, end_c, end_c)
    assert recorded == "4988"

