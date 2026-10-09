"""New evidence queues the ended days it can change (``queue_refresh``)."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import psycopg
from domain_test_support import domain_database, store_observation
from test_collector_db_postgres import NOW, _handoff, _hash, _player
from test_first_battle_log_postgres import LOSS, WIN, _log
from test_reconciliation_postgres import _profile
from test_reset_reading_before_loss_postgres import DAY_B, DAY_C, DAY_D
from test_reset_settlement_state_postgres import TAG, _process, _reset_work

from clashlens import queue_refresh, reconciliation_db
from clashlens.collector_db import CollectorDatabase
from clashlens.db import Database
from clashlens.domain import ranked_day_for
from clashlens.reconciliation import (
    DISPUTED_BATTLE_REASONS,
    RECONCILIATION_RULE_VERSION,
)


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


def _day(connection_info: str, tag: str, day_start) -> tuple:
    with psycopg.connect(connection_info) as connection:
        return connection.execute(
            """
            SELECT day.state, day.confidence, day.failure_reasons
            FROM ranked_day_versions AS day
            JOIN players AS player ON player.id = day.player_id
            WHERE player.normalized_tag = %s AND day.ranked_day_start = %s
            ORDER BY day.version DESC LIMIT 1
            """,
            (tag, day_start),
        ).fetchone()


def _calculate(connection_info: str, archive_server, tag: str, day_start) -> None:
    database = Database(connection_info)
    try:
        job = reconciliation_db.enqueue_reconciliation(
            database, player_tag=tag, day_start=day_start, now=DAY_D,
            request_key=f"test-{tag}",
        )
    finally:
        database.close()
    _process(connection_info, archive_server, [job])


def test_unchanged_covering_check_lets_a_later_reading_contradict_the_day(
    database_url: str, archive_server
) -> None:
    # Day B's 05:20 Reset reading proves it. A 05:40 reading 10 more cannot
    # contradict it while no battle log covers it; an unchanged 05:45 check of
    # the Reset log, which saves no log, covers it, and the day is judged.
    day_b = [(DAY_B + timedelta(hours=1), True)] + [
        (DAY_B + timedelta(hours=hour), False) for hour in range(2, 10)
    ]
    end_b = 6000 + WIN - 8 * LOSS
    reset_log = _log(*day_b)
    digest = hashlib.sha256(reset_log).hexdigest()
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(
            connection_info, archive_server, DAY_B, profile=_profile(6000), log=_log()
        )
        jobs += _reset_work(
            connection_info, archive_server, DAY_C, profile=_profile(end_b),
            log=reset_log, profile_at=DAY_C + timedelta(minutes=20),
        )
        jobs.append(store_observation(
            connection_info, archive_server, occurrence_key="later-profile",
            endpoint="profile", body=_profile(end_b + 10),
            observed_at=DAY_C + timedelta(minutes=40), normalized_tag=TAG,
        )[1])
        _process(connection_info, archive_server, jobs)
        _calculate(connection_info, archive_server, TAG, DAY_B)
        before = _day(connection_info, TAG, DAY_B)
        with psycopg.connect(connection_info) as connection:
            player_id, log_id = connection.execute(
                "SELECT player_id, id FROM collector_observations"
                " WHERE endpoint = 'battle_log' AND response_completed_at = %s",
                (DAY_C,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO collector_response_state (
                    scope, identity_key, endpoint, player_id, normalized_tag,
                    last_response_hash, last_content_fingerprint, last_occurrence_key,
                    last_applied_occurrence_key, last_seen_at, last_observation_id,
                    last_success_at
                ) VALUES ('player', %s, 'battle_log', %s, %s, %s, %s, 'reset-log',
                          'reset-log', %s, %s, %s)
                """,
                (TAG, player_id, TAG, digest, digest, DAY_C, log_id, DAY_C),
            )
        unchanged = CollectorDatabase(connection_info).record_response(_handoff(
            occurrence_key="covering-check", response_hash=digest, player_id=player_id,
            endpoint="battle_log", completed_at=DAY_C + timedelta(minutes=45),
        ))
        _process(connection_info, archive_server, [])
        after = _day(connection_info, TAG, DAY_B)

    assert unchanged.observation_id is None
    assert before[:2] == ("Complete", "exact")
    assert after[:2] == ("Inconsistent", "uncertain")
    assert "trophy_equation_mismatch" in after[2]


