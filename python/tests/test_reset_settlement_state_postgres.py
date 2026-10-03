"""Every Reset is recorded as provisional, and no day result changes.

The profile read at the 05:00 UTC Reset can come before the game applies the
previous day's automatic defense loss, so a processed Reset pair proves the
responses arrived, not that trophies settled.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from domain_test_support import domain_database, store_observation
from test_domain_processing_postgres import _role_connection
from test_reconciliation_postgres import (
    BATTLE_FIXTURE,
    DAY_END,
    _battle_log,
    _processor,
    _profile,
)

from clashlens import reconciliation_db, reset_baselines, reset_settlement

TAG = "#2PP"
# The fixture profiles report the Season that ends at the August 10 Reset.
BOUNDARIES = {
    "ordinary": DAY_END,
    "monday": datetime(2026, 8, 3, 5, tzinfo=UTC),
    "season": datetime(2026, 8, 10, 5, tzinfo=UTC),
    "season_day_2": datetime(2026, 8, 11, 5, tzinfo=UTC),
}


def _eight_defenses() -> bytes:
    template = json.loads(BATTLE_FIXTURE.read_bytes())["items"][0]
    return json.dumps({"items": [
        {**template, "attack": False, "opponentPlayerTag": f"#{tag}PP"}
        for tag in "89QGRJCU"
    ]}).encode()


def _reset_work(connection_info, archive_server, boundary, *, profile=None,
                log=None, profile_status=200, status="complete",
                profile_at=None, log_at=None) -> list[int]:
    """Save a Reset sweep's responses for one player; return their jobs."""
    ids, jobs = {}, []
    for endpoint, body, http_status, observed_at in (
        ("profile", profile, profile_status, profile_at or boundary),
        ("battle_log", log, 200, log_at or boundary),
    ):
        if body is not None:
            ids[endpoint], job = store_observation(
                connection_info, archive_server,
                occurrence_key=f"{boundary.isoformat()}-{endpoint}",
                endpoint=endpoint, body=body, observed_at=observed_at,
                normalized_tag=TAG, http_status=http_status,
            )
            jobs.append(job)
    with psycopg.connect(connection_info) as connection:
        connection.execute(
            "INSERT INTO players (normalized_tag) VALUES (%s) ON CONFLICT DO NOTHING",
            (TAG,),
        )
        connection.execute(
            """
            WITH player AS (SELECT id FROM players WHERE normalized_tag = %(tag)s),
            sweep AS (
                INSERT INTO collector_reset_sweeps (
                    boundary_at, member_ids, membership_captured_at
                ) SELECT %(at)s, ARRAY[player.id], %(at)s FROM player RETURNING id
            )
            INSERT INTO collector_work (
                kind, lane, scope, player_id, normalized_tag, sweep_id, due_at,
                coalescing_key, status, profile_status, battle_log_status,
                profile_observation_id, battle_log_observation_id, completed_at
            ) SELECT 'reset_baseline', 'reset', 'player', player.id, %(tag)s,
                     sweep.id, %(at)s, 'reset-' || %(at)s::text, %(status)s,
                     %(profile_status)s, %(log_status)s, %(profile)s, %(log)s,
                     CASE WHEN %(status)s = 'complete' THEN %(at)s END
              FROM player, sweep
            """,
            {
                "tag": TAG, "at": boundary, "status": status,
                "profile": ids.get("profile"), "log": ids.get("battle_log"),
                "profile_status": "observed" if profile else "failed",
                "log_status": "observed" if log else "failed",
            },
        )
    return jobs


def _process(connection_info, archive_server, jobs) -> None:
    database, processor = _processor(connection_info, archive_server)
    try:
        for job_id in jobs:
            assert processor.process_job(job_id, owner=f"job-{job_id}") is not None
        if not jobs:
            reset_baselines.settle_failed_reset_work(database)
        while True:
            with database.pool.connection() as connection:
                pending = connection.execute(
                    "SELECT id FROM python_processing_jobs WHERE status = 'pending'"
                    " AND work_type = 'reconcile_ranked_day' ORDER BY id LIMIT 1"
                ).fetchone()
            if pending is None:
                return
            processor.process_job(int(pending[0]), owner="reconcile")
    finally:
        database.close()


