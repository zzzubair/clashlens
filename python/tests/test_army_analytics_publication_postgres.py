from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import Event
from time import monotonic
from unittest.mock import Mock

import pytest
from domain_test_support import (
    domain_database,
    enable_direct_army_fixture,
    store_observation,
    text,
)
from psycopg_pool import PoolTimeout

from clashlens import api_analytics, army_rank_bands, boundary_publication
from clashlens.api_analytics import (
    ARMY_ANALYTICS_ADMISSION_TIMEOUT_SECONDS,
    ARMY_ANALYTICS_PARALLEL_POOL_TIMEOUT_SECONDS,
    ARMY_ANALYTICS_QUERY_WORK_MEM,
)
from clashlens.api_db import ApiDatabase
from clashlens.archive import S3ArchiveReader
from clashlens.army_analytics import (
    ArmyAnalyticsSelection,
    ArmyAnalyticsUnavailable,
    build_army_result,
)
from clashlens.db import Database
from clashlens.worker import ObservationProcessor

DAY_START = datetime(2026, 8, 4, 5, tzinfo=UTC)
SEASON_ID = "1783918800"
DAY_NUMBER = 23
FIXTURE_CODE = "h0p9e14_32d1x53u2x58-1x97s2x2"
PARTIAL_CODE = "u2x58-3x9999"
DEFENDER_CODE = "u1x51"