def test_late_opponent_report_settles_the_battles_day_for_both_players(
    database_url: str, archive_server
) -> None:
    # The player's log reports a 2-star, 60% defense against #GQPP on day B;
    # the attacker's log first reports 3 stars and 100%, so both days hold a
    # disputed battle. The attacker's corrected log, saved after day B ended,
    # agrees, and both players' day B is calculated again without it.
    at = f"{DAY_B + timedelta(hours=3):%Y%m%dT%H%M%S.000Z}"
    defense = {**json.loads(_log((DAY_B, False)))["items"][0],
               "opponentPlayerTag": "#GQPP", "battleTimestamp": at}
    attack = {**json.loads(_log((DAY_B, True)))["items"][0],
              "opponentPlayerTag": TAG, "battleTimestamp": at}
    corrected = {**attack, "stars": 2, "destructionPercentage": 60}
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(
            connection_info, archive_server, DAY_B, profile=_profile(6000), log=_log()
        )
        jobs.append(store_observation(
            connection_info, archive_server, occurrence_key="attacker-log",
            endpoint="battle_log", body=json.dumps({"items": [attack]}).encode(),
            observed_at=DAY_B + timedelta(hours=4), normalized_tag="#GQPP",
        )[1])
        jobs += _reset_work(
            connection_info, archive_server, DAY_C, profile=_profile(6000 - LOSS),
            log=json.dumps({"items": [defense]}).encode(),
        )
        _process(connection_info, archive_server, jobs)
        _calculate(connection_info, archive_server, "#GQPP", DAY_B)
        before = [_day(connection_info, tag, DAY_B)[2] for tag in (TAG, "#GQPP")]
        _, log_job = store_observation(
            connection_info, archive_server, occurrence_key="attacker-corrected",
            endpoint="battle_log", body=json.dumps({"items": [corrected]}).encode(),
            observed_at=DAY_C + timedelta(hours=1), normalized_tag="#GQPP",
        )
        _process(connection_info, archive_server, [log_job])
        after = [_day(connection_info, tag, DAY_B)[2] for tag in (TAG, "#GQPP")]

    assert all(set(reasons) & DISPUTED_BATTLE_REASONS for reasons in before)
    assert not any(set(reasons) & DISPUTED_BATTLE_REASONS for reasons in after)


def test_one_new_battle_in_a_full_log_queues_only_its_two_players(
    database_url: str, archive_server
) -> None:
    # A 50-row log repeats day B's 8 defenses, one against #8PP, whose day B
    # is saved, adds 41 battles of another mode and one new defense against
    # #GQPP: only the player's and #GQPP's days are queued.
    day_b = [(DAY_B + timedelta(hours=hour), False) for hour in range(1, 9)]
    full = json.loads(_log(*day_b, filler=[
        DAY_B - timedelta(hours=9 - index / 10) for index in range(41)
    ]))
    full["items"].append({
        **full["items"][0], "opponentPlayerTag": "#GQPP",
        "battleTimestamp": f"{DAY_B + timedelta(hours=20):%Y%m%dT%H%M%S.000Z}",
    })
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(
            connection_info, archive_server, DAY_B, profile=_profile(6000), log=_log()
        )
        jobs += _reset_work(
            connection_info, archive_server, DAY_C, profile=_profile(6000 - 8 * LOSS),
            log=_log(*day_b),
        )
        _process(connection_info, archive_server, jobs)
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "INSERT INTO players (normalized_tag, active) VALUES ('#GQPP', true)"
            )
        for tag in ("#8PP", "#GQPP"):
            _calculate(connection_info, archive_server, tag, DAY_B)
        _, log_job = store_observation(
            connection_info, archive_server, occurrence_key="full-log",
            endpoint="battle_log", body=json.dumps(full).encode(),
            observed_at=DAY_C + timedelta(hours=1), normalized_tag=TAG,
        )
        _process(connection_info, archive_server, [log_job])
        with psycopg.connect(connection_info) as connection:
            expected = {
                str(row[0]) for row in connection.execute(
                    "SELECT id FROM players WHERE normalized_tag IN (%s, '#GQPP')",
                    (TAG,),
                )
            }
        queued = {player for player, _ in _queued(connection_info, "reconcile:report:")}

    assert len(full["items"]) == 50
    assert queued == expected