def _rows(connection_info: str, query: str) -> list[tuple]:
    with psycopg.connect(connection_info) as connection:
        return connection.execute(query).fetchall()


BOUNDARY_ROWS = """
    SELECT settlement.boundary_at, settlement.state, settlement.selected_trophies,
           settlement.reasons, evidence.failure_reasons, settlement.change_number
    FROM reset_boundary_settlements AS settlement
    JOIN reset_baseline_evidence AS evidence
      ON evidence.id = settlement.early_baseline_id
    ORDER BY settlement.boundary_at
"""


@pytest.mark.parametrize("kind", sorted(BOUNDARIES))
def test_state_insert_does_not_change_existing_day_results(
    database_url: str, archive_server, monkeypatch, kind: str
) -> None:
    boundary = BOUNDARIES[kind]
    results = []
    for record in (False, True):
        with (
            domain_database(database_url, include_coordinator=True) as connection_info,
            monkeypatch.context() as patch,
        ):
            if not record:
                patch.setattr(reset_settlement, "record_provisional_boundary",
                              lambda *args, **kwargs: None)
            jobs = _reset_work(connection_info, archive_server,
                               boundary - timedelta(days=1), profile=_profile(5100),
                               log=_battle_log(empty=True))
            jobs += _reset_work(connection_info, archive_server, boundary,
                                profile=_profile(5140), log=_battle_log())
            _process(connection_info, archive_server, jobs)
            results.append((
                _rows(connection_info, """
                    SELECT log.ranked_day_start, log.version, version.state,
                           version.start_trophies, version.next_start_trophies,
                           version.result_hash
                    FROM api_player_daily_logs AS log
                    JOIN ranked_day_versions AS version
                      ON version.id = log.ranked_day_version_id ORDER BY 1, 2"""),
                _rows(connection_info, "SELECT work_type, deduplication_key"
                                       " FROM python_processing_jobs ORDER BY id"),
                _rows(connection_info, "SELECT count(*) FROM collector_work"),
            ))
            states = [row[1:3] for row in _rows(connection_info, BOUNDARY_ROWS)]
            assert states == ([("provisional", None)] * 2 if record else [])
    assert results[0][0], "the day was published"
    assert results[1] == results[0]


