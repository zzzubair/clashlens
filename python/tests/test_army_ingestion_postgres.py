from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from domain_test_support import domain_database, store_observation, text

from clashlens import (
    army_ingestion,
    battle_ingestion,
    boundary_publication,
    reset_baselines,
)
from clashlens.archive import S3ArchiveReader
from clashlens.db import Database
from clashlens.worker import ObservationProcessor


def _processor(connection_info: str, archive_server):
    database = Database(connection_info)
    processor = ObservationProcessor(
        database,
        S3ArchiveReader(
            endpoint=archive_server[0],
            bucket="evidence",
            access_key="test",
            secret_key="test",
            secure=False,
            allow_insecure_test_origin=True,
        ),
    )
    return database, processor


def _live_row(attack: bool, tag: str, code: str | None, ts: datetime):
    row = {
        "battleType": "legend",
        "attack": attack,
        "battleTime": ts.strftime("%Y%m%dT%H%M%S.000Z"),
        "stars": 3,
        "destructionPercentage": 100,
        "opponentPlayerTag": tag,
        "opponentName": "Opp",
        "opponentTownHallLevel": 17,
    }
    if code is not None:
        row["armyShareCode"] = code
    return row


def test_ranked_day_enqueue_lookup_uses_day_index(
    database_url: str,
) -> None:
    target_day = datetime(2026, 9, 3, 5, tzinfo=UTC)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        with psycopg.connect(connection_info) as connection:
            connection.execute(
                """
                INSERT INTO players (normalized_tag, active, eligibility_state)
                SELECT '#IDX' || g, false, 'unknown'
                FROM generate_series(1, 12500) AS values(g)
                """
            )
            connection.execute(
                """
                INSERT INTO ranked_day_versions (
                    player_id, ranked_day_start, ranked_day_end,
                    official_season_id, season_day_number,
                    season_anchor_rule_version, reconciliation_rule_version,
                    result_hash, version, state, confidence, coverage_complete
                )
                SELECT player.id,
                       %s - (day - 1) * interval '1 day',
                       %s - (day - 2) * interval '1 day',
                       'test-season', day,
                       'test-anchor', 'test-reconciliation',
                       md5(player.id::text) || md5(day::text), 1,
                       CASE WHEN day = 1 THEN 'Complete' ELSE 'Partial' END,
                       'exact', day = 1
                FROM players AS player
                CROSS JOIN generate_series(1, 28) AS days(day)
                """,
                (target_day, target_day),
            )
            connection.execute("DROP INDEX ranked_day_versions_completed_day_v1")
            before = connection.execute(
                """
                EXPLAIN (ANALYZE, FORMAT JSON)
                SELECT id, official_season_id
                FROM ranked_day_versions
                WHERE ranked_day_start = %s
                  AND state = 'Complete' AND coverage_complete
                ORDER BY id DESC
                LIMIT 1
                """,
                (target_day,),
            ).fetchone()[0][0]["Plan"]
            before_scans = []
            pending = [before]
            while pending:
                node = pending.pop()
                if node.get("Node Type") in {"Seq Scan", "Bitmap Heap Scan"}:
                    before_scans.append(node)
                pending.extend(node.get("Plans", []))
            assert before_scans and before_scans[0]["Actual Rows"] >= 12500
            connection.execute(
                """
                CREATE INDEX ranked_day_versions_completed_day_v1
                    ON ranked_day_versions (ranked_day_start, id DESC)
                    WHERE state = 'Complete' AND coverage_complete
                """
            )
            connection.execute("ANALYZE ranked_day_versions")
            after = connection.execute(
                """
                EXPLAIN (ANALYZE, FORMAT JSON)
                SELECT id, official_season_id
                FROM ranked_day_versions
                WHERE ranked_day_start = %s
                  AND state = 'Complete' AND coverage_complete
                ORDER BY id DESC
                LIMIT 1
                """,
                (target_day,),
            ).fetchone()[0][0]["Plan"]
            after_scans = []
            pending = [after]
            while pending:
                node = pending.pop()
                if node.get("Index Name") == "ranked_day_versions_completed_day_v1":
                    after_scans.append(node)
                pending.extend(node.get("Plans", []))
            assert after_scans and after_scans[0]["Actual Rows"] == 1


