from __future__ import annotations

import json
from datetime import timedelta

import pytest
import test_reset_settlement_proof_postgres as proof
from domain_test_support import domain_database, store_observation, text
from test_reconciliation_postgres import (
    BATTLE_FIXTURE,
    DAY_END,
    DAY_START,
    _processor,
    _profile,
    _seed_reset_collection_identity,
)

from clashlens import battle, ranked_day_inputs, reset_baselines
from clashlens.domain import ranked_day_for

# Live logs keep this row for days: no opponent, no battle.
NO_OPPONENT_ROW = {
    "battleType": "legend",
    "attack": False,
    "battleTime": 0,
    "battleTimestamp": "20260803T110000.000Z",
    "stars": 0,
    "destructionPercentage": 0,
    "opponentPlayerTag": None,
    "opponentName": None,
    "armyShareCode": None,
}


def _log(odd_row: dict, *, with_battle: bool) -> bytes:
    payload = json.loads(BATTLE_FIXTURE.read_bytes())
    # A real attack at 12:00 that moved no trophies.
    real = payload["items"][0] | {"stars": 0, "destructionPercentage": 0}
    payload["items"] = [real, odd_row] if with_battle else [odd_row]
    return json.dumps(payload).encode()


def _pair(connection_info, archive_server, key, boundary, profile, log):
    jobs = []
    observations = []
    for endpoint, body in (("profile", profile), ("battle_log", log)):
        observation, job = store_observation(
            connection_info,
            archive_server,
            occurrence_key=f"{key}-{endpoint}",
            endpoint=endpoint,
            body=body,
            observed_at=boundary,
            normalized_tag="#2PP",
        )
        observations.append(observation)
        jobs.append(job)
    _seed_reset_collection_identity(
        connection_info,
        key=key,
        boundary=boundary,
        profile_observation_id=observations[0],
        battle_observation_id=observations[1],
    )
    return jobs


def _run(database, processor, job_ids) -> None:
    for job_id in job_ids:
        result = processor.process_job(job_id, owner="test")
        assert result is not None and result.outcome in {
            "processed", "processed_with_gaps"
        }
    while True:
        with database.pool.connection() as connection:
            pending = connection.execute(
                "SELECT id FROM python_processing_jobs"
                " WHERE work_type = 'reconcile_ranked_day' AND status = 'pending'"
                " ORDER BY id LIMIT 1"
            ).fetchone()
        if pending is None:
            return
        processor.process_job(int(pending[0]), owner="reconcile")


def _resets(database) -> list[tuple[str, list[str]]]:
    with database.pool.connection() as connection:
        rows = connection.execute(
            """
            SELECT DISTINCT ON (boundary_at) state, failure_reasons
            FROM reset_baseline_evidence
            ORDER BY boundary_at, version DESC, id DESC
            """
        ).fetchall()
    return [(text(state), [text(r) for r in reasons]) for state, reasons in rows]


def _day(database):
    with database.pool.connection() as connection:
        state, reasons, attacks = connection.execute(
            """
            SELECT state, failure_reasons, attack_count FROM ranked_day_versions
            WHERE ranked_day_start = %s ORDER BY version DESC, id DESC LIMIT 1
            """,
            (DAY_START,),
        ).fetchone()
    return text(state), [text(reason) for reason in reasons], attacks


