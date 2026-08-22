from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from domain_test_support import domain_database, store_observation, text

from clashlens.api_db import ApiDatabase
from clashlens.archive import S3ArchiveReader
from clashlens.army_analytics import (
    ArmyAnalyticsSelection,
    ArmyAnalyticsUnavailable,
)
from clashlens.db import Database
from clashlens.worker import ObservationProcessor

DAY_START = datetime(2026, 8, 4, 5, tzinfo=UTC)
SEASON_ID = "1783918800"
DAY_NUMBER = 23
FIXTURE_CODE = "h0p9e14_32d1x53u2x58-1x97s2x2"
PARTIAL_CODE = "u2x58-3x9999"
DEFENDER_CODE = "u1x51"


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


def _row(attack: bool, opponent: str, code: str, ts: datetime, stars: int, dest: int):
    return {
        "battleType": "legend",
        "attack": attack,
        "battleTimestamp": ts.isoformat().replace("+00:00", "Z"),
        "stars": stars,
        "destructionPercentage": dest,
        "opponentPlayerTag": opponent,
        "opponentName": "Opp",
        "opponentTownHallLevel": 17,
        "armyShareCode": code,
    }


def _event(battle_id: int, lens: str, ts: datetime, stars: int, dest: int, change: int):
    return {
        "battle_id": str(battle_id),
        "lens": lens,
        "battle_timestamp": ts.isoformat().replace("+00:00", "Z"),
        "stars": stars,
        "destruction_percentage": dest,
        "trophy_change": change,
        "opponent": {"tag": "#8PP" if lens == "offense" else "#2PP", "name": "Opp"},
        "included": True,
    }


def _publish_day(
    database: Database,
    player_tag: str,
    events: list[dict],
    *,
    start_trophies: int | None = 6000,
) -> None:
    with database.pool.connection() as connection:
        player_id = connection.execute(
            "SELECT id FROM players WHERE normalized_tag = %s", (player_tag,)
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO ranked_day_versions (
                player_id, ranked_day_start, ranked_day_end,
                official_season_id, season_day_number,
                season_anchor_rule_version, reconciliation_rule_version,
                result_hash, version, state, confidence, input_hash,
                evidence_complete, coverage_complete, start_trophies
            ) VALUES (
                %s, %s, %s, %s, %s, 'legend-season-anchor-v1',
                'legend-ranked-day-v1', repeat('a', 64), 1,
                'Complete', 'exact', repeat('b', 64), true, true, %s
            )
            """,
            (
                player_id,
                DAY_START,
                DAY_START + timedelta(days=1),
                SEASON_ID,
                DAY_NUMBER,
                start_trophies,
            ),
        )
        connection.execute(
            """
            INSERT INTO api_player_daily_logs (
                player_id, ranked_day_start, version, state, coverage,
                battles, ranked_day_end, official_season_id, season_day_number
            ) VALUES (%s, %s, 1, 'Complete', 'complete', %s, %s, %s, %s)
            """,
            (
                player_id,
                DAY_START,
                json.dumps(events),
                DAY_START + timedelta(days=1),
                SEASON_ID,
                DAY_NUMBER,
            ),
        )
        database._enqueue_army_analytics(connection, ranked_day_start=DAY_START)


def _army_job(database: Database) -> int:
    with database.pool.connection() as connection:
        return int(
            connection.execute(
                """
                SELECT id FROM python_processing_jobs
                WHERE work_type = 'build_army_analytics'
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()[0]
        )


def _seed_frozen_snapshot(
    database: Database, entries: list[tuple[int, int]], *, stale_first: bool = False
) -> int:
    """entries: ordered (player_id,) by position starting at 1."""
    with database.pool.connection() as connection:
        observation_id = connection.execute(
            "SELECT id FROM collector_observations ORDER BY id LIMIT 1"
        ).fetchone()[0]
        snapshot_id = connection.execute(
            """
            INSERT INTO leaderboard_snapshots (
                snapshot_kind, boundary_at, version, ordering_rule_version,
                freshness_rule_version, state, measured_coverage, stale_entry_count
            ) VALUES ('frozen', %s, 1, 'ordering-v1', 'freshness-v1', 'published', 1.0, 0)
            RETURNING id
            """,
            (DAY_START + timedelta(days=1),),
        ).fetchone()[0]
        for position, (player_id,) in enumerate(entries, start=1):
            snapshot_position = position if position == 1 else position + 4
            connection.execute(
                """
                INSERT INTO leaderboard_snapshot_entries (
                    snapshot_id, position, player_id, trophies,
                    trophy_observation_id, trophy_observed_at,
                    observation_age_seconds, freshness, confidence, tie_hash
                ) VALUES (%s, %s, %s, 6000, %s, %s, %s, %s, %s, repeat('c', 64))
                """,
                (
                    snapshot_id,
                    snapshot_position,
                    player_id,
                    observation_id,
                    DAY_START,
                    10 if not stale_first or position > 1 else 100_000,
                    "stale" if stale_first and position == 1 else "fresh",
                    "uncertain" if stale_first and position == 1 else "confirmed",
                ),
            )
        return snapshot_id


