from __future__ import annotations

import json
from datetime import timedelta

import pytest
from domain_test_support import domain_database, store_observation, text
from test_reconciliation_postgres import (
    BATTLE_FIXTURE,
    DAY_END,
    DAY_START,
    _processor,
    _profile,
    _seed_reset_collection_identity,
)

from clashlens import battle, reset_baselines

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