def _processor(connection_info: str, archive_server, monkeypatch):
    database = Database(connection_info)
    enable_direct_army_fixture(database, monkeypatch)
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
        "battleTime": ts.strftime("%Y%m%dT%H%M%S.000Z"),
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
        boundary_publication._enqueue_army_analytics(database, connection, ranked_day_start=day_start)


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
    database: Database,
    entries: list[tuple[int, int]],
    *,
    stale_first: bool = False,
    version: int = 1,
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
            ) VALUES ('frozen', %s, %s, 'ordering-v1', 'freshness-v1', 'published', 1.0, 0)
            RETURNING id
            """,
            (DAY_START + timedelta(days=1), version),
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


def test_army_cache_is_bounded_thread_safe_and_returns_copies() -> None:
    api = api_analytics.ArmyAnalyticsReader(Mock(timeout=5, max_size=8), cache_capacity=2)
    first = {"publication_identity": "one", "rows": [{"key": "troop:1"}]}
    api.cache_put(("selection-one",), first)
    hit = api.cache_get(("selection-one",))
    assert hit == first
    assert hit is not first
    hit["rows"].clear()
    assert api.cache_get(("selection-one",))["rows"]
    assert api.cache_get(("selection-two",)) is None
    api.cache_put(("selection-two",), {"publication_identity": "two"})
    api.cache_put(("selection-three",), {"publication_identity": "three"})
    assert api.cache_get(("selection-one",)) is None


def test_troop_source_hash_pool_timeout_fails_before_expensive_queries(
    monkeypatch,
) -> None:
    def fake_connection():
        connection = Mock()
        query_kinds: list[str] = []

        def execute(query, params=()):
            del params
            sql = str(query)
            cursor = Mock()
            if "GROUP BY army_state" in sql:
                query_kinds.append("state")
                cursor.fetchall.return_value = [("decoded", 1, 0, 0, False)]
            elif sql.startswith("VALUES (pg_export_snapshot())"):
                query_kinds.append("snapshot")
                cursor.fetchone.return_value = ("test-snapshot",)
            elif "CROSS JOIN LATERAL" in sql:
                query_kinds.append("component")
                cursor.fetchall.return_value = [
                    ("troop:1", 1, 0, 0, 0, 1, 3, 100)
                ]
            else:
                raise AssertionError(f"unexpected query: {sql}")
            return cursor

        connection.execute.side_effect = execute
        return connection, query_kinds

    selection = _selection(start_day=1, end_day=1)
    hash_connections = []

    def source_hash(connection, **_kwargs):
        hash_connections.append(connection)
        return "a" * 64

    monkeypatch.setattr(api_analytics, "_selected_source_hash", source_hash)
    arguments = {
        "fact_filters": ["true"],
        "fact_params": [],
        "requested": selection.as_dict(),
        "snapshot_ids": [],
        "selection": selection,
    }
    sequential_connection, _ = fake_connection()
    expected = api_analytics._query_troops_aggregates(
        sequential_connection, **arguments
    )

    timed_out_connection, timed_out_queries = fake_connection()
    pool = Mock(max_size=2)
    pool.connection.side_effect = PoolTimeout("second connection unavailable")
    with pytest.raises(PoolTimeout):
        api_analytics._query_troops_aggregates(
            timed_out_connection, pool=pool, **arguments
        )

    assert expected[0]["rows"][0]["usage_count"] == 1
    pool.connection.assert_called_once_with(
        timeout=ARMY_ANALYTICS_PARALLEL_POOL_TIMEOUT_SECONDS
    )
    assert timed_out_queries == ["state", "snapshot"]
    assert hash_connections == [sequential_connection]


def test_publication_writer_serves_reproducible_perspective_results(
    database_url: str, archive_server, monkeypatch) -> None:
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
        database, processor = _processor(ci, archive_server, monkeypatch)
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
            assert facts[(attacker_vs_other, "offense")][2] == "decoded"
            assert facts[(attacker_vs_defender, "defense")][2] == "decoded"
            assert facts[(attacker_vs_defender, "offense")][3] == 6000
            assert facts[(attacker_vs_other, "offense")][3] == 6035

            api = ApiDatabase(ci)
            api_reader = api_analytics.ArmyAnalyticsReader(api.pool, cache_capacity=0)
            fact_queries: list[str] = []
            original_connection = api.pool.connection

            @contextmanager
            def traced_connection(*args, **kwargs):
                with original_connection(*args, **kwargs) as connection:
                    class TracedConnection:
                        def execute(self, query, *args, **kwargs):
                            sql = str(query)
                            if "FROM army_analytics_battle_facts" in sql:
                                fact_queries.append(sql)
                            return connection.execute(query, *args, **kwargs)

                        def __getattr__(self, name):
                            return getattr(connection, name)

                    yield TracedConnection()

            api.pool.connection = traced_connection
            try:
                # Offense lens: the unnamed troop 9999 counts like any other.
                selection = _selection()
                first = api_analytics.get_army_analytics(api_reader, selection)
                assert first is not None
                missing_trophy_queries = [
                    query
                    for query in fact_queries
                    if "battle_time_trophies IS NULL" in query
                ]
                assert len(missing_trophy_queries) == 1
                missing_trophy_query = " ".join(missing_trophy_queries[0].split())
                assert (
                    "SELECT count(*) FROM army_analytics_battle_facts WHERE "
                    in missing_trophy_query
                )
                assert "AND is_current AND battle_time_trophies IS NULL" in (
                    missing_trophy_query
                )
                assert "FILTER" not in missing_trophy_query
                assert first["total_attacks"] == 2
                assert first["usable_army_sample"] == 2
                assert first["army_states"]["fully_decoded"] == 2
                assert "partial" not in first["army_states"]
                assert first["army_states_sum_confirmed"] is True
                assert first["unknown_affected_attacks"] == 0
                assert first["unknown_component_occurrences"] == 0
                rows = {row["key"]: row for row in first["rows"]}
                troop_row, unnamed = rows["troop:58"], rows["troop:9999"]
                assert (unnamed["label"], unnamed["usage_count"]) == (
                    "Unknown troop or siege #9999", 1)
                assert (troop_row["usage_count"], troop_row["usage_denominator"]) == (2, 2)
                identity = first["publication_identity"]
                assert identity.startswith("army-publication-")
                assert first["reproducibility"]["official_season_id"] == SEASON_ID
                # Golden-check the SQL aggregate against the retained reducer.
                with database.pool.connection() as connection:
                    selected_rows = connection.execute(
                        """
                        SELECT id, battle_id, population_player_id,
                               battle_time_trophies, stars,
                               destruction_percentage, army_state,
                               failure_reason, home_troops,
                               unresolved_components,
                               perspective_disagreement, input_hash,
                               source_ranked_day_version_id
                        FROM army_analytics_battle_facts_with_armies
                        WHERE official_season_id = %s
                          AND season_day_number = %s AND lens = %s
                          AND is_current
                          AND battle_time_trophies BETWEEN %s AND %s
                        ORDER BY battle_id
                        """,
                        (SEASON_ID, DAY_NUMBER, "offense", 5000, 9000),
                    ).fetchall()
                expected_facts = [
                    {
                        "id": int(row[0]),
                        "battle_id": int(row[1]),
                        "population_player_id": int(row[2]),
                        "battle_time_trophies": None if row[3] is None else int(row[3]),
                        "stars": int(row[4]),
                        "destruction_percentage": int(row[5]),
                        "army_state": text(row[6]),
                        "failure_reason": None if row[7] is None else text(row[7]),
                        "home_troops": row[8],
                        "spells": [],
                        "siege": [],
                        "cc_troops": [],
                        "heroes": [],
                        "unresolved_components": row[9],
                        "perspective_disagreement": bool(row[10]),
                        "input_hash": text(row[11]),
                        "source_ranked_day_version_id": int(row[12]),
                    }
                    for row in selected_rows
                ]
                expected = build_army_result(expected_facts, selection)
                expected["collection_coverage"].update(covered_days=[DAY_NUMBER], streak_gap_days=[DAY_NUMBER])
                expected["cohort_evidence"] = {
                    "cohort_players": 0,
                    "stale_or_uncertain_cohort_members": 0,
                    "streak_excluded_players": 0,
                    "shielded_player_days": 0,
                }
                expected["reproducibility"] = {
                    "official_season_id": SEASON_ID,
                    "legend_days": [DAY_NUMBER, DAY_NUMBER],
                    "snapshot_versions": [],
                }
                expected_source_hash = hashlib.sha256(
                    json.dumps(
                        {
                            "selection": selection.as_dict(),
                            "facts": [
                                (fact["id"], fact["input_hash"])
                                for fact in expected_facts
                            ],
                            "snapshots": [],
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                expected_result_hash = hashlib.sha256(
                    json.dumps(
                        {"result": expected, "source_evidence": expected_source_hash},
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                expected["reproducibility"]["source_evidence_hash"] = expected_source_hash
                expected["selection"] = selection.as_dict()
                selection_hash = hashlib.sha256(
                    json.dumps(
                        selection.as_dict(), sort_keys=True, separators=(",", ":")
                    ).encode()
                ).hexdigest()
                expected["publication_identity"] = (
                    f"army-publication-{selection_hash[:24]}-{expected_result_hash[:16]}"
                )
                expected["season_bounds"] = {
                    "id": SEASON_ID, "start": "2026-07-13T05:00:00+00:00",
                    "end": "2026-08-10T05:00:00+00:00", "day_count": 28,
                    "last_day_start": "2026-08-09T05:00:00+00:00"}
                assert first == expected
                category_columns = {
                    "troops": "home_troops",
                    "spells": "spells",
                    "siege": "siege",
                    "cc-troops": "cc_troops",
                    "cc-composition": "cc_troops",
                    "heroes": "heroes",
                    "pets": "heroes",
                    "equipment": "heroes",
                    "equipment-for-hero": "heroes",
                    "hero-pet": "heroes",
                    "hero-equipment": "heroes",
                }
                for category, component_column in category_columns.items():
                    fact_queries.clear()
                    category_result = api_analytics.get_army_analytics(api_reader,
                        _selection(category=category)
                    )
                    assert category_result is not None
                    if category == "troops":
                        # SQL returns one aggregate row, never selected fact
                        # payload rows or unrelated component JSON.
                        aggregate_queries = [
                            query
                            for query in fact_queries
                            if "jsonb_array_elements" in query
                        ]
                        assert len(aggregate_queries) == 1
                        aggregate_query = aggregate_queries[0]
                        assert "SELECT component.key, count(*)" in aggregate_query
                        for other_column in (
                            "spells", "siege", "cc_troops", "heroes"
                        ):
                            assert other_column not in aggregate_query
                        continue
                    assert len(
                        [
                            query
                            for query in fact_queries
                            if "battle_time_trophies IS NULL" in query
                        ]
                    ) == 1
                    payload_queries = [
                        query
                        for query in fact_queries
                        if "SELECT id, battle_id" in query
                    ]
                    assert len(payload_queries) == 1
                    payload_query = payload_queries[0]
                    assert "battle_time_trophies BETWEEN %s AND %s" in payload_query
                    assert f"{component_column} AS component_payload" in payload_query
                    for other_column in {
                        "home_troops", "spells", "siege", "cc_troops", "heroes"
                    } - {component_column}:
                        assert f"{other_column} AS component_payload" not in payload_query

                # Malformed component entries are skipped exactly like the
                # Python reducer while the aggregate still returns one row.
                with database.pool.connection() as connection:
                    original_troops = connection.execute(
                        """
                        SELECT home_troops
                        FROM army_analytics_battle_facts
                        WHERE battle_id = %s AND lens = 'offense' AND is_current
                        """,
                        (attacker_vs_defender,),
                    ).fetchone()[0]
                    connection.execute(
                        """
                        UPDATE army_analytics_battle_facts
                        SET home_troops = %s::jsonb
                        WHERE battle_id = %s AND lens = 'offense' AND is_current
                        """,
                        (
                            json.dumps(
                                [["badtyped", 1], ["badtyped", 2], {}, [], "bad"]
                            ),
                            attacker_vs_defender,
                        ),
                    )
                malformed = api_analytics.get_army_analytics(api_reader, selection)
                assert malformed is not None
                malformed_row = next(
                    row for row in malformed["rows"] if row["key"] == "badtyped"
                )
                assert malformed_row["usage_count"] == 1
                assert malformed_row["label"] == "Unknown badtyped"
                with database.pool.connection() as connection:
                    connection.execute(
                        """
                        UPDATE army_analytics_battle_facts
                        SET home_troops = %s::jsonb
                        WHERE battle_id = %s AND lens = 'offense' AND is_current
                        """,
                        (None if original_troops is None else json.dumps(original_troops), attacker_vs_defender),
                    )

                # Reads calculate from retained facts without persistent
                # per-selection storage. Identical inputs keep one identity;
                # arbitrary URL-backed trophy ranges get distinct identities.
                second = api_analytics.get_army_analytics(api_reader, selection)
                assert second is not None
                assert second["publication_identity"] == identity
                other_ranges = [
                    api_analytics.get_army_analytics(api_reader,
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
                fact_queries.clear()
                defense = api_analytics.get_army_analytics(api_reader,
                    _selection(lens="defense", category="siege")
                )
                assert defense is not None
                assert defense["total_attacks"] == 1
                siege_row = next(
                    row for row in defense["rows"] if row["key"] == "troop:51"
                )
                assert siege_row["usage_count"] == 1
                assert siege_row["usage_denominator"] == 1
                payload_queries = [
                    query for query in fact_queries if "SELECT id, battle_id" in query
                ]
                assert len(payload_queries) == 1
                assert "siege AS component_payload" in payload_queries[0]
                assert "home_troops" not in payload_queries[0]
                assert "spells" not in payload_queries[0]
                assert "cc_troops" not in payload_queries[0]
                assert "heroes" not in payload_queries[0]

                # Frozen Top-N membership from the final-day snapshot.
                with database.pool.connection() as connection:
                    defender_id = connection.execute(
                        "SELECT id FROM players WHERE normalized_tag = '#8PP'"
                    ).fetchone()[0]
                    attacker_id = connection.execute(
                        "SELECT id FROM players WHERE normalized_tag = '#2PP'"
                    ).fetchone()[0]
                snapshot_id = _seed_frozen_snapshot(
                    database, [(defender_id,), (attacker_id,)]
                )
                army_rank_bands.refresh_rank_band_totals(database)
                top5 = api_analytics.get_army_analytics(api_reader, _selection(lens="defense", population="top-5"))
                assert top5 is not None
                assert top5["total_attacks"] == 1
                assert top5["reproducibility"]["snapshot_versions"] == [1]
                # The attacker sits at position 6 of the frozen snapshot, so a
                # Top-5 offense cohort excludes both of their attacks.
                fact_queries.clear()
                top5_offense = api_analytics.get_army_analytics(api_reader,
                    _selection(population="top-5")
                )
                assert top5_offense is not None
                assert top5_offense["total_attacks"] == 0
                assert top5_offense["rows"] == []
                assert fact_queries == []
                # Top 5 is one rank band (from rank 1) with no facts that day.
                band_digest = json.dumps([[DAY_NUMBER, 1, None]], separators=(",", ":"))
                expected_top_source_hash = hashlib.sha256(
                    json.dumps(
                        {
                            "selection": _selection(population="top-5").as_dict(),
                            "rank_band_facts": hashlib.sha256(band_digest.encode()).hexdigest(),
                            "snapshots": [snapshot_id],
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode()
                ).hexdigest()
                assert (
                    top5_offense["reproducibility"]["source_evidence_hash"]
                    == expected_top_source_hash
                )

                # A withheld day inside the range names the affected day.
                with pytest.raises(ArmyAnalyticsUnavailable) as unavailable:
                    api_analytics.get_army_analytics(api_reader, _selection(start_day=22))
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
                third = api_analytics.get_army_analytics(api_reader, selection)
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


def test_unselected_fact_correction_does_not_change_selected_result(
    database_url: str, archive_server, monkeypatch) -> None:
    with domain_database(database_url) as ci:
        ts = DAY_START + timedelta(hours=1)
        observations = []
        for index, attacker in enumerate(("#2PP", "#9PP")):
            observations.append(
                store_observation(
                    ci,
                    archive_server,
                    occurrence_key=f"unselected-correction-a{index}",
                    endpoint="battle_log",
                    body=json.dumps(
                        {"items": [_row(True, "#8PP", FIXTURE_CODE, ts, 3, 100)]}
                    ).encode(),
                    observed_at=ts + timedelta(minutes=index + 1),
                    normalized_tag=attacker,
                )[1]
            )
        database, processor = _processor(ci, archive_server, monkeypatch)
        api = ApiDatabase(ci)
        api_reader = api_analytics.ArmyAnalyticsReader(api.pool)
        try:
            for index, job in enumerate(observations):
                assert processor.process_job(job, owner=f"ingest-{index}").outcome == "processed"
            with database.pool.connection() as connection:
                battles = {
                    text(attacker): int(battle_id)
                    for attacker, battle_id in connection.execute(
                        """
                        SELECT attacker.normalized_tag, battle.id
                        FROM legend_battles AS battle
                        JOIN players AS attacker
                          ON attacker.id = battle.attacker_player_id
                        """
                    ).fetchall()
                }
                player_ids = {
                    text(tag): int(player_id)
                    for tag, player_id in connection.execute(
                        """
                        SELECT normalized_tag, id FROM players
                        WHERE normalized_tag IN ('#2PP', '#9PP')
                        """
                    ).fetchall()
                }
            for attacker in ("#2PP", "#9PP"):
                _publish_day(
                    database,
                    attacker,
                    [_event(battles[attacker], "offense", ts, 3, 100, 35)],
                )
                job = _army_job(database)
                assert processor.process_job(job, owner=f"analytics-{attacker}").outcome == "processed"
            _seed_frozen_snapshot(
                database, [(player_ids["#2PP"],), (player_ids["#9PP"],)]
            )
            selection = _selection(population="top-5")
            army_rank_bands.refresh_rank_band_totals(database)
            before = api_analytics.get_army_analytics(api_reader, selection)
            assert before is not None and before["total_attacks"] == 1
            with database.pool.connection() as connection:
                before_unselected_hash = connection.execute(
                    """
                    SELECT input_hash
                    FROM army_analytics_battle_facts
                    WHERE battle_id = %s AND lens = 'offense' AND is_current
                    """,
                    (battles["#9PP"],),
                ).fetchone()[0]

            _publish_day_correction(
                database,
                "#9PP",
                [_event(battles["#9PP"], "offense", ts, 2, 75, 12)],
            )
            correction = processor.process_job(
                _army_job(database), owner="unselected-correction"
            )
            assert correction is not None and correction.outcome == "processed"
            army_rank_bands.refresh_rank_band_totals(database)
            after = api_analytics.get_army_analytics(api_reader, selection)
            assert after is not None
            assert after["rows"] == before["rows"]
            assert after["reproducibility"]["source_evidence_hash"] == before[
                "reproducibility"
            ]["source_evidence_hash"]
            assert after["publication_identity"] == before["publication_identity"]
            with database.pool.connection() as connection:
                after_unselected_hash = connection.execute(
                    """
                    SELECT input_hash
                    FROM army_analytics_battle_facts
                    WHERE battle_id = %s AND lens = 'offense' AND is_current
                    """,
                    (battles["#9PP"],),
                ).fetchone()[0]
            assert after_unselected_hash != before_unselected_hash
        finally:
            api.close()
            database.close()


def _publish_day_correction(
    database: Database,
    player_tag: str,
    events: list[dict],
    *,
    version: int = 2,
) -> None:
    result_hash = hashlib.sha256(f"result:{version}".encode()).hexdigest()
    input_hash = hashlib.sha256(f"input:{version}".encode()).hexdigest()
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
                'legend-ranked-day-v1', %s, %s,
                'Complete', 'exact', %s, true, true, 6000
            )
            """,
            (
                player_id,
                DAY_START,
                DAY_START + timedelta(days=1),
                SEASON_ID,
                DAY_NUMBER,
                result_hash,
                version,
                input_hash,
            ),
        )
        connection.execute(
            """
            INSERT INTO api_player_daily_logs (
                player_id, ranked_day_start, version, state, coverage,
                battles, ranked_day_end, official_season_id, season_day_number
            ) VALUES (%s, %s, %s, 'Complete', 'complete', %s, %s, %s, %s)
            """,
            (
                player_id,
                DAY_START,
                version,
                json.dumps(events),
                DAY_START + timedelta(days=1),
                SEASON_ID,
                DAY_NUMBER,
            ),
        )
        boundary_publication._enqueue_army_analytics(database, connection, ranked_day_start=DAY_START)