@pytest.mark.parametrize("trophies,log", [
    (5140, _battle_log(empty=True)), (5140, _eight_defenses()), (5000, _battle_log()),
])
def test_pair_complete_is_still_provisional(
    database_url: str, archive_server, trophies: int, log: bytes
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        _process(connection_info, archive_server, _reset_work(
            connection_info, archive_server, DAY_END,
            profile=_profile(trophies), log=log,
        ))
        # The profile and battle-log jobs each record the pair as they finish.
        assert [row[:5] for row in _rows(connection_info, BOUNDARY_ROWS)] == [
            (DAY_END, "provisional", None, [], [])
        ]


def test_missing_failed_and_unprocessed_endpoints_remain_distinct(
    database_url: str, archive_server
) -> None:
    cases = [  # profile, log, profile HTTP status, work status, jobs to run
        (None, None, 200, "failed", slice(0)),
        (_profile(5140), None, 200, "failed", slice(None)),
        (None, _battle_log(), 200, "failed", slice(None)),
        (_profile(5140), _battle_log(), 503, "complete", slice(None)),
        (b'{"tag": "#2PP"}', _battle_log(), 200, "complete", slice(None)),
        (_profile(5140), _battle_log(), 200, "complete", slice(1, None)),
    ]
    with domain_database(database_url, include_coordinator=True) as connection_info:
        for day, (profile, log, http, status, run) in enumerate(cases):
            jobs = _reset_work(
                connection_info, archive_server, DAY_END + timedelta(days=day),
                profile=profile, log=log, profile_status=http, status=status,
            )
            _process(connection_info, archive_server, jobs[run])
        rows = _rows(connection_info, BOUNDARY_ROWS)
    assert len(rows) == len(cases)
    assert {row[1:3] for row in rows} == {("provisional", None)}
    # Each boundary keeps its own pair's reasons, and no two are alike.
    assert all(row[3] == row[4] and row[3] for row in rows)
    assert len({json.dumps(row[3]) for row in rows}) == len(cases)


def test_boundary_constraints_and_real_worker_permissions(database_url: str) -> None:
    with domain_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            player = connection.execute(
                "INSERT INTO players (normalized_tag) VALUES (%s) RETURNING id", (TAG,)
            ).fetchone()[0]
        insert = ("INSERT INTO reset_boundary_settlements (player_id, boundary_at,"
                  " state, selected_trophies, proof_kind) VALUES (%s, %s, %s, %s, %s)")
        worker = _role_connection(connection_info, "clashlens_python_worker")
        worker.autocommit = True
        worker.execute(insert, (player, DAY_END, "provisional", None, None))
        worker.execute("UPDATE reset_boundary_settlements SET state = 'unresolved'")
        for values, error in (
            ((player, DAY_END + timedelta(minutes=1), "provisional", None, None),
             psycopg.errors.CheckViolation),
            ((player, DAY_END + timedelta(days=1), "settled", 5140, "observed_adjustment"),
             psycopg.errors.CheckViolation),
            ((player, DAY_END + timedelta(days=1), "unresolved", 5140, None),
             psycopg.errors.CheckViolation),
            ((player, DAY_END + timedelta(days=1), "provisional", None, "guess"),
             psycopg.errors.CheckViolation),
            ((player, DAY_END, "provisional", None, None), psycopg.errors.UniqueViolation),
        ):
            with pytest.raises(error):
                worker.execute(insert, values)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            worker.execute("DELETE FROM reset_boundary_settlements")
        worker.close()
        for role in ("clashlens_collector", "clashlens_python_api"):
            with _role_connection(connection_info, role) as other:
                with pytest.raises(psycopg.errors.InsufficientPrivilege):
                    other.execute("UPDATE reset_boundary_settlements SET reasons = '[]'")


def test_concurrent_provisional_recording_and_replay_preserve_verdict(
    database_url: str,
) -> None:
    with domain_database(database_url) as connection_info:
        with psycopg.connect(connection_info) as connection:
            player = connection.execute(
                "INSERT INTO players (normalized_tag) VALUES (%s) RETURNING id", (TAG,)
            ).fetchone()[0]
            sweep = connection.execute(
                "INSERT INTO collector_reset_sweeps (boundary_at, member_ids,"
                " membership_captured_at) VALUES (%s, %s, %s) RETURNING id",
                (DAY_END, [player], DAY_END),
            ).fetchone()[0]

        def record(baseline: int = 7, connection_info=connection_info) -> None:
            with psycopg.connect(connection_info) as connection:
                reset_settlement.record_provisional_boundary(
                    connection, player_id=player, boundary_at=DAY_END,
                    sweep_id=sweep, early_baseline_id=baseline,
                    early_state="complete", reasons=[],
                )

        writers = [threading.Thread(target=record) for _ in range(2)]
        for writer in writers:
            writer.start()
        for writer in writers:
            writer.join()
        state = "SELECT state, change_number, early_baseline_id FROM reset_boundary_settlements"
        assert _rows(connection_info, state) == [("provisional", 1, 7)]
        record()  # The same evidence again is not a new change.
        assert _rows(connection_info, state) == [("provisional", 1, 7)]
        record(8)
        assert _rows(connection_info, state) == [("provisional", 2, 8)]
        # A synthetic later verdict: replaying the Reset pair must not undo it.
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                "UPDATE reset_boundary_settlements SET state = 'settled',"
                " selected_trophies = 5106, proof_kind = 'calculated_target',"
                " proof_rule_version = 'test', proof_fingerprint = 'f',"
                " proof_json = '{\"test\": true}', change_number = 3"
            )
        record(9)
        assert _rows(connection_info, state) == [("settled", 3, 8)]


