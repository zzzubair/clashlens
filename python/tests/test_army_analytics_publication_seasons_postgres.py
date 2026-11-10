from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from domain_test_support import (
    as_api_role,
    domain_database,
    store_observation,
    text,
)
from test_army_analytics_publication_postgres import (
    DAY_START,
    FIXTURE_CODE,
    SEASON_ID,
    _army_job,
    _event,
    _processor,
    _publish_day,
    _row,
    _selection,
)

from clashlens import api_analytics, army_rank_bands, boundary_publication
from clashlens.api_db import ApiDatabase
from clashlens.army_analytics import (
    ArmyAnalyticsUnavailable,
    CurrentSeasonEmpty,
)
from clashlens.db import Database


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
    database_url: str, archive_server, monkeypatch) -> None:
    with domain_database(database_url) as ci:
        ts = DAY_START + timedelta(hours=1)
        _, job = store_observation(
            ci, archive_server, occurrence_key="clip-a1", endpoint="battle_log",
            body=json.dumps({"items": [_row(True, "#8PP", FIXTURE_CODE, ts, 3, 100)]}).encode(),
            observed_at=ts + timedelta(minutes=1), normalized_tag="#2PP",
        )
        database, processor = _processor(ci, archive_server, monkeypatch)
        api = ApiDatabase(ci)
        api_reader = api_analytics.ArmyAnalyticsReader(api.pool)
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
                api_analytics.get_army_analytics(api_reader,
                    _selection(season="current", start_day=28, end_day=28),
                    now=DAY_START + timedelta(days=24, hours=12),
                )
            assert unavailable.value.affected_days == [28]
        finally:
            api.close()
            database.close()


def test_current_season_without_completed_days_names_previous_season(
    database_url: str, archive_server, monkeypatch) -> None:
    with domain_database(database_url) as ci:
        ts = DAY_START + timedelta(hours=1)
        _, job = store_observation(
            ci, archive_server, occurrence_key="empty-current-a1", endpoint="battle_log",
            body=json.dumps({"items": [_row(True, "#8PP", FIXTURE_CODE, ts, 3, 100)]}).encode(),
            observed_at=ts + timedelta(minutes=1), normalized_tag="#2PP",
        )
        database, processor = _processor(ci, archive_server, monkeypatch)
        api = ApiDatabase(ci)
        api_reader = api_analytics.ArmyAnalyticsReader(api.pool)
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
                api_analytics.get_army_analytics(api_reader,
                    _selection(season="current", start_day=23), now=DAY_START
                )
            assert empty.value.previous_season_id == SEASON_ID
            # Without a confirmed anchor there is no honest current-season
            # identity and no previous season to link.
            with database.pool.connection() as connection:
                connection.execute("DELETE FROM legend_season_anchors")
            with pytest.raises(CurrentSeasonEmpty) as empty:
                api_analytics.get_army_analytics(api_reader, _selection(season="current", start_day=23))
            assert empty.value.previous_season_id is None
            # The historical season stays reachable by explicit id.
            assert api_analytics.get_army_analytics(api_reader, _selection(start_day=23)) is not None
        finally:
            api.close()
            database.close()