def test_remove_then_reintroduce_preserves_fact_lineage_and_final_marker(
    database_url: str, archive_server, monkeypatch) -> None:
    with domain_database(database_url) as ci:
        timestamp = DAY_START + timedelta(hours=1)
        _, battle_job = store_observation(
            ci,
            archive_server,
            occurrence_key="remove-reintroduce-fact",
            endpoint="battle_log",
            body=json.dumps(
                {"items": [_row(True, "#8PP", FIXTURE_CODE, timestamp, 3, 100)]}
            ).encode(),
            observed_at=timestamp + timedelta(minutes=1),
            normalized_tag="#2PP",
        )
        database, processor = _processor(ci, archive_server, monkeypatch)
        try:
            assert (
                processor.process_job(battle_job, owner="ingest").outcome
                == "processed"
            )
            with database.pool.connection() as connection:
                battle_id = int(
                    connection.execute(
                        "SELECT max(id) FROM legend_battles"
                    ).fetchone()[0]
                )
            event = _event(battle_id, "offense", timestamp, 3, 100, 35)
            _publish_day(database, "#2PP", [event])
            assert (
                processor.process_job(_army_job(database), owner="initial").outcome
                == "processed"
            )

            _publish_day_correction(database, "#2PP", [], version=2)
            assert (
                processor.process_job(_army_job(database), owner="remove").outcome
                == "processed"
            )
            with database.pool.connection() as connection:
                removed_facts = connection.execute(
                    "SELECT count(*) FROM army_analytics_battle_facts WHERE is_current"
                ).fetchone()[0]
                removed_marker = connection.execute(
                    """
                    SELECT fact_input_hash
                    FROM army_analytics_completed_days
                    WHERE ranked_day_start = %s
                    """,
                    (DAY_START,),
                ).fetchone()[0]
            assert removed_facts == 0
            assert removed_marker == hashlib.sha256(b"[]").hexdigest()

            _publish_day_correction(database, "#2PP", [event], version=3)
            assert (
                processor.process_job(
                    _army_job(database), owner="reintroduce"
                ).outcome
                == "processed"
            )
            with database.pool.connection() as connection:
                history = connection.execute(
                    """
                    SELECT version, is_current
                    FROM army_analytics_battle_facts
                    WHERE battle_id = %s AND lens = 'offense'
                    ORDER BY version
                    """,
                    (battle_id,),
                ).fetchall()
                current = connection.execute(
                    """
                    SELECT version, input_hash
                    FROM army_analytics_battle_facts
                    WHERE battle_id = %s AND lens = 'offense' AND is_current
                    """,
                    (battle_id,),
                ).fetchone()
                final_marker = connection.execute(
                    """
                    SELECT fact_input_hash
                    FROM army_analytics_completed_days
                    WHERE ranked_day_start = %s
                    """,
                    (DAY_START,),
                ).fetchone()[0]
            assert [(int(row[0]), bool(row[1])) for row in history] == [
                (1, False),
                (2, True),
            ]
            assert current is not None and int(current[0]) == 2
            assert final_marker == hashlib.sha256(
                json.dumps(
                    [[battle_id, "offense", current[1]]], separators=(",", ":")
                ).encode()
            ).hexdigest()
        finally:
            database.close()


