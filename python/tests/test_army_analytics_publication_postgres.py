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
    CurrentSeasonEmpty,
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
    day_start: datetime = DAY_START,
    day_number: int = DAY_NUMBER,
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
                day_start,
                day_start + timedelta(days=1),
                SEASON_ID,
                day_number,
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
                day_start,
                json.dumps(events),
                day_start + timedelta(days=1),
                SEASON_ID,
                day_number,
            ),
        )
        database._enqueue_army_analytics(connection, ranked_day_start=day_start)


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
                assert first["reproducibility"]["official_season_id"] == SEASON_ID

                # Reads calculate from retained facts without persistent
                # per-selection storage. Identical inputs keep one identity;
                # arbitrary URL-backed trophy ranges get distinct identities.
                second = api.get_army_analytics(selection)
                assert second is not None
                assert second["publication_identity"] == identity
                other_ranges = [
                    api.get_army_analytics(
                        _selection(population=f"trophies-5000-{maximum}")
                    )
                    for maximum in (8998, 8999)
                ]
                assert all(result is not None for result in other_ranges)
                assert len(
                    {
                        identity,
                        *(result["publication_identity"] for result in other_ranges if result),
                    }
                ) == 3
                with database.pool.connection() as connection:
                    assert connection.execute(
                        "SELECT to_regclass('army_analytics_publications')"
                    ).fetchone()[0] is None

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

                # Corrected evidence changes the deterministic identity.
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
                assert third["publication_identity"] != identity
                two_star_row = next(
                    row
                    for row in third["rows"]
                    if row["key"] == "troop:58"
                )
                assert two_star_row["star_counts"][2] == 1
                with database.pool.connection() as connection:
                    assert connection.execute(
                        "SELECT to_regclass('army_analytics_publications')"
                    ).fetchone()[0] is None
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


