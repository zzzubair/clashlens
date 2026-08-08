from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
from domain_test_support import domain_database, store_observation, text

from clashlens.archive import S3ArchiveReader
from clashlens.db import Database
from clashlens.worker import ObservationProcessor

PROFILE_FIXTURE = Path(__file__).parents[1] / "testdata" / "legend_i_profile_v1.json"
BATTLE_FIXTURE = Path(__file__).parents[1] / "testdata" / "legend_i_battle_log_v1.json"
RANKING_FIXTURE = Path(__file__).parents[1] / "testdata" / "global_top_200_v1.json"


def _processor(
    connection_info: str, archive_server
) -> tuple[Database, ObservationProcessor]:
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


def _prepare_reset_baseline_pair(
    connection_info: str,
    archive_server,
    *,
    boundary: datetime,
    battle_body: bytes | None = None,
    wrong_battle_attempt: bool = False,
) -> tuple[int, int, int, int]:
    profile_observation_id, profile_job_id = store_observation(
        connection_info,
        archive_server,
        occurrence_key="reset-pair-profile",
        endpoint="profile",
        body=PROFILE_FIXTURE.read_bytes(),
        observed_at=boundary,
        normalized_tag="#2PP",
    )
    battle_observation_id, battle_job_id = store_observation(
        connection_info,
        archive_server,
        occurrence_key="reset-pair-battle",
        endpoint="battle_log",
        body=battle_body
        if battle_body is not None
        else json.dumps({"items": []}).encode(),
        observed_at=boundary,
        normalized_tag="#2PP",
    )
    with psycopg.connect(connection_info) as connection:
        player_id = connection.execute(
            "SELECT id FROM players WHERE normalized_tag = '#2PP'"
        ).fetchone()[0]
        sweep_id = connection.execute(
            """
            INSERT INTO collector_reset_sweeps (boundary_at)
            VALUES (%s)
            RETURNING id
            """,
            (boundary,),
        ).fetchone()[0]
        baseline_sweep_id = connection.execute(
            """
            INSERT INTO collector_reset_baseline_sweeps (
                reset_sweep_id, player_id, boundary_at, evidence_kind, state
            ) VALUES (%s, %s, %s, 'paired_v2', 'pending')
            RETURNING id
            """,
            (sweep_id, player_id, boundary),
        ).fetchone()[0]
        root_job_id = connection.execute(
            """
            INSERT INTO collector_jobs (
                work_type, scope, player_id, normalized_tag, capacity_pool,
                priority, due_at, coalescing_key, sweep_id,
                reset_baseline_sweep_id, status
            ) VALUES (
                'reset_baseline', 'player', %s, '#2PP', 'normal', 400, %s,
                'reset-baseline-test', %s, %s, 'complete'
            )
            RETURNING id
            """,
            (player_id, boundary, sweep_id, baseline_sweep_id),
        ).fetchone()[0]
        attempt_id = connection.execute(
            """
            INSERT INTO collector_attempts (
                job_id, status, started_at, completed_at
            ) VALUES (%s, 'complete', %s, %s)
            RETURNING id
            """,
            (root_job_id, boundary, boundary),
        ).fetchone()[0]
        battle_attempt_id = attempt_id
        if wrong_battle_attempt:
            battle_attempt_id = connection.execute(
                """
                INSERT INTO collector_attempts (
                    job_id, attempt_number, status, started_at, completed_at
                ) VALUES (%s, 2, 'complete', %s, %s)
                RETURNING id
                """,
                (root_job_id, boundary, boundary),
            ).fetchone()[0]
        connection.execute(
            "UPDATE collector_jobs SET result_attempt_id = %s WHERE id = %s",
            (attempt_id, root_job_id),
        )
        connection.execute(
            """
            UPDATE collector_observations
            SET collection_job_id = %s, attempt_id = %s
            WHERE id = %s
            """,
            (root_job_id, attempt_id, profile_observation_id),
        )
        connection.execute(
            """
            UPDATE collector_observations
            SET collection_job_id = %s, attempt_id = %s
            WHERE id = %s
            """,
            (root_job_id, battle_attempt_id, battle_observation_id),
        )
        for endpoint, observation_id in (
            ("profile", profile_observation_id),
            ("battle_log", battle_observation_id),
        ):
            source = connection.execute(
                """
                SELECT request_started_at, response_completed_at, http_status,
                       response_hash, archive_reference
                FROM collector_observations
                WHERE id = %s
                """,
                (observation_id,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO collector_endpoint_results (
                    attempt_id, endpoint, outcome, request_started_at,
                    response_completed_at, http_status, response_hash,
                    archive_reference, observation_id, request_count,
                    key_label
                ) VALUES (%s, %s, 'observed', %s, %s, %s, %s, %s, %s, 1, 'normal-a')
                """,
                (attempt_id, endpoint, *source, observation_id),
            )
        connection.commit()
    return profile_observation_id, battle_observation_id, profile_job_id, battle_job_id


def test_reset_baseline_evidence_is_created_from_one_go_attempt_and_is_versioned(
    database_url: str,
    archive_server,
) -> None:
    boundary = datetime(2026, 8, 4, 5, tzinfo=UTC)
    with domain_database(database_url) as connection_info:
        profile_observation_id, battle_observation_id, profile_job_id, battle_job_id = (
            _prepare_reset_baseline_pair(
                connection_info,
                archive_server,
                boundary=boundary,
            )
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            profile_result = processor.process_job(
                profile_job_id,
                owner="reset-profile-worker",
            )
            assert profile_result is not None and profile_result.outcome == "processed"
            with database.pool.connection() as connection:
                partial_rows = connection.execute(
                    """
                    SELECT profile_observation_id, battle_log_observation_id,
                           profile_valid, battle_log_valid, legacy_profile_only
                    FROM reset_baseline_evidence
                    ORDER BY id
                    """
                ).fetchall()
            assert partial_rows == [
                (profile_observation_id, battle_observation_id, True, False, False)
            ]
            with database.pool.connection() as connection:
                assert (
                    connection.execute(
                        """
                    SELECT count(*)
                    FROM python_processing_jobs
                    WHERE work_type = 'reconcile_ranked_day'
                    """
                    ).fetchone()[0]
                    == 0
                )

            battle_result = processor.process_job(
                battle_job_id,
                owner="reset-battle-worker",
            )
            assert battle_result is not None and battle_result.outcome == "processed"
            with database.pool.connection() as connection:
                rows = connection.execute(
                    """
                    SELECT profile_observation_id, battle_log_observation_id,
                           profile_valid, battle_log_valid, legacy_profile_only
                    FROM reset_baseline_evidence
                    ORDER BY id
                    """
                ).fetchall()
                reconciliation_count = connection.execute(
                    """
                    SELECT count(*)
                    FROM python_processing_jobs
                    WHERE work_type = 'reconcile_ranked_day'
                    """
                ).fetchone()[0]
            assert rows == [
                (profile_observation_id, battle_observation_id, True, False, False),
                (profile_observation_id, battle_observation_id, True, True, False),
            ]
            assert reconciliation_count == 1

            database.requeue_completed_job(battle_job_id)
            replay = processor.process_job(battle_job_id, owner="reset-battle-replay")
            assert replay is not None and replay.outcome == "processed"
            with database.pool.connection() as connection:
                versions = connection.execute(
                    "SELECT version, state FROM reset_baseline_evidence ORDER BY version"
                ).fetchall()
                assert (
                    connection.execute(
                        """
                    SELECT count(*)
                    FROM python_processing_jobs
                    WHERE work_type = 'reconcile_ranked_day'
                    """
                    ).fetchone()[0]
                    == 1
                )
            assert [(row[0], text(row[1])) for row in versions] == [
                (1, "partial"),
                (2, "complete"),
            ]
        finally:
            database.close()


def test_malformed_reset_evidence_is_failed_without_reconciliation_enqueue(
    database_url: str,
    archive_server,
) -> None:
    boundary = datetime(2026, 8, 4, 5, tzinfo=UTC)
    with domain_database(database_url) as connection_info:
        _profile_observation, _battle_observation, profile_job, battle_job = (
            _prepare_reset_baseline_pair(
                connection_info,
                archive_server,
                boundary=boundary,
                battle_body=b'{"items":',
            )
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            profile_result = processor.process_job(
                profile_job, owner="malformed-profile"
            )
            assert profile_result is not None and profile_result.outcome == "processed"
            battle_result = processor.process_job(battle_job, owner="malformed-battle")
            assert battle_result is not None
            assert battle_result.outcome == "failed"
            assert text(battle_result.category) == "malformed_json"
            with database.pool.connection() as connection:
                state, reasons = connection.execute(
                    "SELECT state, failure_reasons FROM reset_baseline_evidence ORDER BY version DESC LIMIT 1"
                ).fetchone()
                reconciliation_count = connection.execute(
                    """
                    SELECT count(*)
                    FROM python_processing_jobs
                    WHERE work_type = 'reconcile_ranked_day'
                    """
                ).fetchone()[0]
            assert text(state) == "failed"
            assert "battle_log_malformed_json" in [text(reason) for reason in reasons]
            assert reconciliation_count == 0
        finally:
            database.close()


def test_wrong_attempt_reset_evidence_is_failed_without_reconciliation_enqueue(
    database_url: str,
    archive_server,
) -> None:
    boundary = datetime(2026, 8, 4, 5, tzinfo=UTC)
    with domain_database(database_url) as connection_info:
        _profile_observation, _battle_observation, profile_job, battle_job = (
            _prepare_reset_baseline_pair(
                connection_info,
                archive_server,
                boundary=boundary,
                wrong_battle_attempt=True,
            )
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            profile_result = processor.process_job(
                profile_job, owner="wrong-attempt-profile"
            )
            assert profile_result is not None and profile_result.outcome == "processed"
            battle_result = processor.process_job(
                battle_job, owner="wrong-attempt-battle"
            )
            assert battle_result is not None and battle_result.outcome == "processed"
            with database.pool.connection() as connection:
                state, reasons = connection.execute(
                    "SELECT state, failure_reasons FROM reset_baseline_evidence ORDER BY version DESC LIMIT 1"
                ).fetchone()
                reconciliation_count = connection.execute(
                    """
                    SELECT count(*)
                    FROM python_processing_jobs
                    WHERE work_type = 'reconcile_ranked_day'
                    """
                ).fetchone()[0]
            assert text(state) == "failed"
            assert "battle_log_wrong_attempt" in [text(reason) for reason in reasons]
            assert reconciliation_count == 0
        finally:
            database.close()


def test_profile_and_battle_observations_process_independently_into_canonical_evidence(
    database_url: str,
    archive_server,
) -> None:
    observed_at = datetime(2026, 8, 4, 12, 5, tzinfo=UTC)
    with domain_database(database_url) as connection_info:
        profile_observation_id, profile_job_id = store_observation(
            connection_info,
            archive_server,
            occurrence_key="domain-profile",
            endpoint="profile",
            body=PROFILE_FIXTURE.read_bytes(),
            observed_at=observed_at,
            normalized_tag="#2PP",
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            profile_result = processor.process_once(owner="domain-profile-worker")

            assert profile_result is not None
            assert profile_result.job_id == profile_job_id
            assert profile_result.outcome == "processed"
            with database.pool.connection() as connection:
                outcome = connection.execute(
                    """
                    SELECT outcome, source_http_status
                    FROM observation_processing_outcomes
                    WHERE observation_id = %s
                    """,
                    (profile_observation_id,),
                ).fetchone()
                anchor = connection.execute(
                    """
                    SELECT outcome, current_league_season_id,
                           previous_league_season_id
                    FROM season_anchor_evidence
                    """
                ).fetchone()
            assert tuple(text(value) for value in outcome) == ("processed", 200)
            assert tuple(text(value) for value in anchor) == (
                "accepted",
                "1783918800",
                "1781499600",
            )

            attacker_body = BATTLE_FIXTURE.read_bytes()
            first_observation_id, _first_job = store_observation(
                connection_info,
                archive_server,
                occurrence_key="attacker-log-first",
                endpoint="battle_log",
                body=attacker_body,
                observed_at=observed_at + timedelta(minutes=1),
                normalized_tag="#2PP",
            )
            _repeat_observation_id, _repeat_job = store_observation(
                connection_info,
                archive_server,
                occurrence_key="attacker-log-repeat",
                endpoint="battle_log",
                body=attacker_body,
                observed_at=observed_at + timedelta(minutes=6),
                normalized_tag="#2PP",
            )

            first_result = processor.process_once(owner="battle-attacker-one")
            assert first_result is not None and first_result.outcome == "processed"
            with database.pool.connection() as connection:
                perspectives_before = connection.execute(
                    "SELECT perspective FROM battle_perspectives"
                ).fetchall()
            assert [text(row[0]) for row in perspectives_before] == ["attacker"]

            repeat_result = processor.process_once(owner="battle-attacker-repeat")
            assert repeat_result is not None and repeat_result.outcome == "processed"

            defender_payload = json.loads(attacker_body)
            defender_row = defender_payload["items"][0]
            defender_row["attackOrDefense"] = "defense"
            defender_row["opponent"] = {
                "tag": "#2PP",
                "name": "Synthetic Attacker",
                "trophies": 6040,
            }
            _defender_observation_id, _defender_job = store_observation(
                connection_info,
                archive_server,
                occurrence_key="defender-log",
                endpoint="battle_log",
                body=json.dumps({"items": [defender_row]}).encode(),
                observed_at=observed_at + timedelta(minutes=7),
                normalized_tag="#8PP",
            )
            defender_result = processor.process_once(owner="battle-defender")
            assert (
                defender_result is not None and defender_result.outcome == "processed"
            )

            with database.pool.connection() as connection:
                counts = connection.execute(
                    """
                    SELECT
                        (SELECT count(*) FROM legend_battles),
                        (SELECT count(*) FROM battle_evidence),
                        (SELECT count(*) FROM battle_perspectives),
                        (SELECT count(*) FROM known_player_discoveries)
                    """
                ).fetchone()
                battle = connection.execute(
                    """
                    SELECT b.disagreement_state, e.army_share_code,
                           e.attacker_gain, e.defender_loss, e.trophy_rule_version
                    FROM legend_battles AS b
                    JOIN battle_perspectives AS p
                      ON p.battle_id = b.id AND p.perspective = 'attacker'
                    JOIN battle_evidence AS e ON e.id = p.evidence_id
                    """
                ).fetchone()
                opponent = connection.execute(
                    """
                    SELECT active, eligibility_state
                    FROM players WHERE normalized_tag = '#8PP'
                    """
                ).fetchone()
                first_rows = connection.execute(
                    """
                    SELECT count(*) FROM battle_source_rows AS r
                    JOIN battle_log_observations AS l
                      ON l.id = r.battle_log_observation_id
                    WHERE l.observation_id = %s
                    """,
                    (first_observation_id,),
                ).fetchone()[0]

            assert counts == (1, 3, 2, 2)
            assert tuple(text(value) for value in battle) == (
                "agreed",
                "u1x0-2x1",
                40,
                40,
                "legend-trophy-allocation-v1",
            )
            assert tuple(text(value) for value in opponent) == (False, "unknown")
            assert first_rows == 2
        finally:
            database.close()


def test_conflicting_or_older_profiles_never_replace_last_accepted_current_profile(
    database_url: str,
    archive_server,
) -> None:
    accepted_at = datetime(2026, 8, 4, 12, 5, tzinfo=UTC)
    with domain_database(database_url) as connection_info:
        _accepted_observation_id, accepted_job_id = store_observation(
            connection_info,
            archive_server,
            occurrence_key="accepted-profile",
            endpoint="profile",
            body=PROFILE_FIXTURE.read_bytes(),
            observed_at=accepted_at,
            normalized_tag="#2PP",
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            accepted_result = processor.process_job(
                accepted_job_id, owner="accepted-profile-worker"
            )
            assert accepted_result is not None
            assert accepted_result.outcome == "processed"

            def _profile_rows() -> list[tuple]:
                with database.pool.connection() as connection:
                    return connection.execute(
                        """
                        SELECT
                            pv.id, pv.source_contract_state, pv.name, pv.trophies,
                            pv.league_tier_id, pv.league_tier_name,
                            pv.eligibility_state, pv.observed_at,
                            (SELECT count(*) FROM player_profile_effects AS e
                             WHERE e.profile_version_id = pv.id),
                            (SELECT outcome FROM observation_processing_outcomes AS o
                             WHERE o.observation_id = pv.observation_id),
                            p.current_profile_version_id, p.current_observed_at,
                            p.active, p.eligibility_state
                        FROM player_profile_versions AS pv
                        JOIN players AS p ON p.id = pv.player_id
                        WHERE p.normalized_tag = '#2PP'
                        ORDER BY pv.id
                        """
                    ).fetchall()

            accepted_rows = _profile_rows()
            assert len(accepted_rows) == 1
            accepted_version_id = accepted_rows[0][0]
            assert tuple(text(v) for v in accepted_rows[0][1:7]) == (
                "accepted",
                "Synthetic Legend I",
                6123,
                105000036,
                "Legend I",
                "eligible",
            )
            assert accepted_rows[0][7] == accepted_at
            assert accepted_rows[0][8] == 1
            assert text(accepted_rows[0][9]) == "processed"
            assert accepted_rows[0][10] == accepted_version_id
            assert accepted_rows[0][11] == accepted_at
            assert accepted_rows[0][12] is True
            assert text(accepted_rows[0][13]) == "eligible"

            # Newer conflicting evidence: materially different name, trophies,
            # league tier, and eligibility. It must stay visible historically
            # but must not become current product truth.
            conflicting_payload = json.loads(PROFILE_FIXTURE.read_bytes())
            conflicting_payload["name"] = "Conflicting Rename"
            conflicting_payload["trophies"] = 7100
            conflicting_payload["leagueTier"] = {
                "id": 999999999,
                "name": "Unexpected Tier",
            }
            _conflict_observation_id, conflict_job_id = store_observation(
                connection_info,
                archive_server,
                occurrence_key="conflicting-profile",
                endpoint="profile",
                body=json.dumps(conflicting_payload).encode(),
                observed_at=accepted_at + timedelta(hours=1),
                normalized_tag="#2PP",
            )
            conflict_result = processor.process_job(
                conflict_job_id, owner="conflicting-profile-worker"
            )
            assert conflict_result is not None
            assert conflict_result.outcome == "processed"

            conflict_rows = _profile_rows()
            assert len(conflict_rows) == 2
            conflicting_version_id = conflict_rows[1][0]
            assert tuple(text(v) for v in conflict_rows[1][1:7]) == (
                "conflict",
                "Conflicting Rename",
                7100,
                999999999,
                "Unexpected Tier",
                "uncertain",
            )
            assert conflict_rows[1][7] == accepted_at + timedelta(hours=1)
            assert conflict_rows[1][8] == 1
            assert text(conflict_rows[1][9]) == "processed"
            # Current pointer and current fields still point at the accepted
            # version; the later conflicting version must not win.
            assert conflict_rows[1][10] == accepted_version_id
            assert conflict_rows[1][11] == accepted_at
            assert conflict_rows[1][12] is True
            assert text(conflict_rows[1][13]) == "eligible"
            assert conflicting_version_id != accepted_version_id

            # Older accepted evidence arriving later cannot move the current
            # pointer backward.
            older_payload = json.loads(PROFILE_FIXTURE.read_bytes())
            older_payload["name"] = "Older Accepted Name"
            older_payload["trophies"] = 5555
            _older_observation_id, older_job_id = store_observation(
                connection_info,
                archive_server,
                occurrence_key="older-accepted-profile",
                endpoint="profile",
                body=json.dumps(older_payload).encode(),
                observed_at=accepted_at - timedelta(days=1),
                normalized_tag="#2PP",
            )
            older_result = processor.process_job(
                older_job_id, owner="older-accepted-profile-worker"
            )
            assert older_result is not None
            assert older_result.outcome == "processed"

            older_rows = _profile_rows()
            assert len(older_rows) == 3
            assert tuple(text(v) for v in older_rows[2][1:7]) == (
                "accepted",
                "Older Accepted Name",
                5555,
                105000036,
                "Legend I",
                "eligible",
            )
            assert older_rows[2][10] == accepted_version_id
            assert older_rows[2][11] == accepted_at
            assert older_rows[2][12] is True
            assert text(older_rows[2][13]) == "eligible"

            # Newer accepted evidence advances the current profile normally.
            newer_at = accepted_at + timedelta(days=1)
            newer_payload = json.loads(PROFILE_FIXTURE.read_bytes())
            newer_payload["name"] = "Newer Accepted Name"
            newer_payload["trophies"] = 6400
            _newer_observation_id, newer_job_id = store_observation(
                connection_info,
                archive_server,
                occurrence_key="newer-accepted-profile",
                endpoint="profile",
                body=json.dumps(newer_payload).encode(),
                observed_at=newer_at,
                normalized_tag="#2PP",
            )
            newer_result = processor.process_job(
                newer_job_id, owner="newer-accepted-profile-worker"
            )
            assert newer_result is not None
            assert newer_result.outcome == "processed"

            newer_rows = _profile_rows()
            assert len(newer_rows) == 4
            newer_version_id = newer_rows[3][0]
            assert tuple(text(v) for v in newer_rows[3][1:7]) == (
                "accepted",
                "Newer Accepted Name",
                6400,
                105000036,
                "Legend I",
                "eligible",
            )
            assert newer_rows[3][10] == newer_version_id
            assert newer_rows[3][11] == newer_at
            assert newer_rows[3][12] is True
            assert text(newer_rows[3][13]) == "eligible"

            # Equal-timestamp accepted evidence resolves deterministically by
            # immutable version id: the later version wins.
            equal_payload = json.loads(PROFILE_FIXTURE.read_bytes())
            equal_payload["name"] = "Equal-Time Accepted Name"
            equal_payload["trophies"] = 6300
            _equal_observation_id, equal_job_id = store_observation(
                connection_info,
                archive_server,
                occurrence_key="equal-time-accepted-profile",
                endpoint="profile",
                body=json.dumps(equal_payload).encode(),
                observed_at=newer_at,
                normalized_tag="#2PP",
            )
            equal_result = processor.process_job(
                equal_job_id, owner="equal-time-accepted-profile-worker"
            )
            assert equal_result is not None
            assert equal_result.outcome == "processed"

            equal_rows = _profile_rows()
            assert len(equal_rows) == 5
            equal_version_id = equal_rows[4][0]
            assert tuple(text(v) for v in equal_rows[4][1:7]) == (
                "accepted",
                "Equal-Time Accepted Name",
                6300,
                105000036,
                "Legend I",
                "eligible",
            )
            assert equal_version_id > newer_version_id
            assert equal_rows[4][10] == equal_version_id
            assert equal_rows[4][11] == newer_at
            assert equal_rows[4][12] is True
            assert text(equal_rows[4][13]) == "eligible"
        finally:
            database.close()


def test_canonical_battle_keeps_detail_disagreement_for_both_perspectives(
    database_url: str,
    archive_server,
) -> None:
    observed_at = datetime(2026, 8, 4, 12, 5, tzinfo=UTC)
    with domain_database(database_url) as connection_info:
        attacker_body = BATTLE_FIXTURE.read_bytes()
        _attacker_observation_id, attacker_job_id = store_observation(
            connection_info,
            archive_server,
            occurrence_key="disagreement-attacker",
            endpoint="battle_log",
            body=attacker_body,
            observed_at=observed_at,
            normalized_tag="#2PP",
        )
        defender_payload = json.loads(attacker_body)
        defender_row = defender_payload["items"][0]
        defender_row["attackOrDefense"] = "defense"
        defender_row["opponent"] = {
            "tag": "#2PP",
            "name": "Synthetic Attacker",
            "trophies": 6022,
        }
        defender_row["armyShareCode"] = "different-share-code"
        _defender_observation_id, defender_job_id = store_observation(
            connection_info,
            archive_server,
            occurrence_key="disagreement-defender",
            endpoint="battle_log",
            body=json.dumps({"items": [defender_row]}).encode(),
            observed_at=observed_at + timedelta(minutes=1),
            normalized_tag="#8PP",
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            attacker_result = processor.process_job(
                attacker_job_id,
                owner="disagreement-attacker",
            )
            defender_result = processor.process_job(
                defender_job_id,
                owner="disagreement-defender",
            )
            assert (
                attacker_result is not None and attacker_result.outcome == "processed"
            )
            assert (
                defender_result is not None and defender_result.outcome == "processed"
            )
            with database.pool.connection() as connection:
                state, disagreement_fields = connection.execute(
                    "SELECT disagreement_state, disagreement_fields FROM legend_battles"
                ).fetchone()
                perspectives = connection.execute(
                    """
                    SELECT p.perspective, e.id, e.army_share_code
                    FROM battle_perspectives AS p
                    JOIN battle_evidence AS e ON e.id = p.evidence_id
                    ORDER BY p.perspective
                    """
                ).fetchall()
            assert text(state) == "disagreement"
            assert {text(row[0]) for row in perspectives} == {"attacker", "defender"}
            assert len({row[1] for row in perspectives}) == 2
            assert [text(field) for field in disagreement_fields] == ["army_share_code"]
        finally:
            database.close()


def test_official_top_200_publishes_only_complete_atomic_versions(
    database_url: str,
    archive_server,
) -> None:
    observed_at = datetime(2026, 8, 4, 12, 5, tzinfo=UTC)
    with domain_database(database_url) as connection_info:
        complete_observation_id, _complete_job = store_observation(
            connection_info,
            archive_server,
            occurrence_key="official-complete",
            endpoint="global_player_rankings",
            body=RANKING_FIXTURE.read_bytes(),
            observed_at=observed_at,
            normalized_tag=None,
        )
        database, processor = _processor(connection_info, archive_server)
        try:
            complete_result = processor.process_once(owner="official-complete")
            assert (
                complete_result is not None and complete_result.outcome == "processed"
            )

            partial = json.loads(RANKING_FIXTURE.read_bytes())
            partial["items"].pop()
            store_observation(
                connection_info,
                archive_server,
                occurrence_key="official-partial",
                endpoint="global_player_rankings",
                body=json.dumps(partial).encode(),
                observed_at=observed_at + timedelta(minutes=5),
                normalized_tag=None,
            )
            partial_result = processor.process_once(owner="official-partial")
            assert partial_result is not None and partial_result.outcome == "processed"

            store_observation(
                connection_info,
                archive_server,
                occurrence_key="official-malformed",
                endpoint="global_player_rankings",
                body=b'{"items":',
                observed_at=observed_at + timedelta(minutes=10),
                normalized_tag=None,
            )
            malformed_result = processor.process_once(owner="official-malformed")
            assert malformed_result is not None
            assert malformed_result.outcome == "failed"
            assert malformed_result.category == "malformed_json"

            with database.pool.connection() as connection:
                attempts = connection.execute(
                    """
                    SELECT outcome FROM official_top200_attempts ORDER BY observed_at
                    """
                ).fetchall()
                published = connection.execute(
                    """
                    SELECT v.observation_id,
                           (SELECT count(*) FROM official_top200_entries AS e
                            WHERE e.version_id = v.id)
                    FROM official_top200_versions AS v
                    """
                ).fetchone()
                active_count = connection.execute(
                    "SELECT count(*) FROM players WHERE active = true"
                ).fetchone()[0]

            assert [text(row[0]) for row in attempts] == [
                "official_observed",
                "official_partial",
                "malformed",
            ]
            assert published == (complete_observation_id, 200)
            assert active_count == 0
        finally:
            database.close()