def test_unchanged_full_log_check_covers_nothing(database_url: str, archive_server) -> None:
    # Day B's Reset reading names Season 0: it confirms day B but cannot
    # start day C. A later full 50-row log of another mode has the short Reset
    # log's fingerprint, so it is recorded unchanged, yet it can hide a
    # new-day battle: a 05:40 reading 40 more cannot contradict day B.
    day_b = [(DAY_B + timedelta(hours=1), True)] + [
        (DAY_B + timedelta(hours=hour), False) for hour in range(2, 10)
    ]
    end_b = 6000 + WIN - 8 * LOSS
    reset_log = _log(*day_b)
    digest = hashlib.sha256(reset_log).hexdigest()
    full = _log(filler=[DAY_C + timedelta(minutes=minute) for minute in range(50)])
    season_zero = json.loads(_profile(end_b))
    season_zero["currentLeagueSeasonId"] = 0
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(
            connection_info, archive_server, DAY_B, profile=_profile(6000), log=_log()
        )
        jobs += _reset_work(
            connection_info, archive_server, DAY_C,
            profile=json.dumps(season_zero).encode(), log=reset_log,
        )
        jobs.append(store_observation(
            connection_info, archive_server, occurrence_key="later-profile",
            endpoint="profile", body=_profile(end_b + 40),
            observed_at=DAY_C + timedelta(minutes=40), normalized_tag=TAG,
        )[1])
        _process(connection_info, archive_server, jobs)
        _calculate(connection_info, archive_server, TAG, DAY_B)
        with psycopg.connect(connection_info) as connection:
            player_id, log_id = connection.execute(
                "SELECT player_id, id FROM collector_observations"
                " WHERE endpoint = 'battle_log' AND response_completed_at = %s",
                (DAY_C,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO collector_response_state (
                    scope, identity_key, endpoint, player_id, normalized_tag,
                    last_response_hash, last_content_fingerprint, last_occurrence_key,
                    last_applied_occurrence_key, last_seen_at, last_observation_id,
                    last_success_at
                ) VALUES ('player', %s, 'battle_log', %s, %s, %s, %s, 'reset-log',
                          'reset-log', %s, %s, %s)
                """,
                (TAG, player_id, TAG, digest, digest, DAY_C, log_id, DAY_C),
            )
        unchanged = CollectorDatabase(connection_info).record_response(_handoff(
            occurrence_key="full-check", response_hash=hashlib.sha256(full).hexdigest(),
            content_fingerprint=digest, player_id=player_id, endpoint="battle_log",
            completed_at=DAY_C + timedelta(minutes=45),
        ))
        _process(connection_info, archive_server, [])
        queued = _queued(connection_info, "reconcile:check:")
        day = _day(connection_info, TAG, DAY_B)

    assert unchanged.observation_id is None
    assert queued == [(str(player_id), f"{DAY_B:%Y-%m-%dT%H:%M:%SZ}")]
    assert day[0] == "Complete" and "trophy_equation_mismatch" not in day[2]


def test_a_report_changed_back_queues_its_days_again(
    database_url: str, archive_server
) -> None:
    # Day C's attack is reported at 3 stars and 100%, then 2 stars and 60%,
    # then 3 stars and 100% again: each change from the report before it
    # queues day C and day B, the change back included.
    day_b = [(DAY_B + timedelta(hours=hour), False) for hour in range(1, 9)]
    attack = json.loads(_log((DAY_C + timedelta(hours=1), True)))["items"][0]
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
            log=json.dumps({"items": [attack]}).encode(),
        )
        _process(connection_info, archive_server, jobs)
        queued = []
        for minutes, stars, destruction in ((60, 2, 60), (70, 3, 100)):
            observation_id, log_job = store_observation(
                connection_info, archive_server, occurrence_key=f"log-{minutes}",
                endpoint="battle_log", body=json.dumps({"items": [
                    {**attack, "stars": stars, "destructionPercentage": destruction}
                ]}).encode(),
                observed_at=DAY_D + timedelta(minutes=minutes), normalized_tag=TAG,
            )
            _process(connection_info, archive_server, [log_job])
            queued.append(len(_queued(connection_info, f"reconcile:report:{observation_id}:")))

    assert queued == [2, 2]


def test_a_late_report_queues_the_previous_seasons_days_for_a_week(
    database_url: str,
) -> None:
    # A report of the previous Season's last day, saved 2 days into the new
    # Season, queues that day; saved 8 days in, when the previous Season no
    # longer takes corrections, it queues nothing.
    season_start = datetime(2026, 8, 10, 5, tzinfo=UTC)
    last_day = season_start - timedelta(days=1)
    with domain_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            connection.execute("SET LOCAL session_replication_role = replica")
            player, opponent = [row[0] for row in connection.execute(
                "INSERT INTO players (normalized_tag) VALUES ('#2PP'), ('#8QV') RETURNING id"
            ).fetchall()]
            connection.execute(
                """
                INSERT INTO ranked_day_versions (
                    player_id, ranked_day_start, ranked_day_end, official_season_id,
                    season_day_number, season_anchor_rule_version,
                    reconciliation_rule_version, input_hash, result_hash, version,
                    state, confidence
                ) VALUES (%s, %s, %s, %s, 28, 'test', %s, repeat('a', 64),
                          repeat('b', 64), 1, 'Complete', 'exact')
                """,
                (player, last_day, season_start,
                 ranked_day_for(last_day).official_season_id, RECONCILIATION_RULE_VERSION),
            )
            for observation_id in (101, 102):
                connection.execute(
                    """
                    WITH battle AS (
                        INSERT INTO legend_battles (ranked_day_start, attacker_player_id,
                                                    defender_player_id)
                        VALUES (%(day)s, %(player)s, %(opponent)s)
                        ON CONFLICT (ranked_day_start, attacker_player_id, defender_player_id)
                        DO UPDATE SET updated_at = clock_timestamp()
                        RETURNING id
                    )
                    INSERT INTO battle_evidence (
                        battle_id, source_row_id, observation_id, reporting_player_id,
                        perspective, battle_timestamp, stars, destruction_percentage,
                        army_share_code, attacker_gain, defender_loss,
                        trophy_rule_version, source_observed_at, parser_version
                    )
                    SELECT id, -%(observation)s, %(observation)s, %(player)s, 'attacker',
                           %(at)s, 3, 100 - %(observation)s + 101, '', 40, 40, 'test',
                           %(at)s, 'test'
                    FROM battle
                    """,
                    {"day": last_day, "player": player, "opponent": opponent,
                     "observation": observation_id, "at": last_day + timedelta(hours=3)},
                )
            for observation_id, days_in in ((101, 2), (102, 8)):
                queue_refresh.queue_for_battles(
                    connection, observation_id, (), season_start + timedelta(days=days_in)
                )
        queued = [len(_queued(connection_info, f"reconcile:report:{observation_id}:"))
                  for observation_id in (101, 102)]

    assert queued == [1, 0]


def test_a_late_reading_queues_its_own_ended_day_and_the_day_before(
    database_url: str, archive_server
) -> None:
    # A profile read at 05:20 on day C judges day B from its end and day C
    # during it. Processed after day C ended, it queues both.
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = []
        for day in (DAY_B, DAY_C, DAY_D):
            jobs += _reset_work(
                connection_info, archive_server, day, profile=_profile(6000), log=_log()
            )
        _process(connection_info, archive_server, jobs)
        for day in (DAY_B, DAY_C):
            database = Database(connection_info)
            try:
                job = reconciliation_db.enqueue_reconciliation(
                    database, player_tag=TAG, day_start=day, now=DAY_D,
                    request_key=f"test-{day:%d}",
                )
            finally:
                database.close()
            _process(connection_info, archive_server, [job])
        with psycopg.connect(connection_info) as connection:
            player_id = connection.execute(
                "SELECT id FROM players WHERE normalized_tag = %s", (TAG,)
            ).fetchone()[0]
            queue_refresh.queue_for_reading(
                connection, player_id, "late", DAY_C + timedelta(minutes=20)
            )
        queued = _queued(connection_info, "reconcile:late:")

    assert queued == [(str(player_id), f"{day:%Y-%m-%dT%H:%M:%SZ}") for day in (DAY_B, DAY_C)]
