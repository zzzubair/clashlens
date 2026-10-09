from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from domain_test_support import domain_database, store_observation, text
from test_boundary_publication_postgres import _sweep_with_members

from clashlens import (
    army_ingestion,
    battle_ingestion,
    boundary,
    boundary_publication,
    ingestion,
    job_outcomes,
    reset_baselines,
)
from clashlens.archive import ArchiveReadError, S3ArchiveReader
from clashlens.db import Database
from clashlens.worker import ObservationProcessor, ProcessResult, process_concurrently


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


def test_unnamed_id_is_saved_with_the_army_and_its_identity(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as connection_info:
        timestamp = datetime(2026, 8, 4, 12, tzinfo=UTC)
        _, job_id = store_observation(
            connection_info,
            archive_server,
            occurrence_key="unnamed-id",
            endpoint="battle_log",
            body=json.dumps(
                {"items": [_live_row(True, "#8PP", "u2x58-3x9999s1x2", timestamp)]}
            ).encode(),
            observed_at=timestamp + timedelta(minutes=1),
            normalized_tag="#2PP",
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            assert processor.process_job(job_id, owner="unnamed").outcome == "processed"
            with database.pool.connection() as connection:
                row = connection.execute(
                    """
                    SELECT perspective, status, exact_army_id, home_troops,
                           unresolved_components
                    FROM battle_army_decodes WHERE is_active
                    """
                ).fetchone()
            assert text(row[0]) == "attacker"
            assert text(row[1]) == "decoded"
            assert row[2] is not None
            assert row[3] == [["troop:58", 2, "home"], ["troop:9999", 3, "home"]]
            assert row[4] == []
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
        # Each job pauses while holding its Resets, so let the other wait past
        # the 1 s deadlock check: opposite lock orders must fail as deadlocks.
        monkeypatch.setattr(battle_ingestion, "RESET_LOCK_WAIT", "10s")
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


@pytest.mark.parametrize("code", [None, "u1x58", "u1x58-1x9999", "invalid"])
def test_battle_log_with_saved_decodes_does_not_wait_for_reset_lock(
    database_url: str, archive_server, code: str | None
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
            body=json.dumps({"items": [_live_row(True, "#8PP", code, ts)]}).encode(),
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


def _unswept_battles(ci: str, archive_server, players: dict[str, str]):
    """Save one Aug 4 battle per player whose decodes still need saving."""
    ts = datetime(2026, 8, 4, 12, tzinfo=UTC)
    db, proc = _processor(ci, archive_server)
    try:
        for player, code in players.items():
            _, job_id = store_observation(
                ci,
                archive_server,
                occurrence_key=f"unswept-{player}",
                endpoint="battle_log",
                body=json.dumps({"items": [_live_row(True, "#8PP", code, ts)]}).encode(),
                observed_at=ts + timedelta(minutes=1),
                normalized_tag=player,
            )
            assert proc.process_job(job_id, owner="seed").outcome == "processed"
    finally:
        db.close()
    with psycopg.connect(ci) as connection:
        # As if each battle were new, so the next save writes decodes again.
        connection.execute("UPDATE battle_army_decodes SET is_active = false")
        return {
            text(tag): [int(battle_id)]
            for tag, battle_id in connection.execute(
                """
                SELECT player.normalized_tag, battle.id FROM legend_battles AS battle
                JOIN players AS player ON player.id = battle.attacker_player_id
                """
            ).fetchall()
        }


def test_battle_logs_before_a_reset_do_not_wait_on_each_other(
    database_url: str, archive_server
) -> None:
    # 2026-10-04 03:41 UTC: 7-8 worker connections queued one at a time on
    # the lock of that day's 05:00 Reset, which had not happened yet.
    with domain_database(database_url, include_coordinator=True) as ci:
        battles = _unswept_battles(
            ci, archive_server, {"#2PP": "u1x58", "#2PQ": "u2x58"}
        )
        db = Database(ci)
        try:
            with psycopg.connect(ci) as first, db.pool.connection() as second:
                army_ingestion._upsert_army_decodes(db, first, battles["#2PP"])
                with second.transaction():
                    second.execute("SET LOCAL lock_timeout = '2s'")
                    army_ingestion._upsert_army_decodes(db, second, battles["#2PQ"])
                first.commit()
            with db.pool.connection() as connection:
                saved = connection.execute(
                    "SELECT count(*) FROM battle_army_decodes WHERE is_active"
                ).fetchone()[0]
        finally:
            db.close()

    assert saved == 2


def test_battle_log_started_before_a_reset_records_its_member(
    database_url: str, archive_server
) -> None:
    # A job that saves decodes before the Reset's sweep exists, then records
    # the player's day result after the collector saves the sweep, still
    # records that result in the Reset's first generation.
    with domain_database(database_url, include_coordinator=True) as ci:
        battle_ids = _unswept_battles(ci, archive_server, {"#2PP": "u1x58"})["#2PP"]
        reset = datetime(2026, 8, 5, 5, tzinfo=UTC)
        db = Database(ci)
        try:
            with db.pool.connection() as job:
                with job.transaction():
                    player_id = int(
                        job.execute(
                            "SELECT id FROM players WHERE normalized_tag = '#2PP'"
                        ).fetchone()[0]
                    )
                    army_ingestion._upsert_army_decodes(db, job, battle_ids)
                    with psycopg.connect(ci, autocommit=True) as collector:
                        sweep_id = _sweep_with_members(collector, [player_id])
                    version_id = int(
                        job.execute(
                            """
                            INSERT INTO ranked_day_versions (
                                player_id, ranked_day_start, ranked_day_end,
                                official_season_id, season_day_number,
                                season_anchor_rule_version,
                                reconciliation_rule_version, result_hash,
                                version, state, confidence, input_hash,
                                evidence_complete, coverage_complete
                            ) VALUES (
                                %s, %s, %s, 'test-season', 1, 'test-anchor',
                                'test-rules', %s, 1, 'Complete', 'exact', %s,
                                true, true
                            ) RETURNING id
                            """,
                            (
                                player_id,
                                reset - timedelta(days=1),
                                reset,
                                "a" * 64,
                                "a" * 64,
                            ),
                        ).fetchone()[0]
                    )
                    assert boundary._record_boundary_generation(
                        db,
                        job,
                        boundary_at=reset,
                        player_id=player_id,
                        ranked_day_version_id=version_id,
                        ranked_day_input_hash="a" * 64,
                    )
            with db.pool.connection() as connection:
                members = connection.execute(
                    """
                    SELECT generation.sweep_id, member.player_id,
                           member.ranked_day_version_id,
                           member.ranked_day_input_hash
                    FROM boundary_publication_generation_members AS member
                    JOIN boundary_publication_generations AS generation
                      ON generation.id = member.generation_id
                    WHERE generation.boundary_at = %s
                    """,
                    (reset,),
                ).fetchall()
        finally:
            db.close()

    assert [tuple(row[:3]) + (text(row[3]),) for row in members] == [
        (sweep_id, player_id, version_id, "a" * 64)
    ]


@pytest.mark.parametrize("attack", [True, False])
@pytest.mark.parametrize(
    ("old_code", "new_code", "expected_status"),
    [
        ("u1x58", "u2x58", "decoded"),
        ("u1x58-1x9999", "u2x58-2x9999", "decoded"),
        ("invalid-old", "invalid-new", "failed"),
        ("u1x58", "u1x58", "decoded"),
    ],
)
def test_redecode_preserves_report_corrected_before_battle_lock(
    database_url: str,
    archive_server,
    monkeypatch,
    attack: bool,
    old_code: str,
    new_code: str,
    expected_status: str,
) -> None:
    with domain_database(database_url) as ci:
        ts = datetime(2026, 8, 4, 12, tzinfo=UTC)
        jobs = []
        for occurrence, code, minutes in (
            ("before-correction", old_code, 1),
            ("after-correction", new_code, 5),
        ):
            rows = [_live_row(attack, "#8PP", code, ts)]
            if minutes == 1:
                rows.append(
                    _live_row(attack, "#9PP", "u3x58", ts + timedelta(seconds=30))
                )
            _, job_id = store_observation(
                ci,
                archive_server,
                occurrence_key=occurrence,
                endpoint="battle_log",
                body=json.dumps({"items": rows}).encode(),
                observed_at=ts + timedelta(minutes=minutes),
                normalized_tag="#2PP",
            )
            jobs.append(job_id)
        db, proc = _processor(ci, archive_server)
        waiting = threading.Event()
        resume = threading.Event()
        results = []

        def redecode() -> None:
            try:
                with db.pool.connection() as connection:
                    with connection.transaction():
                        army_ingestion._upsert_army_decodes(db, connection, battle_ids)
                results.append("processed")
            except Exception as error:  # noqa: BLE001 - reported by the assertion
                results.append(repr(error))

        thread = threading.Thread(target=redecode)
        original_execute = psycopg.Connection.execute

        def pause_before_battle_lock(connection, query, params=None, **kwargs):
            if threading.current_thread() is thread and str(
                (params or ("",))[0]
            ).startswith("army-redecode-battle:"):
                waiting.set()
                assert resume.wait(timeout=20), "correction never released the redecode"
            return original_execute(connection, query, params, **kwargs)

        try:
            assert proc.process_job(jobs[0], owner="seed").outcome == "processed"
            with db.pool.connection() as connection:
                battle_ids = [
                    row[0]
                    for row in connection.execute(
                        "SELECT id FROM legend_battles"
                    ).fetchall()
                ]
                connection.execute(
                    "UPDATE battle_army_decodes SET decoder_version = 'army-decoder-v1'"
                )
            monkeypatch.setattr(psycopg.Connection, "execute", pause_before_battle_lock)
            thread.start()
            assert waiting.wait(timeout=10), "redecode never reached its battle lock"
            assert proc.process_job(jobs[1], owner="correction").outcome == "processed"
            with db.pool.connection() as connection:
                corrected = connection.execute(
                    """
                    SELECT id, battle_id, evidence_id, raw_code, status
                    FROM battle_army_decodes
                    WHERE is_active AND decoder_version = %s
                    """,
                    (army_ingestion.DECODER_VERSION,),
                ).fetchone()
            assert (text(corrected[3]), text(corrected[4])) == (
                new_code,
                expected_status,
            )
            resume.set()
            thread.join(timeout=20)
            assert not thread.is_alive()
            assert results == ["processed"]
            with db.pool.connection() as connection:
                active = connection.execute(
                    """
                    SELECT id, battle_id, evidence_id, raw_code, status
                    FROM battle_army_decodes
                    WHERE is_active AND decoder_version = %s
                    ORDER BY battle_id
                    """,
                    (army_ingestion.DECODER_VERSION,),
                ).fetchall()
                assert corrected in active
                assert len(active) == 2
                unchanged = next(row for row in active if row[1] != corrected[1])
                assert (text(unchanged[3]), text(unchanged[4])) == ("u3x58", "decoded")
                assert (
                    connection.execute(
                        "SELECT count(*) FROM battle_army_decodes WHERE battle_id = %s",
                        (corrected[1],),
                    ).fetchone()[0]
                    == 2
                )
                assert not connection.execute(
                    """
                    SELECT 1 FROM battle_army_decodes AS decode
                    JOIN battle_perspectives AS perspective
                      ON perspective.battle_id = decode.battle_id
                     AND perspective.perspective = decode.perspective
                    WHERE decode.is_active AND decode.decoder_version = %s
                      AND decode.evidence_id <> perspective.evidence_id
                    """,
                    (army_ingestion.DECODER_VERSION,),
                ).fetchall()
        finally:
            resume.set()
            if thread.ident is not None:
                thread.join(timeout=20)
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
                {
                    "items": [
                        _live_row(True, "#8PP", None, boundary_at + timedelta(hours=1))
                    ]
                }
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


def test_unit_list_change_writes_nothing_to_saved_battles(
    database_url: str, archive_server, monkeypatch
) -> None:
    # On 2026-10-09 live battle logs re-decoded every battle saved under the
    # previous unit list, two jobs racing on each. Saved armies hold ids, so a
    # later poll of the same battles saves nothing, whichever unit list saved
    # them and even if it kept the then-unnamed Portal Pendant apart.
    ts = datetime(2026, 8, 4, 12, tzinfo=UTC)
    body = json.dumps(
        {
            "items": [
                _live_row(True, "#8PP", "h6p17e61_49u5x177s1x2", ts),
                _live_row(True, "#9PP", "h6p17e61_49u5x177", ts + timedelta(minutes=1)),
            ]
        }
    ).encode()
    read_all = "SELECT to_jsonb(decode) FROM battle_army_decodes AS decode ORDER BY id"
    with domain_database(database_url) as ci:
        db, proc = _processor(ci, archive_server)
        try:
            for occurrence, minutes in (("first-poll", 5), ("later-poll", 10)):
                _, job_id = store_observation(
                    ci,
                    archive_server,
                    occurrence_key=occurrence,
                    endpoint="battle_log",
                    body=body,
                    observed_at=ts + timedelta(minutes=minutes),
                    normalized_tag="#2PP",
                )
                if occurrence == "later-poll":
                    with db.pool.connection() as connection:
                        connection.execute(
                            "UPDATE battle_army_decodes"
                            " SET catalog_version = 'unit-catalog-v2'"
                        )
                        connection.execute(
                            """
                            UPDATE battle_army_decodes
                            SET status = 'partial', exact_army_id = NULL,
                                identity_hash = NULL,
                                heroes = '[{"hero": "hero:6", "pet": "pet:17",
                                            "equipment": ["equipment:49"]}]',
                                unresolved_components = '[{"numeric_id": 61,
                                    "quantity": 1, "section": "h",
                                    "origin": "hero:6:equipment"}]'
                            WHERE id = (SELECT max(id) FROM battle_army_decodes)
                            """
                        )
                        saved = connection.execute(read_all).fetchall()
                    monkeypatch.setattr(army_ingestion, "CATALOG_VERSION", "unit-catalog-v4")
                assert proc.process_job(job_id, owner=occurrence).outcome == "processed"
            with db.pool.connection() as connection:
                after = connection.execute(read_all).fetchall()
        finally:
            db.close()
    assert len(saved) == 2
    assert after == saved


def test_names_at_display_migration_settles_only_catalog_v3_redecodes(
    database_url: str,
) -> None:
    migration = (
        Path(__file__).parents[2] / "deploy/migrations/0091_unit_names_at_display.sql"
    ).read_text(encoding="utf-8")
    jobs = {
        "redecode_army:army-decoder-v2:unit-catalog-v3:1:1": "pending",
        "redecode_army:army-decoder-v2:unit-catalog-v3:2:2": "waiting_retry",
        "redecode_army:army-decoder-v2:unit-catalog-v3:3:3": "complete",
        "redecode_army:army-decoder-v2:unit-catalog-v2:4:4": "pending",
    }
    with domain_database(database_url) as ci:
        with psycopg.connect(ci, autocommit=True) as connection:
            for key, status in jobs.items():
                connection.execute(
                    """
                    INSERT INTO python_processing_jobs (
                        work_type, deduplication_key, input_json, priority,
                        processing_version, domain_rule_version,
                        analytics_rule_version, due_at, status, outcome
                    ) VALUES (
                        'redecode_army', %s, '{"battle_ids": [1]}', 25,
                        'clashlens-domain-processing-v1', 'clashlens-domain-rules-v1',
                        'army-analytics-v2', '2026-10-10 08:00:00+00', %s,
                        CASE WHEN %s = 'complete' THEN 'processed' END
                    )
                    """,
                    (key, status, status),
                )
            connection.execute(migration)
            connection.execute(migration)
            rows = connection.execute(
                """
                SELECT deduplication_key, status, outcome FROM python_processing_jobs
                WHERE work_type = 'redecode_army' ORDER BY deduplication_key
                """
            ).fetchall()
    assert [(text(key), text(status), outcome and text(outcome)) for key, status, outcome in rows] == [
        ("redecode_army:army-decoder-v2:unit-catalog-v2:4:4", "pending", None),
        ("redecode_army:army-decoder-v2:unit-catalog-v3:1:1", "complete", "stale_superseded"),
        ("redecode_army:army-decoder-v2:unit-catalog-v3:2:2", "complete", "stale_superseded"),
        ("redecode_army:army-decoder-v2:unit-catalog-v3:3:3", "complete", "processed"),
    ]


def _job_state(db: Database, job_id: int) -> tuple[str, int]:
    with db.pool.connection() as conn:
        row = conn.execute(
            "SELECT status, attempt_count FROM python_processing_jobs WHERE id = %s",
            (job_id,),
        ).fetchone()
    return text(row[0]), row[1]


@pytest.mark.parametrize(
    ("limit", "reason"),
    [
        ("busy_reset", "database_lock_busy"),
        ("statement", "database_timeout"),
        ("idle_in_transaction", "database_session_timeout"),
        ("transaction", "database_session_timeout"),
        ("idle_at_commit", "database_session_timeout"),
    ],
)
def test_time_limits_retry_a_battle_log_and_keep_the_lane_working(
    database_url: str, archive_server, monkeypatch, limit: str, reason: str
) -> None:
    # On 2026-10-03 one publication held a Reset for 746 seconds while battle
    # logs waiting on it held their rows, and the collector waited on those.
    with domain_database(database_url) as ci:
        ts = datetime(2026, 8, 4, 12, tzinfo=UTC)
        _, limited = store_observation(
            ci,
            archive_server,
            occurrence_key="limited",
            endpoint="battle_log",
            body=json.dumps({"items": [_live_row(True, "#8PP", "u1x58", ts)]}).encode(),
            observed_at=ts + timedelta(minutes=1),
            normalized_tag="#2PP",
        )
        db, proc = _processor(ci, archive_server)
        pending = [limited]
        lock_resets = army_ingestion.reset_settlement.lock_resets
        finish_claim = db._finish_claim

        def end_session(connection, setting: str) -> None:
            connection.execute(f"SET LOCAL {setting} = '50ms'")
            time.sleep(0.3)

        def limited_lock_resets(database, connection, *args, **kwargs):
            if pending and limit == "statement":
                pending.pop()
                connection.execute("SET LOCAL statement_timeout = '50ms'")
                connection.execute("SELECT pg_sleep(1)")
            if pending and limit in {"idle_in_transaction", "transaction"}:
                pending.pop()
                end_session(connection, f"{limit}_session_timeout"
                            if limit == "idle_in_transaction" else "transaction_timeout")
            return lock_resets(database, connection, *args, **kwargs)

        def limited_finish(connection, *args, **kwargs):
            finish_claim(connection, *args, **kwargs)
            if pending and limit == "idle_at_commit":
                pending.pop()
                # The session ends before COMMIT reaches the database.
                end_session(connection, "idle_in_transaction_session_timeout")

        monkeypatch.setattr(army_ingestion.reset_settlement, "lock_resets", limited_lock_resets)
        monkeypatch.setattr(db, "_finish_claim", limited_finish)
        try:
            with psycopg.connect(ci) as publisher:
                if limit == "busy_reset":
                    publisher.execute(
                        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        ("boundary-publication:2026-08-05T05:00:00+00:00",),
                    )
                # The other observation's Reset is free.
                other_ts = ts + timedelta(days=2)
                _, other = store_observation(
                    ci,
                    archive_server,
                    occurrence_key="other",
                    endpoint="battle_log",
                    body=json.dumps(
                        {"items": [_live_row(True, "#9PP", "u2x58", other_ts)]}
                    ).encode(),
                    observed_at=other_ts + timedelta(minutes=1),
                    normalized_tag="#2QQ",
                )
                started = time.monotonic()
                results = process_concurrently(
                    proc, concurrency=1, owner="limited", max_jobs=2
                )
                assert time.monotonic() - started < 5
                assert sorted(results, key=lambda result: result.job_id) == [
                    ProcessResult(limited, "retrying", reason),
                    ProcessResult(other, "processed"),
                ]
                # Nothing of the limited log was kept, and its try is refunded.
                assert _job_state(db, limited) == ("leased", 0)
                with db.pool.connection() as conn:
                    assert conn.execute(
                        "SELECT count(*) FROM legend_battles"
                        " WHERE ranked_day_start < %s",
                        (datetime(2026, 8, 5, 5, tzinfo=UTC),),
                    ).fetchone()[0] == 0
                publisher.rollback()
            db.expire_lease(limited)
            assert db.maintain_queue(max_jobs=1) == 1
            assert proc.process_job(limited, owner="retry").outcome == "processed"
            assert _job_state(db, limited) == ("complete", 1)
        finally:
            db.close()


@pytest.mark.parametrize(
    ("endpoint", "invalid_row", "outcome"),
    [
        ("battle_log", False, "processed"),
        ("battle_log", True, "processed_with_gaps"),
        ("global_player_rankings", False, "processed"),
    ],
)
def test_a_session_ending_after_its_commit_landed_keeps_the_saved_result(
    database_url: str,
    archive_server,
    monkeypatch,
    endpoint: str,
    invalid_row: bool,
    outcome: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as ci:
        ts = datetime(2026, 8, 4, 12, tzinfo=UTC)
        battle_log = endpoint == "battle_log"
        rows = [_live_row(True, "#8PP", "u1x58", ts)]
        if invalid_row:
            rows.append({**_live_row(True, "#9PP", None, ts), "stars": None})
        _, job_id = store_observation(
            ci,
            archive_server,
            occurrence_key="committed",
            endpoint=endpoint,
            body=json.dumps({"items": rows}).encode()
            if battle_log
            else (Path(__file__).parents[1] / "testdata/global_top_200_v1.json").read_bytes(),
            observed_at=ts + timedelta(minutes=1),
            normalized_tag="#2PP" if battle_log else None,
        )
        module, name = (
            (battle_ingestion, "complete_battle_log")
            if battle_log
            else (ingestion, "complete_rankings")
        )
        original = getattr(module, name)

        def committed_then_lost(*args, **kwargs):
            original(*args, **kwargs)
            raise psycopg.errors.TransactionTimeout("terminating connection")

        monkeypatch.setattr(module, name, committed_then_lost)
        db, proc = _processor(ci, archive_server)
        try:
            assert proc.process_job(job_id, owner="committed") == ProcessResult(
                job_id, outcome
            )
            assert _job_state(db, job_id) == ("complete", 1)
        finally:
            db.close()


def test_a_session_ending_after_a_storage_wait_landed_reports_retrying(
    database_url: str, archive_server, monkeypatch
) -> None:
    with domain_database(database_url) as ci:
        ts = datetime(2026, 8, 4, 12, tzinfo=UTC)
        _, job_id = store_observation(
            ci,
            archive_server,
            occurrence_key="storage-wait",
            endpoint="battle_log",
            body=json.dumps({"items": [_live_row(True, "#8PP", "u1x58", ts)]}).encode(),
            observed_at=ts + timedelta(minutes=1),
            normalized_tag="#2PP",
        )
        original = job_outcomes.fail_claim

        def storage_down(*args, **kwargs):
            raise ArchiveReadError("archive_unavailable", "down", retryable=True)

        def committed_then_lost(*args, **kwargs):
            original(*args, **kwargs)
            raise psycopg.errors.TransactionTimeout("terminating connection")

        db, proc = _processor(ci, archive_server)
        monkeypatch.setattr(proc.archive, "read_verified", storage_down)
        monkeypatch.setattr(job_outcomes, "fail_claim", committed_then_lost)
        try:
            assert proc.process_job(job_id, owner="storage") == ProcessResult(
                job_id, "retrying", "archive_unavailable"
            )
            assert _job_state(db, job_id)[0] == "waiting_dependency"
        finally:
            db.close()


def test_a_session_ending_after_a_final_rejection_landed_reports_failed(
    database_url: str, archive_server, monkeypatch
) -> None:
    with domain_database(database_url) as ci:
        ts = datetime(2026, 8, 4, 12, tzinfo=UTC)
        _, job_id = store_observation(
            ci,
            archive_server,
            occurrence_key="final-rejection",
            endpoint="battle_log",
            body=json.dumps({"items": [_live_row(True, "#8PP", "u1x58", ts)]}).encode(),
            observed_at=ts + timedelta(minutes=1),
            normalized_tag="#2PP",
        )
        with psycopg.connect(ci) as conn:
            conn.execute(
                "UPDATE python_processing_jobs SET max_attempts = 1 WHERE id = %s",
                (job_id,),
            )
        original = job_outcomes.fail_claim

        def rejected(*args, **kwargs):
            raise psycopg.errors.RaiseException("reset evidence rejected")

        def committed_then_lost(*args, **kwargs):
            original(*args, **kwargs)
            raise psycopg.errors.TransactionTimeout("terminating connection")

        monkeypatch.setattr(battle_ingestion, "complete_battle_log", rejected)
        monkeypatch.setattr(job_outcomes, "fail_claim", committed_then_lost)
        db, proc = _processor(ci, archive_server)
        try:
            assert proc.process_job(job_id, owner="rejected") == ProcessResult(
                job_id, "failed", "database_rejected"
            )
            assert _job_state(db, job_id) == ("failed", 1)
            assert db.maintain_queue(max_jobs=1) == 0
            assert proc.process_job(job_id, owner="again") is None
        finally:
            db.close()