@pytest.mark.parametrize(
    ("odd_row", "counts"),
    [
        (NO_OPPONENT_ROW, False),
        # Any other row without an opponent may hide a battle.
        (NO_OPPONENT_ROW | {"destructionPercentage": 12}, True),
    ],
)
def test_no_opponent_row_leaves_days_and_resets_complete(
    database_url: str, archive_server, odd_row: dict, counts: bool
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = [
            *_pair(connection_info, archive_server, "start", DAY_START,
                   _profile(6000), _log(odd_row, with_battle=False)),
            *_pair(connection_info, archive_server, "end", DAY_END,
                   _profile(6000), _log(odd_row, with_battle=True)),
        ]
        database, processor = _processor(connection_info, archive_server)
        try:
            _run(database, processor, jobs)
            state, reasons, attacks = _day(database)
            if counts:
                gap = ["battle_log_processed_with_gaps"]
                assert [r[1][:1] for r in _resets(database)] == [gap, gap]
                assert state == "Malformed"
                assert "battle_log_row_gap" in reasons
            else:
                assert [r[0] for r in _resets(database)] == ["complete", "complete"]
                assert state != "Malformed"
                assert "battle_log_row_gap" not in reasons
                assert "malformed_evidence" not in reasons
            assert attacks == 1
        finally:
            database.close()


def test_no_opponent_rows_of_the_day_count_once_as_used_slots(
    database_url: str, archive_server
) -> None:
    defense = NO_OPPONENT_ROW | {"battleTimestamp": "20260804T110000.000Z"}
    attack = defense | {"attack": True, "battleTimestamp": "20260804T130000.000Z"}
    payload = json.loads(_log(defense, with_battle=True))
    payload["items"].append(attack)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = [
            # Yesterday's row, still in the log, is not today's slot.
            *_pair(connection_info, archive_server, "start", DAY_START,
                   _profile(6000), _log(NO_OPPONENT_ROW, with_battle=False)),
            store_observation(
                connection_info, archive_server, occurrence_key="middle",
                endpoint="battle_log", body=_log(defense, with_battle=False),
                observed_at=DAY_START + timedelta(hours=7), normalized_tag="#2PP",
            )[1],
            *_pair(connection_info, archive_server, "end", DAY_END,
                   _profile(6000), json.dumps(payload).encode()),
        ]
        database, processor = _processor(connection_info, archive_server)
        try:
            _run(database, processor, jobs)
            with database.pool.connection() as connection:
                player_id, evidence, defenses = connection.execute(
                    """
                    SELECT player_id, input_evidence, defense_count
                    FROM ranked_day_versions WHERE ranked_day_start = %s
                    ORDER BY version DESC, id DESC LIMIT 1
                    """,
                    (DAY_START,),
                ).fetchone()
                next_day = ranked_day_inputs.load_previous_day(
                    connection, player_id, ranked_day_for(DAY_END + timedelta(hours=1))
                )
            assert _day(database)[2] == 1
            assert defenses == 0
            assert evidence["zero_result_attack_slots"] == 1
            assert evidence["zero_result_defense_slots"] == 1
            assert next_day is not None and next_day.zero_result_defense_slots == 1
        finally:
            database.close()


def test_republication_recovers_resets_failed_by_no_opponent_rows(
    database_url: str, archive_server, monkeypatch
) -> None:
    rejected = json.loads(_profile(6000))
    rejected["currentLeagueSeasonId"] = 0
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = [
            *_pair(connection_info, archive_server, "opening",
                   DAY_START - timedelta(days=22), _profile(5000),
                   json.dumps({"items": []}).encode()),
            *_pair(connection_info, archive_server, "start", DAY_START,
                   _profile(6000), _log(NO_OPPONENT_ROW, with_battle=False)),
            store_observation(
                connection_info,
                archive_server,
                occurrence_key="middle",
                endpoint="battle_log",
                body=_log(NO_OPPONENT_ROW, with_battle=True),
                observed_at=DAY_START + timedelta(hours=7, minutes=30),
                normalized_tag="#2PP",
            )[1],
            *_pair(connection_info, archive_server, "end", DAY_END,
                   json.dumps(rejected).encode(),
                   _log(NO_OPPONENT_ROW | {"attack": True}, with_battle=True)),
        ]
        database, processor = _processor(connection_info, archive_server)
        try:
            # Saved before the row stopped counting as a gap.
            with monkeypatch.context() as before:
                before.setattr(battle, "is_no_opponent_row", lambda *_: False)
                before.setattr(battle, "no_opponent_row_sql", lambda *_: "false")
                _run(database, processor, jobs)
            assert [r[0] for r in _resets(database)] == [
                "complete", "failed", "failed"
            ]
            assert _day(database)[0] == "Malformed"

            report = reset_baselines.repair_current_season_reset_baselines(
                database, max_works=10
            )
            assert report["evaluated_count"] == 2
            assert report["failure_reasons"] == {"profile_invalid": 1}
            # The rejected profile stays rejected.
            assert _resets(database) == [
                ("complete", []), ("complete", []), ("failed", ["profile_invalid"])
            ]
            with database.pool.connection() as connection:
                kept = connection.execute(
                    "SELECT count(*) FROM battle_log_observation_source_rows"
                    " WHERE outcome = 'malformed_legend_row'"
                ).fetchone()[0]
            assert kept == 3, "each log keeps its rejected row as evidence"
            _run(database, processor, [])
            state, reasons, attacks = _day(database)
            assert state != "Malformed"
            assert "battle_log_row_gap" not in reasons
            assert attacks == 1
            again = reset_baselines.repair_current_season_reset_baselines(
                database, max_works=10
            )
            assert again["evaluated_count"] == 0
        finally:
            database.close()


def _log_results(database, observation_id: int) -> tuple[str, bool, int]:
    with database.pool.connection() as connection:
        outcome, gap = connection.execute(
            """
            SELECT outcome.outcome, log.has_row_gap
            FROM battle_log_observations AS log
            JOIN observation_processing_outcomes AS outcome
              ON outcome.observation_id = log.observation_id
             AND outcome.parser_version = log.parser_version
            WHERE log.observation_id = %s
            """,
            (observation_id,),
        ).fetchone()
        kept = connection.execute(
            "SELECT count(*) FROM battle_log_observation_source_rows AS row"
            " JOIN battle_log_observations AS log"
            "   ON log.id = row.battle_log_observation_id"
            " WHERE log.observation_id = %s AND row.outcome = 'malformed_legend_row'",
            (observation_id,),
        ).fetchone()[0]
    return text(outcome), gap, kept


def test_republication_rechecks_a_complete_reset_whose_delayed_log_had_only_no_opponent_gaps(
    database_url: str, archive_server, monkeypatch
) -> None:
    monkeypatch.setenv(proof.SWITCH, "true")
    with domain_database(database_url, include_coordinator=True) as connection_info:
        scenario = proof._scenario(
            connection_info, archive_server, check_rows=(NO_OPPONENT_ROW,)
        )
        # Saved before the row stopped counting as a gap.
        with monkeypatch.context() as before:
            before.setattr(battle, "is_no_opponent_row", lambda *_: False)
            before.setattr(battle, "no_opponent_row_sql", lambda *_: "false")
            proof._process(connection_info, archive_server,
                           [scenario[job] for job in proof.ORDERS["named_check_last"]])
        assert proof._verdict(connection_info)[0] != "settled"
        database, _ = _processor(connection_info, archive_server)
        try:
            with database.pool.connection() as connection:
                log_id = connection.execute(
                    "SELECT battle_log_observation_id FROM collector_work WHERE id = %s",
                    (scenario["work"],),
                ).fetchone()[0]
            assert _log_results(database, log_id) == ("processed_with_gaps", True, 1)
            assert _resets(database) == [("complete", [])]
            with database.pool.connection() as connection:
                assert ranked_day_inputs.load_reading(
                    database, connection, scenario["player"], log_id
                ).usable

            report = reset_baselines.repair_current_season_reset_baselines(
                database, max_works=10
            )

            assert report["evaluated_count"] == 1
            assert _log_results(database, log_id) == ("processed", False, 1)
            assert proof._verdict(connection_info)[:3] == (
                "settled", scenario["target"], []
            )
            again = reset_baselines.repair_current_season_reset_baselines(
                database, max_works=10
            )
            assert again["evaluated_count"] == 0
        finally:
            database.close()


def test_republication_clears_no_opponent_gaps_of_the_delayed_reset_log(
    database_url: str, archive_server, monkeypatch
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        delayed_log, delayed_job = store_observation(
            connection_info,
            archive_server,
            occurrence_key="delayed",
            endpoint="battle_log",
            body=_log(NO_OPPONENT_ROW, with_battle=False),
            observed_at=DAY_START + timedelta(minutes=24),
            normalized_tag="#2PP",
        )
        jobs = [
            *_pair(connection_info, archive_server, "opening",
                   DAY_START - timedelta(days=22), _profile(5000),
                   json.dumps({"items": []}).encode()),
            *_pair(connection_info, archive_server, "start", DAY_START,
                   _profile(6000), _log(NO_OPPONENT_ROW, with_battle=False)),
            delayed_job,
        ]
        database, processor = _processor(connection_info, archive_server)
        try:
            with monkeypatch.context() as before:
                before.setattr(battle, "is_no_opponent_row", lambda *_: False)
                before.setattr(battle, "no_opponent_row_sql", lambda *_: "false")
                _run(database, processor, jobs)
            with database.pool.connection() as connection:
                connection.execute(
                    """
                    WITH check_work AS (
                        INSERT INTO collector_work (
                            kind, lane, scope, player_id, normalized_tag, sweep_id,
                            due_at, coalescing_key, status, battle_log_status,
                            battle_log_observation_id
                        )
                        SELECT 'reset_settlement', 'ordinary', 'player',
                               settlement.player_id, '#2PP', settlement.sweep_id,
                               %(due)s, 'reset_settlement:delayed', 'complete',
                               'observed', %(log)s
                        FROM reset_boundary_settlements AS settlement
                        WHERE settlement.boundary_at = %(boundary)s
                        RETURNING id
                    )
                    UPDATE reset_boundary_settlements
                    SET delayed_work_id = (SELECT id FROM check_work)
                    WHERE boundary_at = %(boundary)s
                    """,
                    {"due": DAY_START + timedelta(minutes=20), "log": delayed_log,
                     "boundary": DAY_START},
                )
            assert _log_results(database, delayed_log) == (
                "processed_with_gaps", True, 1
            )

            report = reset_baselines.repair_current_season_reset_baselines(
                database, max_works=10
            )

            assert report["evaluated_count"] == 1
            assert _log_results(database, delayed_log) == ("processed", False, 1)
        finally:
            database.close()