def test_current_season_reads_latest_finished_days_it_has(
    database_url: str, archive_server, monkeypatch) -> None:
    with domain_database(database_url) as ci:
        ts = DAY_START + timedelta(hours=1)
        _, job = store_observation(
            ci, archive_server, occurrence_key="withheld-a1", endpoint="battle_log",
            body=json.dumps({"items": [_row(True, "#8PP", FIXTURE_CODE, ts, 3, 100)]}).encode(),
            observed_at=ts + timedelta(minutes=1), normalized_tag="#2PP",
        )
        database, processor = _processor(ci, archive_server, monkeypatch)
        api = ApiDatabase(ci)
        api_reader = api_analytics.ArmyAnalyticsReader(api.pool)
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
                api_analytics.get_army_analytics(api_reader,
                    _selection(season="current", start_day=1, end_day=1),
                    now=DAY_START + timedelta(days=1, hours=1),
                )
            assert unavailable.value.affected_days == [1]
            # The default range reads the finished days it has instead of
            # failing because earlier days were never finished.
            covered = api_analytics.get_army_analytics(api_reader,
                _selection(season="current", start_day=1, end_day=28),
                now=DAY_START + timedelta(days=24, hours=12),
            )
            assert (covered["selection"]["start_day"], covered["selection"]["end_day"]) == (23, 23)
            assert (covered["collection_coverage"]["covered_days"], covered["total_attacks"]) == ([23], 1)
            # The completed day itself stays reachable through the chronology.
            resolved = api_analytics.get_army_analytics(api_reader,
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
    database: Database,
    player_tag: str,
    day_start: datetime,
    day_number: int,
    *,
    version: int = 2,
    shield_state: str = "inferred_shielded",
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
                'legend-ranked-day-v1', repeat('d', 63) || right(%s::text, 1), %s,
                'Complete', 'exact', repeat('b', 64), true, true,
                %s, 1
            )
            """,
            (
                player_id,
                day_start,
                day_start + timedelta(days=1),
                SEASON_ID,
                day_number,
                str(version),
                version,
                shield_state,
            ),
        )


def test_streak_evidence_reports_exclusions_and_shielded_member_days(
    database_url: str, archive_server, monkeypatch) -> None:
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
        database, processor = _processor(ci, archive_server, monkeypatch)
        api = ApiDatabase(as_api_role(ci))
        api_reader = api_analytics.ArmyAnalyticsReader(api.pool, cache_capacity=2)
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
            # Corrected current versions infer a shield for each member.
            _mark_shielded(database, "#2PP", DAY_START, 23)
            _mark_shielded(database, "#8PP", day2_start, 24)

            streak = api_analytics.get_army_analytics(api_reader,
                _selection(population="streak-top-5", start_day=23, end_day=24)
            )
            assert streak is not None
            evidence = streak["cohort_evidence"]
            # Saved positions decide membership: the stale, uncertain defender
            # stays in; #9PP, missing from S2, is the only one excluded.
            assert streak["total_attacks"] == 2
            assert evidence["cohort_players"] == 2
            assert evidence["streak_excluded_players"] == 1
            # The defender's weak S2 entry stays visible as a count.
            assert evidence["stale_or_uncertain_cohort_members"] == 1
            assert evidence["shielded_player_days"] == 2
            cached_streak = api_analytics.get_army_analytics(api_reader,
                _selection(population="streak-top-5", start_day=23, end_day=24)
            )
            assert cached_streak == streak
            cached_streak["rows"].clear()
            assert api_analytics.get_army_analytics(api_reader,
                _selection(population="streak-top-5", start_day=23, end_day=24)
            )["rows"]
            with database.pool.connection() as connection:
                connection.execute(
                    """
                    UPDATE ranked_day_versions
                    SET shield_state = 'not_shielded'
                    WHERE player_id = %s AND ranked_day_start = %s
                      AND version = 2
                    """,
                    (attacker_id, DAY_START),
                )
            changed_in_place = api_analytics.get_army_analytics(api_reader,
                _selection(population="streak-top-5", start_day=23, end_day=24)
            )
            assert changed_in_place is not None
            assert (
                changed_in_place["publication_identity"]
                != streak["publication_identity"]
            )
            _mark_shielded(
                database,
                "#2PP",
                DAY_START,
                23,
                version=3,
                shield_state="not_shielded",
            )
            changed_streak = api_analytics.get_army_analytics(api_reader,
                _selection(population="streak-top-5", start_day=23, end_day=24)
            )
            assert changed_streak is not None
            assert changed_streak["publication_identity"] != streak["publication_identity"]

            for _day in (23, 24):
                army_rank_bands.refresh_rank_band_totals(database)
            non_streak = api_analytics.get_army_analytics(api_reader,
                _selection(population="top-5", start_day=23, end_day=24)
            )
            assert non_streak is not None
            assert non_streak["cohort_evidence"]["streak_excluded_players"] == 0
            assert non_streak["cohort_evidence"]["shielded_player_days"] == 0
        finally:
            api.close()
            database.close()


def test_missing_trophy_change_makes_subsequent_unknown_for_new_and_existing_rows(
    database_url: str, archive_server, monkeypatch) -> None:
    """Once an included battle lacks an integer trophy_change, subsequent
    same-day battle_time_trophies become unknown/None.

    Covers both the newly-inserted path (first publish with missing middle)
    and the existing-row path (hash-matched continue and versioned supersede)
    and proves trophy-range filtering excludes subsequent battles and reports
    missing_trophy_membership_evidence.
    """
    with domain_database(database_url) as ci:
        day_a = DAY_START
        day_b = DAY_START + timedelta(days=1)
        ts_a1 = day_a + timedelta(hours=1)
        ts_a2 = day_a + timedelta(hours=2)
        ts_a3 = day_a + timedelta(hours=3)
        ts_b1 = day_b + timedelta(hours=1)
        ts_b2 = day_b + timedelta(hours=2)
        ts_b3 = day_b + timedelta(hours=3)

        def event_without_change(battle_id: int, lens: str, ts: datetime) -> dict:
            return {
                "battle_id": str(battle_id),
                "lens": lens,
                "battle_timestamp": ts.isoformat().replace("+00:00", "Z"),
                "stars": 2,
                "destruction_percentage": 60,
                "opponent": {"tag": "#8PP", "name": "Opp"},
                "included": True,
            }

        # Six distinct battles for two days, all from #2PP attacker.
        rows = [
            ("#8P0", ts_a1), ("#8P2", ts_a2), ("#8P8", ts_a3),
            ("#8P9", ts_b1), ("#8PY", ts_b2), ("#8PL", ts_b3),
        ]
        jobs = []
        for key, (opp, ts) in enumerate(rows):
            _, job = store_observation(
                ci, archive_server, occurrence_key=f"missing-trophy-a{key}", endpoint="battle_log",
                body=json.dumps({"items": [_row(True, opp, FIXTURE_CODE, ts, 2, 60)]}).encode(),
                observed_at=ts + timedelta(minutes=1), normalized_tag="#2PP",
            )
            jobs.append(job)
        database, processor = _processor(ci, archive_server, monkeypatch)
        api = ApiDatabase(ci)
        api_reader = api_analytics.ArmyAnalyticsReader(api.pool)
        try:
            for idx, job in enumerate(jobs):
                out = processor.process_job(job, owner=f"ingest-{idx}")
                assert out.outcome in ("processed", "processed_with_gaps")
            with database.pool.connection() as conn:
                by_opp = {text(d): int(b) for d, b in conn.execute(
                    "SELECT def.normalized_tag, b.id FROM legend_battles b "
                    "JOIN players atk ON atk.id=b.attacker_player_id "
                    "JOIN players def ON def.id=b.defender_player_id "
                    "WHERE atk.normalized_tag='#2PP'"
                ).fetchall()}
            # ---- newly-inserted path: day A published with missing middle ----
            # Events sorted by timestamp, so a2 (missing) propagates unknown to a3.
            a_events = [
                _event(by_opp["#8P0"], "offense", ts_a1, 2, 60, 10),
                event_without_change(by_opp["#8P2"], "offense", ts_a2),
                _event(by_opp["#8P8"], "offense", ts_a3, 2, 60, 5),
            ]
            _publish_day(database, "#2PP", a_events, day_start=day_a, day_number=23)
            assert processor.process_job(_army_job(database), owner="analytics-a").outcome == "processed"
            with database.pool.connection() as conn:
                trophies_a = {int(bid): t for bid, t in conn.execute(
                    "SELECT battle_id, battle_time_trophies FROM army_analytics_battle_facts "
                    "WHERE ranked_day_start=%s AND is_current ORDER BY battle_id", (day_a,)
                ).fetchall()}
            # Current battle (a2) keeps its pre-battle value (6010); subsequent a3 is unknown.
            assert trophies_a[by_opp["#8P0"]] == 6000
            assert trophies_a[by_opp["#8P2"]] == 6010
            assert trophies_a[by_opp["#8P8"]] is None
            # Existing-row continue path: re-process same missing payload stays unknown.
            with database.pool.connection() as conn:
                job_a = int(conn.execute("SELECT id FROM python_processing_jobs WHERE work_type='build_army_analytics' ORDER BY id DESC LIMIT 1").fetchone()[0])
            database.requeue_completed_job(job_a)
            assert processor.process_job(job_a, owner="reprocess-a").outcome == "processed"
            with database.pool.connection() as conn:
                trophies_a2 = {int(bid): t for bid, t in conn.execute(
                    "SELECT battle_id, battle_time_trophies FROM army_analytics_battle_facts WHERE ranked_day_start=%s AND is_current ORDER BY battle_id", (day_a,)
                ).fetchall()}
            assert trophies_a2 == trophies_a
            trophy_a = api_analytics.get_army_analytics(api_reader, _selection(population="trophies-5000-9000", start_day=23, end_day=23))
            assert trophy_a is not None
            assert trophy_a["missing_trophy_membership_evidence"] == 1
            assert trophy_a["total_attacks"] == 2  # a3 excluded from trophy range
            # ---- existing-row supersede path: day B valid then corrected to missing ----
            b_events_valid = [
                _event(by_opp["#8P9"], "offense", ts_b1, 2, 60, 10),
                _event(by_opp["#8PY"], "offense", ts_b2, 2, 60, 20),
                _event(by_opp["#8PL"], "offense", ts_b3, 2, 60, 5),
            ]
            _publish_day(database, "#2PP", b_events_valid, day_start=day_b, day_number=24)
            assert processor.process_job(_army_job(database), owner="analytics-b").outcome == "processed"
            with database.pool.connection() as conn:
                trophies_b_valid = {int(bid): t for bid, t in conn.execute(
                    "SELECT battle_id, battle_time_trophies FROM army_analytics_battle_facts WHERE ranked_day_start=%s AND is_current ORDER BY battle_id", (day_b,)
                ).fetchall()}
            assert trophies_b_valid[by_opp["#8P9"]] == 6000
            assert trophies_b_valid[by_opp["#8PY"]] == 6010
            assert trophies_b_valid[by_opp["#8PL"]] == 6030
            trophy_b_valid = api_analytics.get_army_analytics(api_reader, _selection(population="trophies-5000-9000", start_day=24, end_day=24))
            assert trophy_b_valid is not None
            assert trophy_b_valid["missing_trophy_membership_evidence"] == 0
            assert trophy_b_valid["total_attacks"] == 3
            # Correction where middle battle lacks trophy_change: subsequent becomes unknown via supersede.
            b_events_corrected = [
                _event(by_opp["#8P9"], "offense", ts_b1, 2, 60, 10),
                event_without_change(by_opp["#8PY"], "offense", ts_b2),
                _event(by_opp["#8PL"], "offense", ts_b3, 2, 60, 5),
            ]
            with database.pool.connection() as conn:
                conn.execute(
                    "INSERT INTO ranked_day_versions (player_id, ranked_day_start, ranked_day_end, official_season_id, season_day_number, season_anchor_rule_version, reconciliation_rule_version, result_hash, version, state, confidence, input_hash, evidence_complete, coverage_complete, start_trophies) "
                    "VALUES ((SELECT id FROM players WHERE normalized_tag='#2PP'), %s, %s, %s, 24, 'legend-season-anchor-v1', 'legend-ranked-day-v1', repeat('f',64), 2, 'Complete', 'exact', repeat('e',64), true, true, 6000)",
                    (day_b, day_b + timedelta(days=1), SEASON_ID)
                )
                conn.execute(
                    "INSERT INTO api_player_daily_logs (player_id, ranked_day_start, version, state, coverage, battles, ranked_day_end, official_season_id, season_day_number) "
                    "VALUES ((SELECT id FROM players WHERE normalized_tag='#2PP'), %s, 2, 'Complete', 'complete', %s, %s, %s, 24)",
                    (day_b, json.dumps(b_events_corrected), day_b + timedelta(days=1), SEASON_ID)
                )
                boundary_publication._enqueue_army_analytics(database, conn, ranked_day_start=day_b)
            corr_job = _army_job(database)
            assert processor.process_job(corr_job, owner="correction-b").outcome == "processed"
            with database.pool.connection() as conn:
                trophies_b_corr = {int(bid): t for bid, t in conn.execute(
                    "SELECT battle_id, battle_time_trophies FROM army_analytics_battle_facts WHERE ranked_day_start=%s AND is_current ORDER BY battle_id", (day_b,)
                ).fetchall()}
            assert trophies_b_corr[by_opp["#8P9"]] == 6000
            assert trophies_b_corr[by_opp["#8PY"]] == 6010
            assert trophies_b_corr[by_opp["#8PL"]] is None
            trophy_b_corr = api_analytics.get_army_analytics(api_reader, _selection(population="trophies-5000-9000", start_day=24, end_day=24))
            assert trophy_b_corr is not None
            assert trophy_b_corr["missing_trophy_membership_evidence"] == 1
            assert trophy_b_corr["total_attacks"] == 2
        finally:
            api.close()
            database.close()