def _selection(**changes) -> ArmyAnalyticsSelection:
    values = {
        "lens": "offense",
        "season": SEASON_ID,
        "start_day": DAY_NUMBER,
        "end_day": DAY_NUMBER,
        "population": "trophies-5000-9000",
        "category": "troops",
        "sort": "usage-rate",
    }
    values.update(changes)
    return ArmyAnalyticsSelection.parse(**values)


def test_publication_writer_serves_reproducible_perspective_results(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as ci:
        ts1 = DAY_START + timedelta(hours=1)
        ts2 = DAY_START + timedelta(hours=3)
        _, j1 = store_observation(
            ci, archive_server, occurrence_key="pub-a1", endpoint="battle_log",
            body=json.dumps({"items": [_row(True, "#8PP", FIXTURE_CODE, ts1, 3, 100)]}).encode(),
            observed_at=ts1 + timedelta(minutes=1), normalized_tag="#2PP",
        )
        _, j2 = store_observation(
            ci, archive_server, occurrence_key="pub-a2", endpoint="battle_log",
            body=json.dumps({"items": [_row(True, "#9PP", PARTIAL_CODE, ts2, 1, 50)]}).encode(),
            observed_at=ts2 + timedelta(minutes=1), normalized_tag="#2PP",
        )
        _, j3 = store_observation(
            ci, archive_server, occurrence_key="pub-d1", endpoint="battle_log",
            body=json.dumps({"items": [_row(False, "#2PP", DEFENDER_CODE, ts1, 3, 100)]}).encode(),
            observed_at=ts1 + timedelta(minutes=2), normalized_tag="#8PP",
        )
        database, processor = _processor(ci, archive_server)
        try:
            for index, job in enumerate((j1, j2, j3)):
                assert processor.process_job(job, owner=f"ingest-{index}").outcome in (
                    "processed",
                    "processed_with_gaps",
                )
            with database.pool.connection() as connection:
                battles = {
                    (text(a), text(d)): int(bid)
                    for a, d, bid in connection.execute(
                        """
                        SELECT atk.normalized_tag, def.normalized_tag, b.id
                        FROM legend_battles b
                        JOIN players atk ON atk.id = b.attacker_player_id
                        JOIN players def ON def.id = b.defender_player_id
                        """
                    ).fetchall()
                }
            attacker_vs_defender = battles[("#2PP", "#8PP")]
            attacker_vs_other = battles[("#2PP", "#9PP")]

            # Publish both perspectives of the completed Legend day.
            _publish_day(
                database,
                "#2PP",
                [
                    _event(attacker_vs_defender, "offense", ts1, 3, 100, 35),
                    _event(attacker_vs_other, "offense", ts2, 1, 50, 12),
                ],
            )
            _publish_day(
                database,
                "#8PP",
                [_event(attacker_vs_defender, "defense", ts1, 3, 100, -35)],
            )
            job_id = _army_job(database)
            result = processor.process_job(job_id, owner="analytics")
            assert result is not None and result.outcome == "processed"

            # Facts exist per lens and keep battle-time trophies.
            with database.pool.connection() as connection:
                facts = {
                    (int(r[0]), text(r[1])): r
                    for r in connection.execute(
                        """
                        SELECT battle_id, lens, army_state, battle_time_trophies,
                               perspective_disagreement
                        FROM army_analytics_battle_facts WHERE is_current
                        """
                    ).fetchall()
                }
            assert set(facts) == {
                (attacker_vs_defender, "offense"),
                (attacker_vs_other, "offense"),
                (attacker_vs_defender, "defense"),
            }
            assert facts[(attacker_vs_defender, "offense")][2] == "decoded"
            assert facts[(attacker_vs_other, "offense")][2] == "partial"
            assert facts[(attacker_vs_defender, "defense")][2] == "decoded"
            assert facts[(attacker_vs_defender, "offense")][3] == 6000
            assert facts[(attacker_vs_other, "offense")][3] == 6035

            api = ApiDatabase(ci)
            try:
                # Offense lens: partial known component counts individually.
                selection = _selection()
                first = api.get_army_analytics(selection)
                assert first is not None
                assert first["total_attacks"] == 2
                assert first["usable_army_sample"] == 2
                assert first["army_states"]["fully_decoded"] == 1
                assert first["army_states"]["partial"] == 1
                assert first["army_states_sum_confirmed"] is True
                assert first["unknown_affected_attacks"] == 1
                assert first["unknown_component_occurrences"] == 1
                troop_row = next(
                    row for row in first["rows"] if row["key"] == "troop:58"
                )
                assert troop_row["usage_count"] == 2
                assert troop_row["usage_denominator"] == 2
                identity = first["publication_identity"]
                assert identity.startswith("army-publication-")
                assert "-v1" in identity
                assert first["reproducibility"]["official_season_id"] == SEASON_ID

                # Same selection twice is one stable publication.
                second = api.get_army_analytics(selection)
                assert second is not None
                assert second["publication_identity"] == identity

                # Defense lens uses the defender's own accepted report.
                defense = api.get_army_analytics(
                    _selection(lens="defense", category="siege")
                )
                assert defense is not None
                assert defense["total_attacks"] == 1
                siege_row = next(
                    row for row in defense["rows"] if row["key"] == "troop:51"
                )
                assert siege_row["usage_count"] == 1
                assert siege_row["usage_denominator"] == 1

                # Per-battle army display keeps perspectives separate.
                attacker_army = api.get_battle_army(attacker_vs_defender, "attacker")
                defender_army = api.get_battle_army(attacker_vs_defender, "defender")
                assert attacker_army is not None and defender_army is not None
                attacker_ids = {c["typed_id"] for c in attacker_army["components"]}
                defender_ids = {c["typed_id"] for c in defender_army["components"]}
                assert "troop:58" in attacker_ids and "troop:51" not in attacker_ids
                assert "troop:51" in defender_ids and "troop:58" not in defender_ids

                # Frozen Top-N membership from the final-day snapshot.
                with database.pool.connection() as connection:
                    defender_id = connection.execute(
                        "SELECT id FROM players WHERE normalized_tag = '#8PP'"
                    ).fetchone()[0]
                    attacker_id = connection.execute(
                        "SELECT id FROM players WHERE normalized_tag = '#2PP'"
                    ).fetchone()[0]
                _seed_frozen_snapshot(
                    database, [(defender_id,), (attacker_id,)]
                )
                top5 = api.get_army_analytics(_selection(lens="defense", population="top-5"))
                assert top5 is not None
                assert top5["total_attacks"] == 1
                assert top5["reproducibility"]["snapshot_versions"] == [1]
                # The attacker sits at position 6 of the frozen snapshot, so a
                # Top-5 offense cohort excludes both of their attacks.
                top5_offense = api.get_army_analytics(
                    _selection(population="top-5")
                )
                assert top5_offense is not None
                assert top5_offense["total_attacks"] == 0
                assert top5_offense["rows"] == []

                # A withheld day inside the range names the affected day.
                with pytest.raises(ArmyAnalyticsUnavailable) as unavailable:
                    api.get_army_analytics(_selection(start_day=22))
                assert unavailable.value.affected_days == [22]

                # Corrected evidence supersedes the publication atomically.
                # A corrected ranked-day version republishes under v2.
                corrected_events = [
                    _event(attacker_vs_defender, "offense", ts1, 3, 100, 35),
                    _event(attacker_vs_other, "offense", ts2, 2, 75, 12),
                ]
                _publish_day_correction(database, "#2PP", corrected_events)
                correction_job = _army_job(database)
                correction = processor.process_job(correction_job, owner="correction")
                assert correction is not None and correction.outcome == "processed"
                third = api.get_army_analytics(selection)
                assert third is not None
                assert third["publication_identity"].endswith("-v2")
                two_star_row = next(
                    row
                    for row in third["rows"]
                    if row["key"] == "troop:58"
                )
                assert two_star_row["star_counts"][2] == 1
                with database.pool.connection() as connection:
                    superseded_current = connection.execute(
                        """
                        SELECT p1.is_current, p2.version
                        FROM army_analytics_publications p1
                        JOIN army_analytics_publications p2 ON p2.supersedes_id = p1.id
                        """
                    ).fetchall()
                # Exactly the corrected publication was superseded; history stays.
                assert [int(r[1]) for r in superseded_current] == [2]
                assert all(r[0] is False for r in superseded_current)
            finally:
                api.close()
        finally:
            database.close()


def _publish_day_correction(
    database: Database,
    player_tag: str,
    events: list[dict],
) -> None:
    with database.pool.connection() as connection:
        player_id = connection.execute(
            "SELECT id FROM players WHERE normalized_tag = %s", (player_tag,)
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO ranked_day_versions (
                player_id, ranked_day_start, ranked_day_end,
                official_season_id, season_day_number,
                season_anchor_rule_version, reconciliation_rule_version,
                result_hash, version, state, confidence, input_hash,
                evidence_complete, coverage_complete, start_trophies
            ) VALUES (
                %s, %s, %s, %s, %s, 'legend-season-anchor-v1',
                'legend-ranked-day-v1', repeat('f', 64), 2,
                'Complete', 'exact', repeat('e', 64), true, true, 6000
            )
            """,
            (
                player_id,
                DAY_START,
                DAY_START + timedelta(days=1),
                SEASON_ID,
                DAY_NUMBER,
            ),
        )
        connection.execute(
            """
            INSERT INTO api_player_daily_logs (
                player_id, ranked_day_start, version, state, coverage,
                battles, ranked_day_end, official_season_id, season_day_number
            ) VALUES (%s, %s, 2, 'Complete', 'complete', %s, %s, %s, %s)
            """,
            (
                player_id,
                DAY_START,
                json.dumps(events),
                DAY_START + timedelta(days=1),
                SEASON_ID,
                DAY_NUMBER,
            ),
        )
        database._enqueue_army_analytics(connection, ranked_day_start=DAY_START)


def test_completed_day_without_army_job_is_unavailable_not_false_empty(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as ci:
        ts = DAY_START + timedelta(hours=1)
        _, job = store_observation(
            ci, archive_server, occurrence_key="marker-a1", endpoint="battle_log",
            body=json.dumps({"items": [_row(True, "#8PP", FIXTURE_CODE, ts, 3, 100)]}).encode(),
            observed_at=ts + timedelta(minutes=1), normalized_tag="#2PP",
        )
        database, processor = _processor(ci, archive_server)
        api = ApiDatabase(ci)
        try:
            assert processor.process_job(job, owner="ingest").outcome == "processed"
            with database.pool.connection() as connection:
                battle_id = int(connection.execute("SELECT max(id) FROM legend_battles").fetchone()[0])
            _publish_day(database, "#2PP", [_event(battle_id, "offense", ts, 3, 100, 35)])
            # The ranked-day publication is complete but build_army_analytics has
            # not run: the selection must be unavailable, never a false empty.
            with pytest.raises(ArmyAnalyticsUnavailable) as unavailable:
                api.get_army_analytics(_selection())
            assert unavailable.value.affected_days == [DAY_NUMBER]
        finally:
            api.close()
            database.close()


def test_missing_start_trophies_keep_facts_and_report_missing_evidence(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as ci:
        ts1 = DAY_START + timedelta(hours=1)
        ts2 = DAY_START + timedelta(hours=3)
        _, j1 = store_observation(
            ci, archive_server, occurrence_key="null-trophies-a1", endpoint="battle_log",
            body=json.dumps({"items": [_row(True, "#8PP", FIXTURE_CODE, ts1, 3, 100)]}).encode(),
            observed_at=ts1 + timedelta(minutes=1), normalized_tag="#2PP",
        )
        _, j2 = store_observation(
            ci, archive_server, occurrence_key="null-trophies-a2", endpoint="battle_log",
            body=json.dumps({"items": [_row(True, "#9PP", PARTIAL_CODE, ts2, 1, 50)]}).encode(),
            observed_at=ts2 + timedelta(minutes=1), normalized_tag="#2PP",
        )
        database, processor = _processor(ci, archive_server)
        api = ApiDatabase(ci)
        try:
            for index, job in enumerate((j1, j2)):
                processor.process_job(job, owner=f"ingest-{index}")
            with database.pool.connection() as connection:
                battles = {
                    (text(a), text(d)): int(bid)
                    for a, d, bid in connection.execute(
                        """
                        SELECT atk.normalized_tag, def.normalized_tag, b.id
                        FROM legend_battles b
                        JOIN players atk ON atk.id = b.attacker_player_id
                        JOIN players def ON def.id = b.defender_player_id
                        """
                    ).fetchall()
                }
                attacker_id = int(connection.execute(
                    "SELECT id FROM players WHERE normalized_tag = '#2PP'"
                ).fetchone()[0])
                defender_id = int(connection.execute(
                    "SELECT id FROM players WHERE normalized_tag = '#8PP'"
                ).fetchone()[0])
            _publish_day(
                database,
                "#2PP",
                [
                    _event(battles[("#2PP", "#8PP")], "offense", ts1, 3, 100, 35),
                    _event(battles[("#2PP", "#9PP")], "offense", ts2, 1, 50, 12),
                ],
                start_trophies=None,
            )
            result = processor.process_job(_army_job(database), owner="analytics")
            assert result is not None and result.outcome == "processed"

            # Facts are retained with unknown battle-time trophies.
            with database.pool.connection() as connection:
                trophies = [
                    row[0]
                    for row in connection.execute(
                        """
                        SELECT battle_time_trophies FROM army_analytics_battle_facts
                        WHERE lens='offense' AND is_current ORDER BY battle_id
                        """
                    ).fetchall()
                ]
            assert trophies == [None, None]

            # Trophy-range filter excludes them and reports missing evidence.
            trophy_filtered = api.get_army_analytics(
                _selection(population="trophies-5000-9000")
            )
            assert trophy_filtered is not None
            assert trophy_filtered["total_attacks"] == 0
            assert trophy_filtered["missing_trophy_membership_evidence"] == 2

            # Frozen cohort filters still include the attacks.
            _seed_frozen_snapshot(database, [(attacker_id,), (defender_id,)])
            top5 = api.get_army_analytics(_selection(population="top-5"))
            assert top5 is not None
            assert top5["total_attacks"] == 2
            assert top5["missing_trophy_membership_evidence"] == 0
        finally:
            api.close()
            database.close()


def test_stale_snapshot_membership_is_reported_as_cohort_evidence(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as ci:
        ts = DAY_START + timedelta(hours=1)
        _, job = store_observation(
            ci, archive_server, occurrence_key="stale-cohort-a1", endpoint="battle_log",
            body=json.dumps({"items": [_row(True, "#8PP", FIXTURE_CODE, ts, 3, 100)]}).encode(),
            observed_at=ts + timedelta(minutes=1), normalized_tag="#2PP",
        )
        database, processor = _processor(ci, archive_server)
        api = ApiDatabase(ci)
        try:
            processor.process_job(job, owner="ingest")
            with database.pool.connection() as connection:
                battle_id = int(connection.execute("SELECT max(id) FROM legend_battles").fetchone()[0])
                attacker_id = int(connection.execute(
                    "SELECT id FROM players WHERE normalized_tag = '#2PP'"
                ).fetchone()[0])
                defender_id = int(connection.execute(
                    "SELECT id FROM players WHERE normalized_tag = '#8PP'"
                ).fetchone()[0])
            _publish_day(database, "#2PP", [_event(battle_id, "offense", ts, 3, 100, 35)])
            processor.process_job(_army_job(database), owner="analytics")
            _seed_frozen_snapshot(
                database, [(attacker_id,), (defender_id,)], stale_first=True
            )
            top2 = api.get_army_analytics(_selection(population="top-5"))
            assert top2 is not None
            # Position membership still includes the stale entry; the weakness
            # stays visible instead of silently shrinking the cohort.
            assert top2["total_attacks"] == 1
            evidence = top2["cohort_evidence"]
            assert evidence["stale_or_uncertain_cohort_members"] == 1
            assert evidence["streak_excluded_players"] == 0
        finally:
            api.close()
            database.close()


def test_correction_with_unchanged_aggregates_creates_publication_history(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as ci:
        ts = DAY_START + timedelta(hours=1)
        _, job = store_observation(
            ci, archive_server, occurrence_key="same-aggregates-a1", endpoint="battle_log",
            body=json.dumps({"items": [_row(True, "#8PP", FIXTURE_CODE, ts, 3, 100)]}).encode(),
            observed_at=ts + timedelta(minutes=1), normalized_tag="#2PP",
        )
        database, processor = _processor(ci, archive_server)
        api = ApiDatabase(ci)
        try:
            processor.process_job(job, owner="ingest")
            with database.pool.connection() as connection:
                battle_id = int(connection.execute("SELECT max(id) FROM legend_battles").fetchone()[0])
            events = [_event(battle_id, "offense", ts, 3, 100, 35)]
            _publish_day(database, "#2PP", events)
            processor.process_job(_army_job(database), owner="analytics")
            selection = _selection()
            first = api.get_army_analytics(selection)
            assert first is not None
            assert first["publication_identity"].endswith("-v1")

            # A corrected source publication with identical battle facts keeps
            # every aggregate unchanged but must still create v2 history.
            _publish_day_correction(database, "#2PP", events)
            correction = processor.process_job(_army_job(database), owner="correction")
            assert correction is not None and correction.outcome == "processed"
            second = api.get_army_analytics(selection)
            assert second is not None
            assert second["publication_identity"].endswith("-v2")
            assert second["rows"] == first["rows"]
            assert (
                second["reproducibility"]["source_evidence_hash"]
                != first["reproducibility"]["source_evidence_hash"]
            )
            with database.pool.connection() as connection:
                versions = [
                    int(row[0])
                    for row in connection.execute(
                        "SELECT version FROM army_analytics_publications ORDER BY version"
                    ).fetchall()
                ]
            assert versions == [1, 2]
        finally:
            api.close()
            database.close()


def test_perspective_disagreement_changes_fact_input_and_version(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as ci:
        ts = DAY_START + timedelta(hours=1)
        _, job = store_observation(
            ci, archive_server, occurrence_key="disagreement-hash-a1", endpoint="battle_log",
            body=json.dumps({"items": [_row(True, "#8PP", FIXTURE_CODE, ts, 3, 100)]}).encode(),
            observed_at=ts + timedelta(minutes=1), normalized_tag="#2PP",
        )
        database, processor = _processor(ci, archive_server)
        try:
            processor.process_job(job, owner="ingest")
            with database.pool.connection() as connection:
                battle_id = int(connection.execute("SELECT max(id) FROM legend_battles").fetchone()[0])
            _publish_day(database, "#2PP", [_event(battle_id, "offense", ts, 3, 100, 35)])
            processor.process_job(_army_job(database), owner="analytics")
            with database.pool.connection() as connection:
                before = connection.execute(
                    """
                    SELECT version, input_hash, perspective_disagreement
                    FROM army_analytics_battle_facts WHERE lens='offense' AND is_current
                    """
                ).fetchone()
            assert before is not None and before[2] is False
            # A later disagreement on the same battle changes the fact input.
            with database.pool.connection() as connection:
                connection.execute(
                    "UPDATE legend_battles SET disagreement_state='disagreement' WHERE id=%s",
                    (battle_id,),
                )
                prior_job = int(connection.execute(
                    """
                    SELECT id FROM python_processing_jobs
                    WHERE work_type = 'build_army_analytics' ORDER BY id DESC LIMIT 1
                    """
                ).fetchone()[0])
            database.requeue_completed_job(prior_job)
            processor.process_job(prior_job, owner="redisagree")
            with database.pool.connection() as connection:
                after = connection.execute(
                    """
                    SELECT version, input_hash, perspective_disagreement
                    FROM army_analytics_battle_facts
                    WHERE lens='offense' ORDER BY version DESC LIMIT 1
                    """
                ).fetchone()
            assert after is not None
            assert int(after[0]) == int(before[0]) + 1
            assert text(after[1]) != text(before[1])
            assert after[2] is True
        finally:
            database.close()


def test_current_season_clipped_below_start_day_is_unavailable(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as ci:
        ts = DAY_START + timedelta(hours=1)
        _, job = store_observation(
            ci, archive_server, occurrence_key="clip-a1", endpoint="battle_log",
            body=json.dumps({"items": [_row(True, "#8PP", FIXTURE_CODE, ts, 3, 100)]}).encode(),
            observed_at=ts + timedelta(minutes=1), normalized_tag="#2PP",
        )
        database, processor = _processor(ci, archive_server)
        api = ApiDatabase(ci)
        try:
            processor.process_job(job, owner="ingest")
            with database.pool.connection() as connection:
                battle_id = int(connection.execute("SELECT max(id) FROM legend_battles").fetchone()[0])
            _publish_day(database, "#2PP", [_event(battle_id, "offense", ts, 3, 100, 35)])
            # The latest completed Legend day is 23; a request starting at 28
            # clips to an empty range and must name the affected days.
            with pytest.raises(ArmyAnalyticsUnavailable) as unavailable:
                api.get_army_analytics(
                    _selection(season="current", start_day=28, end_day=28)
                )
            assert unavailable.value.affected_days == [28]
        finally:
            api.close()
            database.close()