def _season_profile(trophies: int, season_id: int) -> bytes:
    payload = json.loads(_profile(trophies))
    payload["currentLeagueSeasonId"] = season_id
    payload["previousLeagueSeasonId"] = season_id - 28 * 24 * 60 * 60
    return json.dumps(payload).encode()


OLD_SEASON, NEW_SEASON = 1783918800, 1786338000  # Seasons around August 10.


@pytest.mark.parametrize("kind,season_id,accepted", [
    ("season", OLD_SEASON, False),
    ("season", NEW_SEASON, True),
    ("season_day_2", OLD_SEASON, False),
    ("monday", OLD_SEASON, True),
])
def test_reset_start_needs_a_profile_naming_the_resets_season(
    database_url: str, archive_server, kind: str, season_id: int, accepted: bool
) -> None:
    boundary = BOUNDARIES[kind]
    trophies = 5000 if season_id == NEW_SEASON else 6400
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(connection_info, archive_server,
                           boundary - timedelta(days=1), profile=_profile(6400),
                           log=_battle_log(empty=True))
        jobs += _reset_work(connection_info, archive_server, boundary,
                            profile=_season_profile(trophies, season_id),
                            log=_battle_log(empty=True))
        _process(connection_info, archive_server, jobs)
        # The opening day is otherwise reconciled when its battles arrive.
        database, processor = _processor(connection_info, archive_server)
        try:
            job = reconciliation_db.enqueue_reconciliation(
                database, player_tag=TAG, day_start=boundary, now=boundary,
                request_key="opening",
            )
            assert processor.process_job(job, owner="opening") is not None
        finally:
            database.close()
        days = _rows(connection_info, """
            SELECT DISTINCT ON (ranked_day_start) ranked_day_start,
                   official_season_id, season_day_number, start_trophies,
                   next_start_trophies
            FROM ranked_day_versions ORDER BY ranked_day_start, version DESC""")
        evidence = _rows(connection_info, """
            SELECT DISTINCT ON (boundary_at) state,
                   profile_observation_id IS NOT NULL
            FROM reset_baseline_evidence
            WHERE boundary_at = (SELECT max(boundary_at) FROM reset_baseline_evidence)
            ORDER BY boundary_at, version DESC""")
        settlements = {row[1:3] for row in _rows(connection_info, BOUNDARY_ROWS)}
    start = trophies if accepted else None
    previous_day = boundary - timedelta(days=1)
    by_start = {row[0]: row[1:] for row in days}
    assert by_start[previous_day][3] == start
    assert by_start[boundary][2] == start
    if kind == "season":
        # The calendar names both days even while the profile is old.
        assert by_start[previous_day][:2] == (str(OLD_SEASON), 28)
        assert by_start[boundary][:2] == (str(NEW_SEASON), 1)
    # The raw reading is kept, and no Reset is settled from it.
    assert evidence == [("complete", True)]
    assert settlements == {("provisional", None)}


def _conflicting_profile(kind: str) -> bytes:
    payload = json.loads(_profile(5000))
    if kind == "season_zero":  # As the game sometimes sends with 5,000.
        payload["currentLeagueSeasonId"] = 0
    else:  # A tier name we do not recognise, under a valid Season.
        payload["leagueTier"]["name"] = "Legend One"
    return json.dumps(payload).encode()