def test_correction_with_unchanged_aggregates_changes_deterministic_identity(
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

            # A corrected source with identical aggregates still changes the
            # identity because retained source evidence changed.
            _publish_day_correction(database, "#2PP", events)
            correction = processor.process_job(_army_job(database), owner="correction")
            assert correction is not None and correction.outcome == "processed"
            second = api.get_army_analytics(selection)
            assert second is not None
            assert second["publication_identity"] != first["publication_identity"]
            assert second["rows"] == first["rows"]
            assert (
                second["reproducibility"]["source_evidence_hash"]
                != first["reproducibility"]["source_evidence_hash"]
            )
            with database.pool.connection() as connection:
                assert connection.execute(
                    "SELECT to_regclass('army_analytics_publications')"
                ).fetchone()[0] is None
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


def _insert_confirmed_anchor(
    database: Database, current_season_id: str, previous_season_id: str
) -> None:
    """Insert one confirmed season anchor without replaying profile intake."""
    with database.pool.connection() as connection:
        player_id = connection.execute(
            "SELECT id FROM players WHERE normalized_tag = '#2PP'"
        ).fetchone()[0]
        observation_id = int(
            connection.execute("SELECT max(id) FROM collector_observations").fetchone()[0]
        )
        profile_version_id = connection.execute(
            """
            INSERT INTO player_profile_versions (
                player_id, observation_id, normalized_tag, endpoint_version,
                schema_version, parser_version, observed_at, source_http_status,
                name, trophies, league_tier_id, league_tier_name,
                eligibility_state, current_league_season_id,
                previous_league_season_id, profile_json
            ) VALUES (
                %s, %s, '#2PP', 'v1', 'profile-schema-v1', 'test-fixture',
                %s, 200, 'Opp', 6000, 29000022, 'Legend League', 'eligible',
                %s, %s, '{}'::jsonb
            ) RETURNING id
            """,
            (player_id, observation_id, DAY_START, current_season_id, previous_season_id),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO legend_season_anchors (
                current_league_season_id, previous_league_season_id,
                current_start, previous_start, anchor_rule_version,
                source_profile_version_id, state
            ) VALUES (%s, %s, %s, %s - interval '28 days',
                      'legend-season-anchor-v1', %s, 'confirmed')
            """,
            (current_season_id, previous_season_id, DAY_START, DAY_START,
             profile_version_id),
        )


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
            _insert_confirmed_anchor(database, SEASON_ID, "1781296800")
            # Day 25's interval has ended but day 23 is still the latest with
            # completed source evidence; a request starting at 28 lies beyond
            # the ended days and must name the affected days.
            with pytest.raises(ArmyAnalyticsUnavailable) as unavailable:
                api.get_army_analytics(
                    _selection(season="current", start_day=28, end_day=28),
                    now=DAY_START + timedelta(days=24, hours=12),
                )
            assert unavailable.value.affected_days == [28]
        finally:
            api.close()
            database.close()


def test_current_season_without_completed_days_names_previous_season(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as ci:
        ts = DAY_START + timedelta(hours=1)
        _, job = store_observation(
            ci, archive_server, occurrence_key="empty-current-a1", endpoint="battle_log",
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
            army_job = _army_job(database)
            assert processor.process_job(army_job, owner="analytics").outcome == "processed"
            # The historical season has completed days; the confirmed current
            # season has none yet, so season=current must not silently serve
            # the previous season's publications.
            _insert_confirmed_anchor(database, "1900000000", SEASON_ID)
            # No Legend-day interval of the confirmed current season has ended
            # yet at the season anchor itself.
            with pytest.raises(CurrentSeasonEmpty) as empty:
                api.get_army_analytics(
                    _selection(season="current", start_day=23), now=DAY_START
                )
            assert empty.value.previous_season_id == SEASON_ID
            # Without a confirmed anchor there is no honest current-season
            # identity and no previous season to link.
            with database.pool.connection() as connection:
                connection.execute("DELETE FROM legend_season_anchors")
            with pytest.raises(CurrentSeasonEmpty) as empty:
                api.get_army_analytics(_selection(season="current", start_day=23))
            assert empty.value.previous_season_id is None
            # The historical season stays reachable by explicit id.
            assert api.get_army_analytics(_selection(start_day=23)) is not None
        finally:
            api.close()
            database.close()


def test_current_season_ended_but_withheld_day_is_unavailable(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as ci:
        ts = DAY_START + timedelta(hours=1)
        _, job = store_observation(
            ci, archive_server, occurrence_key="withheld-a1", endpoint="battle_log",
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
            assert processor.process_job(_army_job(database), owner="analytics").outcome == "processed"
            _insert_confirmed_anchor(database, SEASON_ID, "1781296800")
            # Legend day 1's interval has ended one full day after the anchor,
            # but no completed source publication exists for it: the default
            # range must name it unavailable, not fall back to an empty state.
            with pytest.raises(ArmyAnalyticsUnavailable) as unavailable:
                api.get_army_analytics(
                    _selection(season="current", start_day=1, end_day=1),
                    now=DAY_START + timedelta(days=1, hours=1),
                )
            assert unavailable.value.affected_days == [1]
            # Later ended-but-withheld days stay named alongside missing
            # earlier days instead of being silently clipped away.
            with pytest.raises(ArmyAnalyticsUnavailable) as unavailable:
                api.get_army_analytics(
                    _selection(season="current", start_day=1, end_day=28),
                    now=DAY_START + timedelta(days=24, hours=12),
                )
            assert unavailable.value.affected_days == list(range(1, 23)) + [24]
            # The completed day itself stays reachable through the chronology.
            resolved = api.get_army_analytics(
                _selection(season="current", start_day=23, end_day=28),
                now=DAY_START + timedelta(days=23),
            )
            assert resolved is not None
            assert resolved["selection"]["end_day"] == 23
            assert resolved["total_attacks"] == 1
        finally:
            api.close()
            database.close()


def _seed_frozen_snapshot_at(
    database: Database,
    boundary: datetime,
    entries: list[tuple[int, int, str, str]],
) -> int:
    """entries: (player_id, position, freshness, confidence)."""
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
            (boundary,),
        ).fetchone()[0]
        for player_id, position, freshness, confidence in entries:
            connection.execute(
                """
                INSERT INTO leaderboard_snapshot_entries (
                    snapshot_id, position, player_id, trophies,
                    trophy_observation_id, trophy_observed_at,
                    observation_age_seconds, freshness, confidence, tie_hash
                ) VALUES (%s, %s, %s, 6000, %s, %s, 10, %s, %s, repeat('c', 64))
                """,
                (
                    snapshot_id,
                    position,
                    player_id,
                    observation_id,
                    boundary - timedelta(days=1),
                    freshness,
                    confidence,
                ),
            )
        return snapshot_id


def _mark_shielded(
    database: Database, player_tag: str, day_start: datetime, day_number: int
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
                evidence_complete, coverage_complete, shield_state,
                shield_duration_days
            ) VALUES (
                %s, %s, %s, %s, %s, 'legend-season-anchor-v1',
                'legend-ranked-day-v1', repeat('d', 64), 2,
                'Complete', 'exact', repeat('b', 64), true, true,
                'inferred_shielded', 1
            )
            """,
            (
                player_id,
                day_start,
                day_start + timedelta(days=1),
                SEASON_ID,
                day_number,
            ),
        )


def test_streak_evidence_reports_exclusions_and_shielded_member_days(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url) as ci:
        day2_start = DAY_START + timedelta(days=1)
        ts1 = DAY_START + timedelta(hours=1)
        ts2 = day2_start + timedelta(hours=1)
        _, j1 = store_observation(
            ci, archive_server, occurrence_key="streak-ev-a1", endpoint="battle_log",
            body=json.dumps({"items": [_row(True, "#8PP", FIXTURE_CODE, ts1, 3, 100)]}).encode(),
            observed_at=ts1 + timedelta(minutes=1), normalized_tag="#2PP",
        )
        _, j2 = store_observation(
            ci, archive_server, occurrence_key="streak-ev-a2", endpoint="battle_log",
            body=json.dumps({"items": [_row(True, "#9PP", FIXTURE_CODE, ts2, 2, 60)]}).encode(),
            observed_at=ts2 + timedelta(minutes=1), normalized_tag="#2PP",
        )
        database, processor = _processor(ci, archive_server)
        api = ApiDatabase(ci)
        try:
            for index, job in enumerate((j1, j2)):
                assert processor.process_job(job, owner=f"ingest-{index}").outcome in (
                    "processed",
                    "processed_with_gaps",
                )
            with database.pool.connection() as connection:
                battles = {
                    text(d): int(b)
                    for d, b in connection.execute(
                        """
                        SELECT def.normalized_tag, b.id
                        FROM legend_battles b
                        JOIN players atk ON atk.id = b.attacker_player_id
                        JOIN players def ON def.id = b.defender_player_id
                        WHERE atk.normalized_tag = '#2PP'
                        """
                    ).fetchall()
                }
                attacker_id = int(connection.execute(
                    "SELECT id FROM players WHERE normalized_tag = '#2PP'"
                ).fetchone()[0])
                defender8_id = int(connection.execute(
                    "SELECT id FROM players WHERE normalized_tag = '#8PP'"
                ).fetchone()[0])
                player9_id = int(connection.execute(
                    "SELECT id FROM players WHERE normalized_tag = '#9PP'"
                ).fetchone()[0])
            _publish_day(database, "#2PP", [_event(battles["#8PP"], "offense", ts1, 3, 100, 35)])
            assert processor.process_job(_army_job(database), owner="analytics").outcome == "processed"
            _publish_day(
                database,
                "#2PP",
                [_event(battles["#9PP"], "offense", ts2, 2, 60, 20)],
                day_start=day2_start,
                day_number=24,
            )
            assert processor.process_job(_army_job(database), owner="analytics-2").outcome == "processed"

            # Snapshot S1 ends day 23; S2 ends day 24. The defender stays in
            # the Top-N but turns stale in S2; #9PP drops out of S2 entirely.
            _seed_frozen_snapshot_at(
                database,
                DAY_START + timedelta(days=1),
                [
                    (attacker_id, 1, "fresh", "confirmed"),
                    (defender8_id, 2, "fresh", "confirmed"),
                    (player9_id, 3, "fresh", "confirmed"),
                ],
            )
            _seed_frozen_snapshot_at(
                database,
                DAY_START + timedelta(days=2),
                [
                    (attacker_id, 1, "fresh", "confirmed"),
                    (defender8_id, 2, "stale", "uncertain"),
                ],
            )
            # A corrected current version infers a shield for the confirmed
            # member on day 23 and for the excluded defender on day 24.
            _mark_shielded(database, "#2PP", DAY_START, 23)
            _mark_shielded(database, "#8PP", day2_start, 24)

            streak = api.get_army_analytics(
                _selection(population="streak-top-5", start_day=23, end_day=24)
            )
            assert streak is not None
            evidence = streak["cohort_evidence"]
            # Only the attacker keeps fresh confirmed Top-5 membership in both
            # snapshots; stale membership and missing membership both exclude.
            assert streak["total_attacks"] == 2
            assert evidence["streak_excluded_players"] == 2
            # The defender was present in every snapshot but not always fresh
            # and confirmed; that weakness stays separately visible.
            assert evidence["stale_or_uncertain_cohort_members"] == 1
            # Shielded-day evidence counts only confirmed members' member-days.
            assert evidence["shielded_player_days"] == 1

            non_streak = api.get_army_analytics(
                _selection(population="top-5", start_day=23, end_day=24)
            )
            assert non_streak is not None
            assert non_streak["cohort_evidence"]["streak_excluded_players"] == 0
            assert non_streak["cohort_evidence"]["shielded_player_days"] == 0
        finally:
            api.close()
            database.close()
