from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from clashlens_prototype.archive import S3ArchiveReader
from clashlens_prototype.db import Database
from clashlens_prototype.worker import ObservationProcessor
from domain_test_support import domain_database, store_observation, text

PROFILE_FIXTURE = Path(__file__).parents[1] / "testdata" / "legend_i_profile_v1.json"
BATTLE_FIXTURE = Path(__file__).parents[1] / "testdata" / "legend_i_battle_log_v1.json"
RANKING_FIXTURE = Path(__file__).parents[1] / "testdata" / "global_top_200_v1.json"


def _processor(connection_info: str, archive_server) -> tuple[Database, ObservationProcessor]:
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
            assert defender_result is not None and defender_result.outcome == "processed"

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
            assert complete_result is not None and complete_result.outcome == "processed"

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