@pytest.mark.parametrize("kind,conflict", [
    ("ordinary", "season_zero"),
    ("season", "season_zero"),
    ("ordinary", "tier_name"),
])
def test_rejected_reset_profile_gives_no_start(
    database_url: str, archive_server, kind: str, conflict: str
) -> None:
    boundary = BOUNDARIES[kind]
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(connection_info, archive_server,
                           boundary - timedelta(days=1), profile=_profile(6400),
                           log=_battle_log(empty=True))
        jobs += _reset_work(connection_info, archive_server, boundary,
                            profile=_conflicting_profile(conflict),
                            log=_battle_log(empty=True))
        _process(connection_info, archive_server, jobs)
        # Both days are otherwise reconciled when their battles arrive.
        database, processor = _processor(connection_info, archive_server)
        try:
            for day_start in (boundary - timedelta(days=1), boundary):
                job = reconciliation_db.enqueue_reconciliation(
                    database, player_tag=TAG, day_start=day_start,
                    now=boundary, request_key=day_start.isoformat(),
                )
                assert processor.process_job(job, owner="day") is not None
        finally:
            database.close()
        days = _rows(connection_info, """
            SELECT DISTINCT ON (ranked_day_start) ranked_day_start,
                   start_trophies, next_start_trophies,
                   input_evidence -> 'start_baseline_evidence',
                   input_evidence -> 'end_baseline_evidence'
            FROM ranked_day_versions ORDER BY ranked_day_start, version DESC""")
        kept = _rows(connection_info, """
            SELECT profile.source_contract_state, profile.trophies
            FROM reset_baseline_evidence AS evidence
            JOIN player_profile_effects AS effect
              ON effect.observation_id = evidence.profile_observation_id
            JOIN player_profile_versions AS profile
              ON profile.id = effect.profile_version_id
            WHERE evidence.boundary_at = (
                SELECT max(boundary_at) FROM reset_baseline_evidence)""")
    by_start = {row[0]: row[1:] for row in days}
    ended, opened = by_start[boundary - timedelta(days=1)], by_start[boundary]
    # Neither day uses the rejected 5,000, and its Season is unknown rather
    # than waiting for this player's Season reset.
    assert ended[1] is None and opened[0] is None
    assert "season_reset_pending" not in ended[3]
    assert "season_reset_pending" not in opened[2]
    assert opened[2]["profile"]["trophies"] == 5000
    # The rejected reading itself stays saved as evidence.
    assert set(kept) == {("conflict", 5000)}


def test_reset_profile_read_after_the_first_battle_gives_no_start(
    database_url: str, archive_server
) -> None:
    # A 05:06 attack comes before a delayed 05:10 Reset profile of 6,040.
    attack_at = DAY_END + timedelta(minutes=6)
    log = json.loads(_battle_log())
    log["items"][0]["battleTimestamp"] = attack_at.strftime("%Y%m%dT%H%M%S.000Z")
    with domain_database(database_url, include_coordinator=True) as connection_info:
        jobs = _reset_work(connection_info, archive_server,
                           DAY_END - timedelta(days=1), profile=_profile(6000),
                           log=_battle_log(empty=True))
        jobs += _reset_work(connection_info, archive_server, DAY_END,
                            profile=_profile(6040), log=json.dumps(log).encode(),
                            profile_at=DAY_END + timedelta(minutes=10),
                            log_at=DAY_END + timedelta(minutes=11))
        _process(connection_info, archive_server, jobs)
        database, processor = _processor(connection_info, archive_server)
        try:
            for day_start in (DAY_END - timedelta(days=1), DAY_END):
                job = reconciliation_db.enqueue_reconciliation(
                    database, player_tag=TAG, day_start=day_start,
                    now=DAY_END + timedelta(hours=1),
                    request_key=day_start.isoformat(),
                )
                assert processor.process_job(job, owner="day") is not None
        finally:
            database.close()
        days = {row[0]: row[1:] for row in _rows(connection_info, """
            SELECT DISTINCT ON (ranked_day_start) ranked_day_start,
                   start_trophies, next_start_trophies
            FROM ranked_day_versions ORDER BY ranked_day_start, version DESC""")}
        evidence = _rows(connection_info, f"""
            SELECT evidence.profile_valid, evidence.failure_reasons,
                   profile.source_contract_state, profile.trophies
            FROM reset_baseline_evidence AS evidence
            JOIN player_profile_effects AS effect
              ON effect.observation_id = evidence.profile_observation_id
            JOIN player_profile_versions AS profile
              ON profile.id = effect.profile_version_id
            WHERE evidence.boundary_at = '{DAY_END.isoformat()}'
            ORDER BY evidence.version DESC, evidence.id DESC LIMIT 1""")
    # The accepted 6,040 is kept as evidence but starts neither day.
    assert evidence == [
        (False, ["profile_after_first_event"], "accepted", 6040)
    ]
    assert days[DAY_END - timedelta(days=1)] == (6000, None)
    assert days[DAY_END][0] is None