def test_missing_army_code_remains_canonical_no_army_facts(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as ci:
        observed_at = datetime(2026, 8, 4, 12, 5, tzinfo=UTC)
        # missing code row (no armyShareCode key)
        body_missing = json.dumps(
            {"items": [_live_row(True, "#8PP", None, observed_at)]}
        ).encode()
        # we need to remove the key: _live_row with code None still adds no key, good
        _, job_missing = store_observation(
            ci,
            archive_server,
            occurrence_key="missing-code",
            endpoint="battle_log",
            body=body_missing,
            observed_at=observed_at,
            normalized_tag="#2PP",
        )
        # empty code row
        body_empty = json.dumps(
            {"items": [_live_row(True, "#9PP", "", observed_at + timedelta(minutes=1))]}
        ).encode()
        _, job_empty = store_observation(
            ci,
            archive_server,
            occurrence_key="empty-code",
            endpoint="battle_log",
            body=body_empty,
            observed_at=observed_at + timedelta(minutes=1),
            normalized_tag="#2PP",
        )
        database, processor = _processor(ci, archive_server)
        try:
            assert processor.process_job(job_missing, owner="t1").outcome in (
                "processed",
                "processed_with_gaps",
            )
            assert processor.process_job(job_empty, owner="t2").outcome in (
                "processed",
                "processed_with_gaps",
            )
            with database.pool.connection() as conn:
                battles = conn.execute(
                    "SELECT count(*) FROM legend_battles"
                ).fetchone()[0]
                evidences = conn.execute(
                    "SELECT count(*) FROM battle_evidence"
                ).fetchone()[0]
                decodes = conn.execute(
                    "SELECT count(*) FROM battle_army_decodes WHERE status='decoded'"
                ).fetchone()[0]
                failures = conn.execute(
                    "SELECT count(*) FROM battle_army_decodes WHERE status='failed'"
                ).fetchone()[0]
                # Two canonical battles should exist despite missing codes
                assert battles == 2
                assert evidences == 2
                # No decoded facts, only failures (missing/empty)
                assert decodes == 0
                assert failures == 2
                cats = [
                    text(r[0])
                    for r in conn.execute(
                        "SELECT failure_category FROM battle_army_decodes ORDER BY id"
                    ).fetchall()
                ]
                assert set(cats) == {
                    "missing_army_share_code",
                    "empty_army_share_code",
                }
        finally:
            database.close()


def test_fixture_decodes_and_permutations_share_exact_army(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as ci:
        base = "h0p9e14_32d1x53u2x58-1x97s2x2"
        perm = "s2x2u2x58-1x97h0p9e14_32d1x53"
        ts1 = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
        ts2 = datetime(2026, 8, 4, 13, 0, tzinfo=UTC)
        body1 = json.dumps({"items": [_live_row(True, "#8PP", base, ts1)]}).encode()
        body2 = json.dumps({"items": [_live_row(True, "#9PP", perm, ts2)]}).encode()
        _, j1 = store_observation(
            ci,
            archive_server,
            occurrence_key="perm1",
            endpoint="battle_log",
            body=body1,
            observed_at=ts1 + timedelta(minutes=1),
            normalized_tag="#2PP",
        )
        _, j2 = store_observation(
            ci,
            archive_server,
            occurrence_key="perm2",
            endpoint="battle_log",
            body=body2,
            observed_at=ts2 + timedelta(minutes=1),
            normalized_tag="#2PP",
        )
        db, proc = _processor(ci, archive_server)
        try:
            proc.process_job(j1, owner="p1")
            proc.process_job(j2, owner="p2")
            with db.pool.connection() as conn:
                exact_cnt = conn.execute(
                    "SELECT count(*) FROM exact_armies"
                ).fetchone()[0]
                decodes = conn.execute(
                    "SELECT identity_hash, exact_army_id FROM battle_army_decodes WHERE status='decoded' ORDER BY id"
                ).fetchall()
                assert exact_cnt == 1, "permutations must normalize to same army"
                assert len(decodes) == 2
                assert decodes[0][0] == decodes[1][0]
                assert decodes[0][1] == decodes[1][1]
                # Two battles referencing one army count as two uses (check via count of decodes)
                assert (
                    conn.execute(
                        "SELECT count(*) FROM battle_army_decodes WHERE exact_army_id = %s",
                        (decodes[0][1],),
                    ).fetchone()[0]
                    == 2
                )
        finally:
            db.close()


def test_changing_siege_or_cc_troops_does_not_change_identity(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as ci:
        base = "h0p9e14_32d1x53u2x58-1x97s2x2"
        with_siege = "h0p9e14_32d1x53u1x51-2x58-1x97s2x2"
        with_cc = "h0p9e14_32d1x53u2x58-1x97i1x0s2x2"
        ts = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
        _, j_base = store_observation(
            ci,
            archive_server,
            occurrence_key="base-id",
            endpoint="battle_log",
            body=json.dumps({"items": [_live_row(True, "#8PP", base, ts)]}).encode(),
            observed_at=ts + timedelta(minutes=1),
            normalized_tag="#2PP",
        )
        _, j_siege = store_observation(
            ci,
            archive_server,
            occurrence_key="siege-id",
            endpoint="battle_log",
            body=json.dumps(
                {
                    "items": [
                        _live_row(True, "#9PP", with_siege, ts + timedelta(hours=1))
                    ]
                }
            ).encode(),
            observed_at=ts + timedelta(hours=1, minutes=1),
            normalized_tag="#2PP",
        )
        _, j_cc = store_observation(
            ci,
            archive_server,
            occurrence_key="cc-id",
            endpoint="battle_log",
            body=json.dumps(
                {"items": [_live_row(True, "#YPP", with_cc, ts + timedelta(hours=2))]}
            ).encode(),
            observed_at=ts + timedelta(hours=2, minutes=1),
            normalized_tag="#2PP",
        )
        db, proc = _processor(ci, archive_server)
        try:
            proc.process_job(j_base, owner="a1")
            proc.process_job(j_siege, owner="a2")
            proc.process_job(j_cc, owner="a3")
            with db.pool.connection() as conn:
                hashes = [
                    text(r[0])
                    for r in conn.execute(
                        "SELECT identity_hash FROM battle_army_decodes WHERE status='decoded' ORDER BY id"
                    ).fetchall()
                ]
                assert hashes[0] == hashes[1] == hashes[2]
                # siege preserved but not in identity
                siege_rows = conn.execute(
                    "SELECT siege FROM battle_army_decodes WHERE status='decoded' ORDER BY id"
                ).fetchall()
                assert (
                    siege_rows[0][0] == []
                    or siege_rows[0][0] is None
                    or len(siege_rows[0][0]) == 0
                )
                assert len(siege_rows[1][0]) == 1
        finally:
            db.close()


def test_attacker_defender_perspectives_count_once(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as ci:
        code = "h0p9e14_32d1x53u2x58-1x97s2x2"
        ts = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
        attacker_body = json.dumps(
            {"items": [_live_row(True, "#8PP", code, ts)]}
        ).encode()
        defender_row = _live_row(False, "#2PP", code, ts)
        defender_row["opponentPlayerTag"] = "#2PP"
        defender_body = json.dumps({"items": [defender_row]}).encode()
        _, j_att = store_observation(
            ci,
            archive_server,
            occurrence_key="att-persp",
            endpoint="battle_log",
            body=attacker_body,
            observed_at=ts + timedelta(minutes=1),
            normalized_tag="#2PP",
        )
        _, j_def = store_observation(
            ci,
            archive_server,
            occurrence_key="def-persp",
            endpoint="battle_log",
            body=defender_body,
            observed_at=ts + timedelta(minutes=2),
            normalized_tag="#8PP",
        )
        db, proc = _processor(ci, archive_server)
        try:
            proc.process_job(j_att, owner="att")
            proc.process_job(j_def, owner="def")
            with db.pool.connection() as conn:
                battles = conn.execute(
                    "SELECT count(*) FROM legend_battles"
                ).fetchone()[0]
                decodes = conn.execute(
                    "SELECT count(*) FROM battle_army_decodes WHERE status='decoded'"
                ).fetchone()[0]
                assert battles == 1
                assert decodes == 2, (
                    "attacker and defender reports retain independent decodes"
                )
        finally:
            db.close()


def test_correction_replaces_stale_facts(database_url: str, archive_server) -> None:
    with domain_database(database_url) as ci:
        ts1 = datetime(2026, 8, 4, 12, 0, tzinfo=UTC)
        code1 = "h0p9e14_32d1x53u2x58-1x97s2x2"
        code2 = "h0p9e14_32d1x53u1x58-1x97s2x2"  # qty change
        _, j1 = store_observation(
            ci,
            archive_server,
            occurrence_key="corr1",
            endpoint="battle_log",
            body=json.dumps({"items": [_live_row(True, "#8PP", code1, ts1)]}).encode(),
            observed_at=ts1 + timedelta(minutes=1),
            normalized_tag="#2PP",
        )
        db, proc = _processor(ci, archive_server)
        try:
            proc.process_job(j1, owner="c1")
            with db.pool.connection() as conn:
                first_hash = text(
                    conn.execute(
                        "SELECT identity_hash FROM battle_army_decodes WHERE is_active=true"
                    ).fetchone()[0]
                )
            # reprocess same battle with corrected attacker code (new observation, later timestamp, same battle identity)
            _, j2 = store_observation(
                ci,
                archive_server,
                occurrence_key="corr2",
                endpoint="battle_log",
                body=json.dumps(
                    {"items": [_live_row(True, "#8PP", code2, ts1)]}
                ).encode(),
                observed_at=ts1 + timedelta(minutes=5),
                normalized_tag="#2PP",
            )
            proc.process_job(j2, owner="c2")
            with db.pool.connection() as conn:
                active = conn.execute(
                    "SELECT count(*) FROM battle_army_decodes WHERE is_active=true"
                ).fetchone()[0]
                total = conn.execute(
                    "SELECT count(*) FROM battle_army_decodes"
                ).fetchone()[0]
                second_hash = text(
                    conn.execute(
                        "SELECT identity_hash FROM battle_army_decodes WHERE is_active=true"
                    ).fetchone()[0]
                )
                assert active == 1
                assert total == 2, "old result remains auditable but not active"
                assert first_hash != second_hash
                # stale current-version rows removed (is_active false)
                assert (
                    conn.execute(
                        "SELECT count(*) FROM battle_army_decodes WHERE is_active=false"
                    ).fetchone()[0]
                    == 1
                )
        finally:
            db.close()


def test_partial_decode_persists_known_and_unknown_facts_per_perspective(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as connection_info:
        timestamp = datetime(2026, 8, 4, 12, tzinfo=UTC)
        _, job_id = store_observation(
            connection_info,
            archive_server,
            occurrence_key="partial-perspective",
            endpoint="battle_log",
            body=json.dumps(
                {"items": [_live_row(True, "#8PP", "u2x58-3x9999s1x2", timestamp)]}
            ).encode(),
            observed_at=timestamp + timedelta(minutes=1),
            normalized_tag="#2PP",
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            assert processor.process_job(job_id, owner="partial").outcome == "processed"
            with database.pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT perspective, status, exact_army_id, home_troops,
                           unresolved_components
                    FROM battle_army_decodes WHERE is_active
                    """
                ).fetchone()
            assert text(row[0]) == "attacker"
            assert text(row[1]) == "partial"
            assert row[2] is None
            assert row[3] == [["troop:58", 2, "home"]]
            assert row[4] == [
                {
                    "numeric_id": 9999,
                    "quantity": 3,
                    "section": "u",
                    "origin": "home",
                }
            ]
        finally:
            database.close()


def _pause_both_jobs_after_call(
    monkeypatch, module, name: str, call_number: int, *, barrier=None
):
    """Hold each job at a lock-taking step until both arrive, which forces the overlap."""
    original = getattr(module, name)
    if barrier is None:
        barrier = threading.Barrier(2, timeout=3)
    calls = threading.local()

    def paused(*args, **kwargs):
        calls.count = getattr(calls, "count", 0) + 1
        if calls.count == call_number:
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass  # the other job is waiting on a lock this one holds
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, paused)


def _process_battle_logs_concurrently(ci: str, archive_server, jobs: list[int]):
    db, proc = _processor(ci, archive_server)
    results: dict[int, object] = {}

    def run(job_id: int) -> None:
        try:
            results[job_id] = proc.process_job(job_id, owner=f"lane-{job_id}").outcome
        except Exception as error:  # noqa: BLE001 - reported by the assertion
            results[job_id] = repr(error)

    threads = [threading.Thread(target=run, args=(job_id,)) for job_id in jobs]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
    finally:
        db.close()
    return results


@pytest.mark.parametrize("reset_boundary", [False, True])
def test_concurrent_battle_logs_spanning_shared_days_both_complete(
    database_url: str, archive_server, monkeypatch, reset_boundary: bool
) -> None:
    # One battle log spans two Legend days, the other three. Production's
    # 2026-10-01 deadlocks took the next-Reset locks for those days in
    # opposite orders when PostgreSQL returned the days unsorted.
    with domain_database(database_url) as ci:
        days = [datetime(2026, 9, day, 12, tzinfo=UTC) for day in (28, 29, 30)]
        if reset_boundary:
            days = [
                datetime(2026, 9, 30, 12, tzinfo=UTC),
                datetime(2026, 10, 1, 12, tzinfo=UTC),
            ]
        jobs = []
        observations = []
        for player, opponents, battle_days in (
            ("#2PP", ("#8PP", "#9PP"), days[-1:] if reset_boundary else days[1:]),
            ("#2PQ", ("#8PQ", "#9PQ", "#8QQ"), days),
        ):
            rows = [
                _live_row(True, opponent, None, ts)
                for opponent, ts in zip(opponents, battle_days)
            ]
            observation_id, job_id = store_observation(
                ci,
                archive_server,
                occurrence_key=f"days-{player}",
                endpoint="battle_log",
                body=json.dumps({"items": rows}).encode(),
                observed_at=days[-1] + timedelta(hours=1),
                normalized_tag=player,
            )
            jobs.append(job_id)
            observations.append(observation_id)
        if reset_boundary:
            boundary_at = days[-1].replace(hour=5)
            with psycopg.connect(ci) as connection:
                sweep_id = connection.execute(
                    """
                    INSERT INTO collector_reset_sweeps
                        (boundary_at, member_ids, membership_captured_at)
                    SELECT %s, ARRAY[player_id], clock_timestamp()
                    FROM collector_observations WHERE id = %s
                    RETURNING id
                    """,
                    (boundary_at, observations[0]),
                ).fetchone()[0]
                connection.execute(
                    """
                    INSERT INTO collector_work (
                        kind, lane, scope, player_id, normalized_tag, sweep_id,
                        due_at, coalescing_key, status, profile_status,
                        battle_log_status, battle_log_observation_id
                    )
                    SELECT 'reset_baseline', 'reset', 'player', player_id,
                           normalized_tag, %s, %s, 'delayed-reset', 'failed',
                           'failed', 'observed', id
                    FROM collector_observations WHERE id = %s
                    """,
                    (sweep_id, boundary_at, observations[0]),
                )
            monkeypatch.setattr(
                ObservationProcessor,
                "_process_claim",
                ObservationProcessor._process_claim_once,
            )
        # Without ORDER BY, PostgreSQL's hash method returns the days unsorted.
        options = psycopg.conninfo.conninfo_to_dict(ci)["options"]
        unsorted_ci = psycopg.conninfo.make_conninfo(
            ci, options=f"{options} -c enable_sort=off"
        )
        barrier = threading.Barrier(2, timeout=3)
        _pause_both_jobs_after_call(
            monkeypatch,
            boundary_publication,
            "_enqueue_army_analytics",
            2,
            barrier=barrier,
        )
        if reset_boundary:
            _pause_both_jobs_after_call(
                monkeypatch,
                reset_baselines,
                "_refresh_reset_baseline_evidence",
                1,
                barrier=barrier,
            )

        results = _process_battle_logs_concurrently(unsorted_ci, archive_server, jobs)

    assert results == {job_id: "processed" for job_id in jobs}


def test_concurrent_battle_logs_sharing_armies_both_complete(
    database_url: str, archive_server, monkeypatch
) -> None:
    # Two battle logs use the same two armies in opposite order; the
    # 2026-10-01 01:03 UTC deadlock was on these shared exact_armies rows.
    with domain_database(database_url) as ci:
        army_a = "h0p9e14_32d1x53u2x58-1x97s2x2"
        army_b = "h0p9e14_32d1x53u1x58-1x97s2x2"
        ts = datetime(2026, 8, 4, 12, tzinfo=UTC)
        jobs = []
        for player, opponents, codes in (
            ("#2PP", ("#8PP", "#9PP"), (army_a, army_b)),
            ("#2PQ", ("#8PQ", "#9PQ"), (army_b, army_a)),
        ):
            rows = [
                _live_row(True, opponent, code, ts + timedelta(minutes=index))
                for index, (opponent, code) in enumerate(zip(opponents, codes))
            ]
            _, job_id = store_observation(
                ci,
                archive_server,
                occurrence_key=f"armies-{player}",
                endpoint="battle_log",
                body=json.dumps({"items": rows}).encode(),
                observed_at=ts + timedelta(hours=1),
                normalized_tag=player,
            )
            jobs.append(job_id)
        _pause_both_jobs_after_call(
            monkeypatch, army_ingestion, "decode_army_share_code", 2
        )

        results = _process_battle_logs_concurrently(ci, archive_server, jobs)

    assert results == {job_id: "processed" for job_id in jobs}


def test_battle_log_with_saved_decodes_does_not_wait_for_reset_lock(
    database_url: str, archive_server
) -> None:
    # After the 2026-10-01 Reset, battle logs that only repeated saved battles
    # queued one at a time behind that day's Reset lock.
    with domain_database(database_url) as ci:
        ts = datetime(2026, 8, 4, 12, tzinfo=UTC)
        _, job_id = store_observation(
            ci,
            archive_server,
            occurrence_key="saved-decodes",
            endpoint="battle_log",
            body=json.dumps({"items": [_live_row(True, "#8PP", None, ts)]}).encode(),
            observed_at=ts + timedelta(minutes=1),
            normalized_tag="#2PP",
        )
        db, proc = _processor(ci, archive_server)
        try:
            assert proc.process_job(job_id, owner="seed").outcome == "processed"
            with psycopg.connect(ci) as publisher:
                publisher.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    ("boundary-publication:2026-08-05T05:00:00+00:00",),
                )
                with db.pool.connection() as connection:
                    with connection.transaction():
                        connection.execute("SET LOCAL lock_timeout = '2s'")
                        battle_ids = [
                            row[0]
                            for row in connection.execute(
                                "SELECT id FROM legend_battles"
                            ).fetchall()
                        ]
                        army_ingestion._upsert_army_decodes(db, connection, battle_ids)
        finally:
            db.close()


def test_reset_battle_log_and_baseline_writer_take_locks_in_one_order(
    database_url: str, archive_server, monkeypatch
) -> None:
    # 2026-10-01 05:18 deadlocks: a Reset battle log held the Reset lock and
    # waited for its baseline lock, while the job recording that baseline held
    # the baseline lock and waited for the Reset lock.
    with domain_database(database_url) as ci:
        boundary_at = datetime(2026, 10, 1, 5, tzinfo=UTC)
        observation_id, job_id = store_observation(
            ci,
            archive_server,
            occurrence_key="reset-lock-order",
            endpoint="battle_log",
            body=json.dumps(
                {"items": [_live_row(True, "#8PP", None, boundary_at + timedelta(hours=1))]}
            ).encode(),
            observed_at=boundary_at + timedelta(hours=2),
            normalized_tag="#2PP",
        )
        with psycopg.connect(ci) as connection:
            work_id = connection.execute(
                """
                WITH sweep AS (
                    INSERT INTO collector_reset_sweeps
                        (boundary_at, member_ids, membership_captured_at)
                    SELECT %s, ARRAY[player_id], clock_timestamp()
                    FROM collector_observations WHERE id = %s
                    RETURNING id
                )
                INSERT INTO collector_work (
                    kind, lane, scope, player_id, normalized_tag, sweep_id,
                    due_at, coalescing_key, status, profile_status,
                    battle_log_status, battle_log_observation_id
                )
                SELECT 'reset_baseline', 'reset', 'player', player_id,
                       normalized_tag, sweep.id, %s, 'delayed-reset', 'failed',
                       'failed', 'observed', observation.id
                FROM collector_observations AS observation, sweep
                WHERE observation.id = %s
                RETURNING id
                """,
                (boundary_at, observation_id, boundary_at, observation_id),
            ).fetchone()[0]
        monkeypatch.setattr(
            ObservationProcessor,
            "_process_claim",
            ObservationProcessor._process_claim_once,
        )
        baseline_key = f"reset-baseline:{work_id}"
        results: dict[int, object] = {}

        def run() -> None:
            db, proc = _processor(ci, archive_server)
            try:
                results[job_id] = proc.process_job(job_id, owner="reset").outcome
            except Exception as error:  # noqa: BLE001 - reported by the assertion
                results[job_id] = repr(error)
            finally:
                db.close()

        thread = threading.Thread(target=run)
        with psycopg.connect(ci) as baseline_writer:
            baseline_writer.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (baseline_key,),
            )
            thread.start()
            with psycopg.connect(ci, autocommit=True) as observer:
                for _ in range(200):
                    waiting = observer.execute(
                        """
                        SELECT count(*) FROM pg_locks
                        WHERE locktype = 'advisory' AND NOT granted
                          AND objid = (hashtextextended(%s, 0) & 4294967295)::oid
                        """,
                        (baseline_key,),
                    ).fetchone()[0]
                    if waiting:
                        break
                    time.sleep(0.05)
            assert waiting, "battle log never reached its baseline lock"
            baseline_writer.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                ("boundary-publication:2026-10-01T05:00:00+00:00",),
            )
        thread.join(timeout=60)

    assert results == {job_id: "processed"}


def test_deadlocked_battle_log_is_retried_without_using_an_attempt(
    database_url: str, archive_server, monkeypatch
) -> None:
    with domain_database(database_url) as ci:
        ts = datetime(2026, 8, 4, 12, tzinfo=UTC)
        _, job_id = store_observation(
            ci,
            archive_server,
            occurrence_key="deadlock-retry",
            endpoint="battle_log",
            body=json.dumps({"items": [_live_row(True, "#8PP", None, ts)]}).encode(),
            observed_at=ts + timedelta(minutes=1),
            normalized_tag="#2PP",
        )
        original = battle_ingestion.complete_battle_log
        deadlocks = [psycopg.errors.DeadlockDetected("deadlock detected")]

        def deadlock_once(*args, **kwargs):
            if deadlocks:
                raise deadlocks.pop()
            return original(*args, **kwargs)

        monkeypatch.setattr(battle_ingestion, "complete_battle_log", deadlock_once)
        db, proc = _processor(ci, archive_server)
        try:
            result = proc.process_job(job_id, owner="deadlock")
            with db.pool.connection() as conn:
                job = conn.execute(
                    "SELECT status, attempt_count FROM python_processing_jobs WHERE id = %s",
                    (job_id,),
                ).fetchone()
        finally:
            db.close()

    assert result.outcome == "processed"
    assert (text(job[0]), job[1]) == ("complete", 1)