def test_completed_day_without_army_job_is_unavailable_not_false_empty(
    database_url: str, archive_server, monkeypatch) -> None:
    with domain_database(database_url) as ci:
        ts = DAY_START + timedelta(hours=1)
        _, job = store_observation(
            ci, archive_server, occurrence_key="marker-a1", endpoint="battle_log",
            body=json.dumps({"items": [_row(True, "#8PP", FIXTURE_CODE, ts, 3, 100)]}).encode(),
            observed_at=ts + timedelta(minutes=1), normalized_tag="#2PP",
        )
        database, processor = _processor(ci, archive_server, monkeypatch)
        api = ApiDatabase(ci)
        api_reader = api_analytics.ArmyAnalyticsReader(api.pool)
        try:
            assert processor.process_job(job, owner="ingest").outcome == "processed"
            with database.pool.connection() as connection:
                battle_id = int(connection.execute("SELECT max(id) FROM legend_battles").fetchone()[0])
            _publish_day(database, "#2PP", [_event(battle_id, "offense", ts, 3, 100, 35)])
            # The ranked-day publication is complete but build_army_analytics has
            # not run: the selection must be unavailable, never a false empty.
            with pytest.raises(ArmyAnalyticsUnavailable) as unavailable:
                api_analytics.get_army_analytics(api_reader, _selection())
            assert unavailable.value.affected_days == [DAY_NUMBER]
        finally:
            api.close()
            database.close()


def test_missing_start_trophies_keep_facts_and_report_missing_evidence(
    database_url: str, archive_server, monkeypatch) -> None:
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
        database, processor = _processor(ci, archive_server, monkeypatch)
        api = ApiDatabase(ci)
        api_reader = api_analytics.ArmyAnalyticsReader(api.pool, cache_capacity=2)
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
            trophy_filtered = api_analytics.get_army_analytics(api_reader,
                _selection(population="trophies-5000-9000")
            )
            assert trophy_filtered is not None
            assert trophy_filtered["total_attacks"] == 0
            assert trophy_filtered["missing_trophy_membership_evidence"] == 2

            # Frozen cohort filters still include the attacks.
            _seed_frozen_snapshot(database, [(attacker_id,), (defender_id,)])
            army_rank_bands.refresh_rank_band_totals(database)
            top5 = api_analytics.get_army_analytics(api_reader, _selection(population="top-5"))
            assert top5 is not None
            assert top5["total_attacks"] == 2
            assert top5["missing_trophy_membership_evidence"] == 0
            cached_top5 = api_analytics.get_army_analytics(api_reader, _selection(population="top-5"))
            assert cached_top5 == top5
        finally:
            api.close()
            database.close()


def test_stale_snapshot_membership_is_reported_as_cohort_evidence(
    database_url: str, archive_server, monkeypatch) -> None:
    with domain_database(database_url) as ci:
        ts = DAY_START + timedelta(hours=1)
        _, job = store_observation(
            ci, archive_server, occurrence_key="stale-cohort-a1", endpoint="battle_log",
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
            army_rank_bands.refresh_rank_band_totals(database)
            top2 = api_analytics.get_army_analytics(api_reader, _selection(population="top-5"))
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
    database_url: str, archive_server, monkeypatch) -> None:
    with domain_database(database_url) as ci:
        ts = DAY_START + timedelta(hours=1)
        _, job = store_observation(
            ci, archive_server, occurrence_key="same-aggregates-a1", endpoint="battle_log",
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
            events = [_event(battle_id, "offense", ts, 3, 100, 35)]
            _publish_day(database, "#2PP", events)
            processor.process_job(_army_job(database), owner="analytics")
            selection = _selection()
            first = api_analytics.get_army_analytics(api_reader, selection)
            assert first is not None
            cached = api_analytics.get_army_analytics(api_reader, selection)
            assert cached == first
            cold_api = ApiDatabase(ci, max_size=2)
            cold_api_reader = api_analytics.ArmyAnalyticsReader(cold_api.pool)
            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    concurrent_results = list(
                        executor.map(
                            lambda item: api_analytics.get_army_analytics(cold_api_reader, item),
                            [selection] * 2,
                        )
                    )
                assert concurrent_results == [first, first]
            finally:
                cold_api.close()

            busy_api = ApiDatabase(ci, max_size=2)
            busy_api_reader = api_analytics.ArmyAnalyticsReader(busy_api.pool, cache_capacity=0)
            assert busy_api_reader.troop_slots.acquire(blocking=False)
            try:
                busy_started = monotonic()
                with pytest.raises(PoolTimeout, match="troop analytics"):
                    api_analytics.get_army_analytics(busy_api_reader, selection)
                assert monotonic() - busy_started < (
                    ARMY_ANALYTICS_ADMISSION_TIMEOUT_SECONDS + 0.5
                )
            finally:
                busy_api_reader.troop_slots.release()
                busy_api.close()

            pool_busy_api = ApiDatabase(ci, max_size=2)
            pool_busy_api_reader = api_analytics.ArmyAnalyticsReader(pool_busy_api.pool, cache_capacity=0)
            try:
                # Materialize both pool connections so this measures paired
                # checkout contention, not connection creation.
                with pool_busy_api.pool.connection() as first_connection:
                    with pool_busy_api.pool.connection() as second_connection:
                        assert first_connection is not second_connection
                with pool_busy_api.pool.connection():
                    pair_started = monotonic()
                    with pytest.raises(PoolTimeout):
                        api_analytics.get_army_analytics(pool_busy_api_reader, selection)
                    pair_elapsed = monotonic() - pair_started
                    assert pair_elapsed >= (
                        ARMY_ANALYTICS_PARALLEL_POOL_TIMEOUT_SECONDS * 0.5
                    )
                    assert pair_elapsed < 1
                assert api_analytics.get_army_analytics(pool_busy_api_reader, selection) == first
            finally:
                pool_busy_api.close()

            # A corrected source with identical aggregates still changes the
            # identity because retained source evidence changed.
            _publish_day_correction(database, "#2PP", events)
            correction = processor.process_job(_army_job(database), owner="correction")
            assert correction is not None and correction.outcome == "processed"
            second = api_analytics.get_army_analytics(api_reader, selection)
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


def test_concurrent_correction_never_mixes_army_publication_revisions(
    database_url: str, archive_server, monkeypatch) -> None:
    with domain_database(database_url) as ci:
        ts = DAY_START + timedelta(hours=1)
        _, job = store_observation(
            ci,
            archive_server,
            occurrence_key="consistent-publication-a1",
            endpoint="battle_log",
            body=json.dumps(
                {"items": [_row(True, "#8PP", FIXTURE_CODE, ts, 3, 100)]}
            ).encode(),
            observed_at=ts + timedelta(minutes=1),
            normalized_tag="#2PP",
        )
        database, processor = _processor(ci, archive_server, monkeypatch)
        stable_api = ApiDatabase(ci)
        stable_api_reader = api_analytics.ArmyAnalyticsReader(stable_api.pool, cache_capacity=0)
        racing_api = ApiDatabase(ci, max_size=2)
        racing_api_reader = api_analytics.ArmyAnalyticsReader(racing_api.pool, cache_capacity=0)
        try:
            processor.process_job(job, owner="ingest")
            with database.pool.connection() as connection:
                battle_id = int(
                    connection.execute("SELECT max(id) FROM legend_battles").fetchone()[
                        0
                    ]
                )
            initial_events = [_event(battle_id, "offense", ts, 3, 100, 35)]
            corrected_events = [_event(battle_id, "offense", ts, 2, 75, 35)]
            _publish_day(database, "#2PP", initial_events)
            processor.process_job(_army_job(database), owner="analytics")
            selection = _selection()
            before = api_analytics.get_army_analytics(stable_api_reader, selection)
            assert before is not None

            source_waiting = Event()
            component_read = Event()
            correction_finished = Event()
            original_connection = racing_api.pool.connection

            @contextmanager
            def coordinated_connection(*args, **kwargs):
                with original_connection(*args, **kwargs) as connection:

                    class CoordinatedConnection:
                        def execute(self, query, *args, **kwargs):
                            sql = str(query)
                            if (
                                "string_agg(" in sql
                                and "army_analytics_battle_facts" in sql
                            ):
                                assert connection.execute("SHOW work_mem").fetchone()[0] == (
                                    ARMY_ANALYTICS_QUERY_WORK_MEM
                                )
                                assert (
                                    connection.execute(
                                        "SHOW transaction_timeout"
                                    ).fetchone()[0]
                                    != "0"
                                )
                                source_waiting.set()
                                assert correction_finished.wait(timeout=30)
                            cursor = connection.execute(query, *args, **kwargs)
                            if "CROSS JOIN LATERAL" in sql:
                                assert (
                                    connection.execute(
                                        "SHOW transaction_timeout"
                                    ).fetchone()[0]
                                    != "0"
                                )
                                component_read.set()
                            return cursor

                        def __getattr__(self, name):
                            return getattr(connection, name)

                    yield CoordinatedConnection()

            racing_api.pool.connection = coordinated_connection

            def correct_facts() -> None:
                try:
                    assert source_waiting.wait(timeout=10)
                    assert component_read.wait(timeout=10)
                    _publish_day_correction(database, "#2PP", corrected_events)
                    correction = processor.process_job(
                        _army_job(database), owner="concurrent-correction"
                    )
                    assert correction is not None
                    assert correction.outcome == "processed"
                finally:
                    correction_finished.set()

            with ThreadPoolExecutor(max_workers=1) as executor:
                correction_future = executor.submit(correct_facts)
                concurrent = api_analytics.get_army_analytics(racing_api_reader, selection)
                correction_future.result()
            assert concurrent is not None

            after = api_analytics.get_army_analytics(stable_api_reader, selection)
            assert after is not None
            assert after["rows"] != before["rows"]
            assert (
                after["reproducibility"]["source_evidence_hash"]
                != before["reproducibility"]["source_evidence_hash"]
            )
            assert concurrent in (before, after)
        finally:
            racing_api.close()
            stable_api.close()
            database.close()


def test_perspective_disagreement_changes_fact_input_and_version(
    database_url: str, archive_server, monkeypatch) -> None:
    with domain_database(database_url) as ci:
        ts = DAY_START + timedelta(hours=1)
        _, job = store_observation(
            ci, archive_server, occurrence_key="disagreement-hash-a1", endpoint="battle_log",
            body=json.dumps({"items": [_row(True, "#8PP", FIXTURE_CODE, ts, 3, 100)]}).encode(),
            observed_at=ts + timedelta(minutes=1), normalized_tag="#2PP",
        )
        database, processor = _processor(ci, archive_server, monkeypatch)
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
