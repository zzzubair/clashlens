"""Completed-season detail retirement (issue #82, final slice).

Finalized seasons keep independently readable player/army summaries while
their daily logs, army facts, and exclusively-retired battle detail are
removed in bounded, restartable batches. Writers are fenced at
finalization; rolling logs still process live-season content.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from domain_test_support import domain_database

from clashlens.api_db import ApiDatabase
from clashlens.army_season_summaries import materialize_completed_army_season
from clashlens.db import Database
from clashlens.domain import DomainRuleError
from clashlens.season_retirement import (
    SEASON_DETAIL_RETIRED,
    finalize_season_detail,
    is_detail_retired_for_day,
    is_season_detail_retired,
    measure_season_storage,
    project_six_months,
    retire_season_detail,
)
from clashlens.season_summaries import materialize_completed_seasons

SEASON = "1785714000"
LIVE_SEASON = "1785714001"
DAY0 = datetime(2026, 5, 1, 5, 0, tzinfo=UTC)
SEASON_END = DAY0 + timedelta(days=28)
AFTER_SEASON = SEASON_END + timedelta(hours=1)
LIVE_DAY0 = SEASON_END


def _player(connection, tag="#2PP"):
    return connection.execute(
        """
        INSERT INTO players (normalized_tag, active, eligibility_state)
        VALUES (%s, true, 'eligible')
        RETURNING id
        """,
        (tag,),
    ).fetchone()[0]


def _ranked(connection, player_id, day_number, start, end, *, season=SEASON):
    return connection.execute(
        """
        INSERT INTO ranked_day_versions (
            player_id, ranked_day_start, ranked_day_end, official_season_id,
            season_day_number, season_anchor_rule_version,
            reconciliation_rule_version, result_hash, version,
            state, confidence, start_trophies, final_trophies_before_reset,
            next_start_trophies, attack_count, defense_count,
            attack_gain, observed_defense_loss
        ) VALUES (
            %s, %s, %s, %s, %s, 'season-anchor-v1',
            'reconciliation-v1', %s, 1,
            'Complete', 'exact', %s, %s, %s, 2, 1, 30, 20
        )
        RETURNING id
        """,
        (
            player_id,
            start,
            end,
            season,
            day_number,
            f"{day_number:064x}",
            6000 + (day_number - 1) * 10,
            6000 + day_number * 10,
            6000 + day_number * 10,
        ),
    ).fetchone()[0]


def _log(connection, player_id, day_number, ranked_version_id, start, *, season=SEASON):
    connection.execute(
        """
        INSERT INTO api_player_daily_logs (
            player_id, ranked_day_start, ranked_day_version_id, version,
            state, coverage, ranked_day_end, official_season_id,
            season_day_number, confidence, attack_count,
            attack_three_star_count, attack_gain, defense_count,
            defense_three_star_count, defense_loss, net_trophy_change,
            adjustments, battles, partial_reasons
        ) VALUES (
            %s, %s, %s, 1, 'Complete', 'complete', %s, %s, %s, 'exact',
            2, 1, 30, 1, 0, 20, 10, '[]'::jsonb, '[]'::jsonb, '[]'::jsonb
        )
        """,
        (
            player_id,
            start,
            ranked_version_id,
            start + timedelta(days=1),
            season,
            day_number,
        ),
    )


def _full_season(connection, player_id, *, season=SEASON, day0=DAY0):
    for day in range(1, 29):
        start = day0 + timedelta(days=day - 1)
        version_id = _ranked(connection, player_id, day, start, start + timedelta(days=1), season=season)
        _log(connection, player_id, day, version_id, start, season=season)


def _seed_army(connection, season=SEASON, day0=DAY0, *, tag="#2PP", base=7000):
    """Minimal facts + completed days so army summaries materialize."""
    connection.execute("SET LOCAL session_replication_role = replica")
    player_id = connection.execute(
        "SELECT id FROM players WHERE normalized_tag = %s", (tag,)
    ).fetchone()
    player_id = int(player_id[0]) if player_id else _player(connection, tag)
    for lens, battle_id, evidence_id in (
        ("offense", base + 1, base + 1001),
        ("defense", base + 2, base + 1002),
    ):
        connection.execute(
            """
            INSERT INTO army_analytics_battle_facts (
                battle_id, evidence_id, source_ranked_day_version_id,
                ranked_day_start, official_season_id, season_day_number,
                lens, population_player_id, stars, destruction_percentage,
                army_state, perspective_disagreement,
                battle_time_trophies, input_hash, version
            ) VALUES (
                %s, %s, 4242, %s, %s, 1, %s, %s, 3, 100, 'decoded',
                false, 6000, %s, 1
            )
            """,
            (
                battle_id,
                evidence_id,
                day0,
                season,
                lens,
                player_id,
                hashlib.sha256(f"{season}:{lens}".encode()).hexdigest(),
            ),
        )
    for day in range(1, 29):
        connection.execute(
            """
            INSERT INTO army_analytics_completed_days (
                ranked_day_start, official_season_id, season_day_number,
                fact_input_hash
            ) VALUES (%s, %s, %s, %s)
            ON CONFLICT (ranked_day_start) DO NOTHING
            """,
            (
                day0 + timedelta(days=day - 1),
                season,
                day,
                hashlib.sha256(f"{season}:day:{day}".encode()).hexdigest(),
            ),
        )
    return player_id


def _materialize_all(connection, season=SEASON):
    player_report = materialize_completed_seasons(
        connection, season_id=season, max_players=1000, now=AFTER_SEASON
    )
    army_report = materialize_completed_army_season(
        connection, season_id=season, now=AFTER_SEASON
    )
    return player_report, army_report


_CHAIN_SEQ = [990000]


def _battle_chain(connection, day_start, attacker_id, defender_id, *, battle_day=None):
    """Real battle + evidence + decode + fact chain for deletion tests."""
    _CHAIN_SEQ[0] += 1
    fake_log = _CHAIN_SEQ[0]
    fake_obs = _CHAIN_SEQ[0] + 500000
    battle_day = battle_day or day_start
    battle_id = connection.execute(
        """
        INSERT INTO legend_battles (ranked_day_start, attacker_player_id, defender_player_id)
        VALUES (%s, %s, %s) RETURNING id
        """,
        (battle_day, attacker_id, defender_id),
    ).fetchone()[0]
    connection.execute("SET LOCAL session_replication_role = replica")
    source_id = connection.execute(
        """
        INSERT INTO battle_source_rows (
            battle_log_observation_id, source_row_index, outcome, source_json
        ) VALUES (%s, 0, 'valid_legend', '{}'::jsonb) RETURNING id
        """,
        (fake_log,),
    ).fetchone()[0]
    evidence_id = connection.execute(
        """
        INSERT INTO battle_evidence (
            battle_id, source_row_id, observation_id, reporting_player_id,
            perspective, battle_timestamp, stars, destruction_percentage,
            army_share_code, attacker_gain, defender_loss,
            trophy_rule_version, source_observed_at, parser_version
        ) VALUES (
            %s, %s, %s, %s, 'attacker', %s, 3, 100, 'code',
            20, 20, 'v1', %s, 'supercell-source-parser-v1'
        ) RETURNING id
        """,
        (battle_id, source_id, fake_obs, attacker_id, day_start, day_start),
    ).fetchone()[0]
    connection.execute("SET LOCAL session_replication_role = DEFAULT")
    connection.execute(
        """
        INSERT INTO battle_perspectives (battle_id, perspective, evidence_id, source_observed_at)
        VALUES (%s, 'attacker', %s, %s)
        """,
        (battle_id, evidence_id, day_start),
    )
    connection.execute(
        """
        INSERT INTO battle_army_decodes (
            battle_id, evidence_id, perspective, decoder_version,
            catalog_version, catalog_hash, status, failure_category
        ) VALUES (%s, %s, 'attacker', 'd1', 'c1', %s, 'failed', 'undecodable')
        """,
        (battle_id, evidence_id, "a" * 64),
    )
    ranked_version = connection.execute(
        "SELECT id FROM ranked_day_versions LIMIT 1"
    ).fetchone()
    assert ranked_version is not None
    connection.execute("SET LOCAL session_replication_role = replica")
    connection.execute(
        """
        INSERT INTO army_analytics_battle_facts (
            battle_id, evidence_id, source_ranked_day_version_id,
            ranked_day_start, official_season_id, season_day_number,
            lens, population_player_id, stars, destruction_percentage,
            army_state, perspective_disagreement, input_hash, version
        ) VALUES (
            %s, %s, %s, %s, %s, 1, 'offense', %s, 3, 100, 'decoded',
            false, %s, 1
        )
        """,
        (
            battle_id,
            evidence_id,
            int(ranked_version[0]),
            battle_day,
            SEASON,
            attacker_id,
            hashlib.sha256(f"chain:{battle_id}".encode()).hexdigest(),
        ),
    )
    connection.execute("SET LOCAL session_replication_role = DEFAULT")
    return int(battle_id)


def _finalize_and_commit(connection, season=SEASON):
    report = finalize_season_detail(connection, season, AFTER_SEASON, apply=True)
    assert report["status"] == "finalized", report
    connection.commit()
    return report


def test_finalize_preview_then_full_retirement_cycle(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id = _player(connection)
                _full_season(connection, player_id)
                _seed_army(connection)
                connection.commit()
                player_report, army_report = _materialize_all(connection)
                connection.commit()
            assert player_report["materialized"] == 1
            assert army_report["season_completed"] is True
            with database.pool.connection() as connection:
                before_player = database.get_player_season_summary("#2PP", SEASON)
                before_army = database.get_army_season_summary(
                    SEASON, "offense", "troops", "usage-rate"
                )
                assert before_player is not None and len(before_player["daily_entries"]) == 28
                assert before_army is not None
                preview = finalize_season_detail(connection, SEASON, AFTER_SEASON)
                assert preview["status"] == "ready"
                assert preview["player_summary_count"] == 1
                assert connection.execute(
                    "SELECT count(*) FROM season_detail_retirements"
                ).fetchone()[0] == 0
                connection.rollback()
                finalized = _finalize_and_commit(connection)
                assert finalized["applied"] is True
                assert is_season_detail_retired(connection, SEASON)
                retire_preview = retire_season_detail(connection, SEASON)
                assert retire_preview["eligible_daily_logs"] == 28
                assert retire_preview["eligible_army_facts"] > 0
                # Preview changed nothing.
                assert connection.execute(
                    "SELECT count(*) FROM api_player_daily_logs WHERE official_season_id = %s",
                    (SEASON,),
                ).fetchone()[0] == 28
                connection.rollback()
                # Bounded restartable apply: page one row at a time.
                total_logs = 0
                for _ in range(60):
                    batch = retire_season_detail(connection, SEASON, max_rows=1, apply=True)
                    assert batch["applied"] is True
                    total_logs += batch.get("deleted_daily_logs", 0)
                    connection.commit()
                    if batch["status"] == "retired":
                        break
                assert total_logs == 28
                assert connection.execute(
                    "SELECT count(*) FROM api_player_daily_logs WHERE official_season_id = %s",
                    (SEASON,),
                ).fetchone()[0] == 0
                assert connection.execute(
                    "SELECT count(*) FROM army_analytics_battle_facts WHERE official_season_id = %s",
                    (SEASON,),
                ).fetchone()[0] == 0
                assert connection.execute(
                    "SELECT count(*) FROM army_analytics_completed_days WHERE official_season_id = %s",
                    (SEASON,),
                ).fetchone()[0] == 0
                status = connection.execute(
                    "SELECT status FROM season_detail_retirements WHERE official_season_id = %s",
                    (SEASON,),
                ).fetchone()[0]
                assert status == "retired"
                # Idempotent rerun.
                rerun = retire_season_detail(connection, SEASON, apply=True)
                assert rerun["status"] == "retired"
                connection.rollback()
            # Historical API reads are unchanged after actual deletion.
            after_player = database.get_player_season_summary("#2PP", SEASON)
            after_army = database.get_army_season_summary(
                SEASON, "offense", "troops", "usage-rate"
            )
            assert after_player == before_player
            assert after_army == before_army
            assert [s["official_season_id"] for s in database.list_player_seasons("#2PP")] == [SEASON]
        finally:
            database.close()


def test_deep_decode_chain_progresses_without_fk_rollback(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id = _player(connection)
                defender_id = _player(connection, "#8PP")
                _full_season(connection, player_id)
                _seed_army(connection)
                battle_id = _battle_chain(connection, DAY0, player_id, defender_id)
                connection.commit()
                _materialize_all(connection)
                connection.commit()
                existing = connection.execute(
                    "SELECT id, evidence_id FROM battle_army_decodes WHERE battle_id = %s",
                    (battle_id,),
                ).fetchone()
                assert existing is not None
                connection.execute(
                    "UPDATE battle_army_decodes SET is_active = false WHERE id = %s",
                    (existing[0],),
                )
                previous_id = int(existing[0])
                for index in range(11):
                    previous_id = connection.execute(
                        """
                        INSERT INTO battle_army_decodes (
                            battle_id, evidence_id, perspective, decoder_version,
                            catalog_version, catalog_hash, status, failure_category,
                            is_active, supersedes_id
                        )
                        SELECT battle_id, evidence_id, perspective, %s, %s,
                               catalog_hash, status, failure_category, %s, %s
                        FROM battle_army_decodes
                        WHERE id = %s
                        RETURNING id
                        """,
                        (
                            f"deep-{index}",
                            f"catalog-{index}",
                            index == 10,
                            previous_id,
                            previous_id,
                        ),
                    ).fetchone()[0]
                _finalize_and_commit(connection)
                for _ in range(60):
                    result = retire_season_detail(
                        connection, SEASON, max_rows=1, apply=True
                    )
                    connection.commit()
                    if result["status"] == "retired":
                        break
                else:
                    pytest.fail("deep decode chain did not retire")
                assert connection.execute(
                    "SELECT count(*) FROM battle_army_decodes WHERE battle_id = %s",
                    (battle_id,),
                ).fetchone()[0] == 0
                assert connection.execute(
                    "SELECT count(*) FROM legend_battles WHERE id = %s", (battle_id,)
                ).fetchone()[0] == 0
        finally:
            database.close()


def test_observationless_army_work_uses_half_open_season_scope(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id = _player(connection)
                defender_id = _player(connection, "#8PP")
                _full_season(connection, player_id)
                _seed_army(connection)
                battle_id = _battle_chain(connection, DAY0, player_id, defender_id)
                next_battle_id = _battle_chain(
                    connection, SEASON_END, player_id, defender_id
                )
                connection.execute(
                    "UPDATE army_analytics_battle_facts SET official_season_id = %s,"
                    " ranked_day_start = %s WHERE battle_id = %s AND official_season_id = %s",
                    (LIVE_SEASON, SEASON_END, next_battle_id, SEASON),
                )
                connection.commit()
                _materialize_all(connection)
                connection.commit()
                day_text = DAY0.strftime("%Y-%m-%dT%H:%M:%SZ")
                next_day_text = SEASON_END.strftime("%Y-%m-%dT%H:%M:%SZ")
                job_rows = [
                    (
                        "build_army_analytics",
                        "retirement-army-season",
                        json.dumps(
                            {
                                "boundary_at": day_text,
                                "generation": 1,
                                "manifest_id": 1,
                                "manifest_digest": "a" * 64,
                            }
                        ),
                    ),
                    (
                        "redecode_army",
                        "retirement-redecode-season",
                        json.dumps({"battle_ids": [battle_id]}),
                    ),
                    (
                        "build_army_analytics",
                        "retirement-army-next-season",
                        json.dumps(
                            {
                                "boundary_at": next_day_text,
                                "generation": 1,
                                "manifest_id": 1,
                                "manifest_digest": "a" * 64,
                            }
                        ),
                    ),
                    (
                        "redecode_army",
                        "retirement-redecode-next-season",
                        json.dumps({"battle_ids": [next_battle_id]}),
                    ),
                ]
                for job_row in job_rows:
                    connection.execute(
                        """
                        INSERT INTO python_processing_jobs_worker (
                            work_type, deduplication_key, input_json,
                            processing_version, domain_rule_version,
                            analytics_rule_version
                        ) VALUES (
                            %s, %s, %s::jsonb, 'clashlens-domain-processing-v1',
                            'clashlens-domain-rules-v1', 'army-analytics-v2'
                        )
                        """,
                        job_row,
                    )
                blocked = finalize_season_detail(connection, SEASON, AFTER_SEASON)
                assert blocked["status"] == "blocked"
                assert blocked["blocking_work"]["observationless_army_jobs"] == 2
                connection.execute(
                    "UPDATE python_processing_jobs SET status = 'complete',"
                    " outcome = 'processed', completed_at = clock_timestamp()"
                    " WHERE deduplication_key IN (%s, %s, %s, %s)",
                    (
                        "retirement-army-season",
                        "retirement-redecode-season",
                        "retirement-army-next-season",
                        "retirement-redecode-next-season",
                    ),
                )
                ready = finalize_season_detail(connection, SEASON, AFTER_SEASON)
                assert ready["status"] == "ready"
                connection.rollback()
        finally:
            database.close()


def test_partial_coverage_season_retires(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id = _player(connection)
                for day in (27, 28):
                    start = DAY0 + timedelta(days=day - 1)
                    version_id = _ranked(connection, player_id, day, start, start + timedelta(days=1))
                    _log(connection, player_id, day, version_id, start)
                _seed_army(connection)
                connection.commit()
                player_report, _ = _materialize_all(connection)
                connection.commit()
            assert player_report["materialized"] == 1
            with database.pool.connection() as connection:
                before = database.get_player_season_summary("#2PP", SEASON)
                assert before is not None and before["coverage_state"] == "partial"
                _finalize_and_commit(connection)
                result = retire_season_detail(connection, SEASON, apply=True)
                connection.commit()
                assert result["status"] == "retired"
            assert database.get_player_season_summary("#2PP", SEASON) == before
        finally:
            database.close()


def test_finalize_blocks_unfinished_and_stale_work(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            # Live season cannot finalize.
            with database.pool.connection() as connection:
                player_id = _player(connection)
                live_start = AFTER_SEASON - timedelta(hours=12)
                live_version = _ranked(
                    connection, player_id, 1, live_start, live_start + timedelta(days=1),
                    season=LIVE_SEASON,
                )
                _log(connection, player_id, 1, live_version, live_start, season=LIVE_SEASON)
                _seed_army(connection, season=LIVE_SEASON, day0=live_start)
                connection.commit()
                live = finalize_season_detail(connection, LIVE_SEASON, AFTER_SEASON, apply=True)
                assert live["status"] in ("not_completed", "blocked")
                connection.rollback()
            # Unknown season fails closed.
            with database.pool.connection() as connection:
                unknown = finalize_season_detail(connection, "no-such-season", AFTER_SEASON, apply=True)
                assert unknown["status"] in ("not_completed", "blocked")
                connection.rollback()
            # Missing summaries block.
            with database.pool.connection() as connection:
                player_id = _player(connection, "#8PY")
                _full_season(connection, player_id)
                connection.commit()
                missing = finalize_season_detail(connection, SEASON, AFTER_SEASON, apply=True)
                assert missing["status"] == "blocked"
                assert missing["reason"] == "verification_failed"
                assert missing["missing_player_count"] >= 1
                connection.rollback()
        finally:
            database.close()


def test_finalize_blocks_stale_summaries_and_pending_work(
    database_url: str, archive_server
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id = _player(connection)
                _full_season(connection, player_id)
                _seed_army(connection)
                connection.commit()
                _materialize_all(connection)
                connection.commit()
                # Stale summary blocks finalization.
                connection.execute(
                    "UPDATE player_season_summaries SET content_digest = repeat('1', 64)"
                    " WHERE official_season_id = %s",
                    (SEASON,),
                )
                stale = finalize_season_detail(connection, SEASON, AFTER_SEASON, apply=True)
                assert stale["status"] == "blocked"
                assert stale["stale_player_count"] == 1
                connection.rollback()
                # Non-terminal processing work scoped to the season blocks.
                from domain_test_support import store_observation

                body = (
                    __import__("pathlib").Path(__file__).parents[1]
                    / "testdata"
                    / "legend_i_battle_log_v1.json"
                ).read_bytes()
                store_observation(
                    connection_info,
                    archive_server,
                    occurrence_key="block-probe",
                    endpoint="battle_log",
                    body=body,
                    observed_at=DAY0 + timedelta(days=2),
                    normalized_tag="#2PP",
                )
                connection.execute(
                    """
                    INSERT INTO python_processing_jobs_worker (
                        work_type, deduplication_key, input_json,
                        processing_version, domain_rule_version, analytics_rule_version
                    ) VALUES (
                        'reconcile_ranked_day', 'retirement-reconcile-probe', %s::jsonb,
                        'clashlens-domain-processing-v1', 'clashlens-domain-rules-v1',
                        'legend-analytics-v1'
                    )
                    """,
                    (
                        json.dumps(
                            {
                                "player_id": player_id,
                                "ranked_day_start": (DAY0 + timedelta(days=2)).strftime(
                                    "%Y-%m-%dT05:00:00Z"
                                ),
                            }
                        ),
                    ),
                )
                blocked = finalize_season_detail(connection, SEASON, AFTER_SEASON, apply=True)
                assert blocked["status"] == "blocked"
                assert blocked["blocking_work"].get("processing_jobs") == 1
                assert blocked["blocking_work"].get("observationless_army_jobs") == 1
                connection.rollback()
        finally:
            database.close()


def test_live_shared_and_protected_records_survive(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id = _player(connection)
                _full_season(connection, player_id)
                _seed_army(connection)
                live_id = _player(connection, "#LIVE")
                _full_season(connection, live_id, season=LIVE_SEASON, day0=LIVE_DAY0)
                _seed_army(
                    connection, season=LIVE_SEASON, day0=LIVE_DAY0, tag="#LIVE", base=9000
                )
                # Retired-season battle plus a live-season battle.
                old_battle = _battle_chain(connection, DAY0, player_id, live_id)
                live_battle = _battle_chain(
                    connection, LIVE_DAY0, live_id, player_id, battle_day=LIVE_DAY0
                )
                connection.execute(
                    "UPDATE army_analytics_battle_facts SET official_season_id = %s,"
                    " ranked_day_start = %s WHERE battle_id = %s AND official_season_id = %s",
                    (LIVE_SEASON, LIVE_DAY0, live_battle, SEASON),
                )
                connection.commit()
                _materialize_all(connection)
                connection.execute(
                    """
                    INSERT INTO api_frozen_leaderboards (
                        public_id, boundary_at, version, ordering_rule_version, coverage
                    ) VALUES (gen_random_uuid(), %s, 1, 'v1', '{}'::jsonb)
                    """,
                    (SEASON_END,),
                )
                connection.commit()
                _finalize_and_commit(connection)
                result = retire_season_detail(connection, SEASON, apply=True)
                connection.commit()
                assert result["status"] == "retired"
                # Live detail is untouched.
                assert connection.execute(
                    "SELECT count(*) FROM api_player_daily_logs WHERE official_season_id = %s",
                    (LIVE_SEASON,),
                ).fetchone()[0] == 28
                assert connection.execute(
                    "SELECT count(*) FROM army_analytics_battle_facts WHERE official_season_id = %s",
                    (LIVE_SEASON,),
                ).fetchone()[0] > 0
                assert connection.execute(
                    "SELECT count(*) FROM legend_battles WHERE id = %s", (live_battle,)
                ).fetchone()[0] == 1
                # Retired battle detail is gone.
                assert connection.execute(
                    "SELECT count(*) FROM legend_battles WHERE id = %s", (old_battle,)
                ).fetchone()[0] == 0
                assert connection.execute(
                    "SELECT count(*) FROM battle_evidence WHERE battle_id = %s", (old_battle,)
                ).fetchone()[0] == 0
                assert connection.execute(
                    "SELECT count(*) FROM battle_perspectives WHERE battle_id = %s", (old_battle,)
                ).fetchone()[0] == 0
                assert connection.execute(
                    "SELECT count(*) FROM battle_army_decodes WHERE battle_id = %s", (old_battle,)
                ).fetchone()[0] == 0
                # Protected records survive: players, ranked versions, frozen
                # boards, live summaries, and the retirement fence itself.
                assert connection.execute("SELECT count(*) FROM players").fetchone()[0] >= 2
                assert connection.execute(
                    "SELECT count(*) FROM ranked_day_versions WHERE official_season_id = %s",
                    (SEASON,),
                ).fetchone()[0] == 28
                assert connection.execute(
                    "SELECT count(*) FROM api_frozen_leaderboards"
                ).fetchone()[0] == 1
                assert connection.execute(
                    "SELECT count(*) FROM player_season_summaries WHERE official_season_id = %s",
                    (SEASON,),
                ).fetchone()[0] == 1
                assert connection.execute(
                    "SELECT count(*) FROM army_season_summaries WHERE official_season_id = %s",
                    (SEASON,),
                ).fetchone()[0] > 0
                connection.rollback()
        finally:
            database.close()


def test_post_finalization_writes_are_fenced(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = Database(connection_info)
        api = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id = _player(connection)
                _full_season(connection, player_id)
                _seed_army(connection)
                connection.commit()
                _materialize_all(connection)
                connection.commit()
                before = api.get_player_season_summary("#2PP", SEASON)
                before_army = api.get_army_season_summary(SEASON, "offense", "troops", "usage-rate")
                _finalize_and_commit(connection)
                # Rematerialization returns the explicit retired marker.
                from clashlens.season_summaries import materialize_player_season

                outcome = materialize_player_season(connection, player_id, SEASON)
                assert outcome["status"] == SEASON_DETAIL_RETIRED
                backfill = materialize_completed_seasons(
                    connection, season_id=SEASON, max_players=10, now=AFTER_SEASON
                )
                assert backfill["reason"] == SEASON_DETAIL_RETIRED
                from clashlens.army_season_summaries import materialize_army_season

                army_outcome = materialize_army_season(connection, SEASON, "offense")
                assert army_outcome["status"] == SEASON_DETAIL_RETIRED
                # Targeted correction raises before domain mutation.
                day5 = DAY0 + timedelta(days=4)
                version_id = connection.execute(
                    "SELECT id FROM ranked_day_versions WHERE player_id = %s"
                    " AND ranked_day_start = %s",
                    (player_id, day5),
                ).fetchone()[0]
                from clashlens.reconciliation import ReconciliationResult

                with pytest.raises(DomainRuleError, match=SEASON_DETAIL_RETIRED):
                    database._publish_player_daily_log(
                        connection,
                        player_id=player_id,
                        ranked_day_start=day5,
                        ranked_day_end=day5 + timedelta(days=1),
                        official_season_id=SEASON,
                        season_day_number=5,
                        version_number=99,
                        ranked_day_version_id=int(version_id),
                        result=ReconciliationResult(
                            state="Complete", confidence="exact", attack_count=0,
                            defense_count=0, attack_trophy_gain=0,
                            observed_defense_loss=0, automatic_defense_loss=None,
                            automatic_defense_evidence_state="unknown",
                            boundary_adjustment=0, boundary_adjustment_type=None,
                            final_trophies_before_reset=6050,
                            shield_state="not_inferred", shield_duration_days=None,
                            coverage_complete=True, failure_reasons=(),
                            net_trophy_change=0,
                        ),
                        contribution_evidence=[],
                    )
                # Reconciliation for the retired day raises on any connection.
                with psycopg.connect(connection_info) as other:
                    assert is_detail_retired_for_day(other, day5) is True
                    assert is_detail_retired_for_day(other, LIVE_DAY0) is False
                connection.rollback()
                # Summaries are unchanged by fenced writes.
                assert api.get_player_season_summary("#2PP", SEASON) == before
                assert (
                    api.get_army_season_summary(SEASON, "offense", "troops", "usage-rate")
                    == before_army
                )
        finally:
            database.close()
            api.close()


def test_rolling_log_processes_live_content_after_finalization(
    database_url: str, archive_server
) -> None:
    from domain_test_support import store_observation
    from test_domain_processing_postgres import _processor


    battle_body = (
        __import__("pathlib").Path(__file__).parents[1]
        / "testdata"
        / "legend_i_battle_log_v1.json"
    ).read_bytes()
    with domain_database(database_url, include_coordinator=True) as connection_info:
        # The static battle fixture is in the August season; seed its canonical
        # anchor so the fail-closed guard can distinguish it from an unknown day.
        with psycopg.connect(connection_info) as connection:
            connection.execute("SET LOCAL session_replication_role = replica")
            connection.execute(
                """
                INSERT INTO legend_season_anchors (
                    current_league_season_id, previous_league_season_id,
                    current_start, previous_start, anchor_rule_version,
                    source_profile_version_id, state
                ) VALUES (%s, %s, %s, %s, 'legend-season-anchor-v1', 1, 'confirmed')
                """,
                (
                    LIVE_SEASON,
                    SEASON,
                    datetime(2026, 8, 1, 5, tzinfo=UTC),
                    DAY0,
                ),
            )
            connection.commit()
        database, processor = _processor(connection_info, archive_server)
        try:
            _observation_id, job_id = store_observation(
                connection_info,
                archive_server,
                occurrence_key="retire-live-log",
                endpoint="battle_log",
                body=battle_body,
                observed_at=LIVE_DAY0 + timedelta(hours=1),
                normalized_tag="#2PP",
            )
            result = processor.process_job(job_id, owner="retire-live")
            assert result is not None and result.outcome == "processed"
        finally:
            database.close()
        with psycopg.connect(connection_info) as connection:
            live_battles_before = connection.execute(
                "SELECT count(*) FROM legend_battles"
            ).fetchone()[0]
            assert live_battles_before > 0
            connection.commit()
        # Finalize an unrelated historical season built from direct fixtures,
        # then prove the live battle path still ingests new observations.
        api = ApiDatabase(connection_info)
        try:
            with api.pool.connection() as connection:
                player_id = connection.execute(
                    "SELECT id FROM players WHERE normalized_tag = '#2PP'"
                ).fetchone()[0]
                _full_season(connection, int(player_id))
                _seed_army(connection)
                connection.commit()
                _materialize_all(connection)
                connection.commit()
                _finalize_and_commit(connection)
        finally:
            api.close()
        database, processor = _processor(connection_info, archive_server)
        try:
            _observation_id, job_id = store_observation(
                connection_info,
                archive_server,
                occurrence_key="retire-live-log-2",
                endpoint="battle_log",
                body=battle_body,
                observed_at=LIVE_DAY0 + timedelta(hours=2),
                normalized_tag="#2PP",
            )
            result = processor.process_job(job_id, owner="retire-live-2")
            assert result is not None and result.outcome == "processed"
            with database.pool.connection() as connection:
                assert connection.execute(
                    "SELECT count(*) FROM legend_battles"
                ).fetchone()[0] >= live_battles_before
        finally:
            database.close()


def test_retired_only_replay_is_terminal_and_does_not_recreate_detail(
    database_url: str, archive_server
) -> None:
    from domain_test_support import store_observation
    from test_domain_processing_postgres import _processor

    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id = _player(connection)
                _full_season(connection, player_id)
                _seed_army(connection)
                connection.commit()
                _materialize_all(connection)
                connection.commit()
                _finalize_and_commit(connection)
                result = retire_season_detail(connection, SEASON, apply=True)
                assert result["status"] == "retired"
                connection.commit()
            body = json.loads(
                (Path(__file__).parents[1] / "testdata" / "legend_i_battle_log_v1.json").read_bytes()
            )
            body["items"][0]["battleTimestamp"] = "2026-05-01T12:00:00Z"
            _observation_id, job_id = store_observation(
                connection_info,
                archive_server,
                occurrence_key="retired-only-replay",
                endpoint="battle_log",
                body=json.dumps(body).encode(),
                observed_at=LIVE_DAY0,
                normalized_tag="#2PP",
            )
            worker_database, processor = _processor(connection_info, archive_server)
            try:
                processed = processor.process_job(job_id, owner="retired-only-replay")
                assert processed is not None
                assert processed.outcome == SEASON_DETAIL_RETIRED
            finally:
                worker_database.close()
            with psycopg.connect(connection_info) as connection:
                assert connection.execute(
                    "SELECT count(*) FROM legend_battles"
                ).fetchone()[0] == 0
                assert connection.execute(
                    "SELECT status, outcome FROM python_processing_jobs WHERE id = %s",
                    (job_id,),
                ).fetchone() == ("complete", SEASON_DETAIL_RETIRED)
                assert connection.execute(
                    "SELECT count(*) FROM battle_log_observations"
                ).fetchone()[0] == 0
                assert connection.execute(
                    "SELECT count(*) FROM parsed_source_payloads"
                ).fetchone()[0] == 0
        finally:
            database.close()


def test_failure_injection_preserves_summaries_and_fence(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id = _player(connection)
                _full_season(connection, player_id)
                _seed_army(connection)
                connection.commit()
                _materialize_all(connection)
                connection.commit()
                _finalize_and_commit(connection)
                before = database.get_player_season_summary("#2PP", SEASON)
                # Deleting the only valid summary blocks retirement.
                connection.execute(
                    "DELETE FROM player_season_summaries WHERE official_season_id = %s",
                    (SEASON,),
                )
                blocked = retire_season_detail(connection, SEASON, apply=True)
                assert blocked["status"] == "blocked"
                assert blocked["reason"] == "player_summaries_missing"
                connection.rollback()
                # Caller-owned transaction rollback restores detail.
                connection.execute(
                    "DELETE FROM api_player_daily_logs WHERE official_season_id = %s",
                    (SEASON,),
                )
                connection.rollback()
                assert connection.execute(
                    "SELECT count(*) FROM api_player_daily_logs WHERE official_season_id = %s",
                    (SEASON,),
                ).fetchone()[0] == 28
                assert database.get_player_season_summary("#2PP", SEASON) == before
                assert is_season_detail_retired(connection, SEASON) is True
                connection.rollback()
        finally:
            database.close()


def test_two_connections_see_fence_and_serialize_finalize(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id = _player(connection)
                _full_season(connection, player_id)
                _seed_army(connection)
                connection.commit()
                _materialize_all(connection)
                connection.commit()
                _finalize_and_commit(connection)
            # A second connection sees the fence after commit.
            with psycopg.connect(connection_info) as other:
                assert is_season_detail_retired(other, SEASON) is True
                repeat = finalize_season_detail(other, SEASON, AFTER_SEASON, apply=True)
                assert repeat["already_finalized"] is True
                other.rollback()
            # Concurrent first materialization loses to the fence.
            with database.pool.connection() as connection:
                from clashlens.season_summaries import materialize_player_season

                outcome = materialize_player_season(connection, player_id, SEASON)
                assert outcome["status"] == SEASON_DETAIL_RETIRED
                connection.rollback()
        finally:
            database.close()


def test_projection_marks_unmeasured_components_and_budget_unavailable() -> None:
    projection = project_six_months(
        player_season_bytes=100.0,
        army_season_bytes=50.0,
        live_detail_bytes_per_day=None,
        daily_bookkeeping_bytes_per_day=None,
        usable_bytes=1_000_000,
    )
    assert projection["projection_semantics"] == "lower_bound_with_unmeasured_components"
    assert projection["live_detail_bytes"] is None
    assert projection["bookkeeping_bytes"] is None
    assert projection["fits_budget"] is None
    assert set(projection["unmeasured_components"]) == {
        "live_detail_bytes_per_day",
        "daily_bookkeeping_bytes_per_day",
    }


def test_rolling_log_waits_for_finalization_when_ranked_day_is_missing(
    database_url: str,
) -> None:
    """A canonical partial season still shares the finalization fence lock."""
    from clashlens.season_retirement import acquire_season_lock

    with domain_database(database_url, include_coordinator=True) as connection_info:
        with psycopg.connect(connection_info) as connection:
            player_id = _player(connection, "#RACE")
            _full_season(connection, player_id)
            _seed_army(connection, tag="#RACE", base=7100)
            connection.commit()
            # The direct fixture anchor is valid canonical metadata; replica
            # mode also lets this regression remove the ranked-day row while
            # retaining the partial season's remaining history.
            connection.execute("SET LOCAL session_replication_role = replica")
            connection.execute(
                "DELETE FROM ranked_day_versions WHERE ranked_day_start = %s",
                (DAY0,),
            )
            connection.execute(
                "DELETE FROM api_player_daily_logs WHERE ranked_day_start = %s",
                (DAY0,),
            )
            connection.execute(
                """
                INSERT INTO legend_season_anchors (
                    current_league_season_id, previous_league_season_id,
                    current_start, previous_start, anchor_rule_version,
                    source_profile_version_id, state
                ) VALUES (%s, %s, %s, %s, 'legend-season-anchor-v1', 1, 'confirmed')
                """,
                (LIVE_SEASON, SEASON, LIVE_DAY0, DAY0),
            )
            connection.commit()
            _materialize_all(connection)
            connection.commit()

        ready = threading.Event()
        release = threading.Event()
        finalized: list[dict[str, object]] = []

        def finalize() -> None:
            with psycopg.connect(connection_info) as finalizer:
                acquire_season_lock(finalizer, SEASON)
                ready.set()
                assert release.wait(10)
                finalized.append(
                    finalize_season_detail(finalizer, SEASON, AFTER_SEASON, apply=True)
                )
                finalizer.commit()

        thread = threading.Thread(target=finalize)
        thread.start()
        assert ready.wait(10)
        result: list[object] = []
        guard_errors: list[BaseException] = []

        def guard() -> None:
            item = SimpleNamespace(
                battle=SimpleNamespace(ranked_day_start=DAY0),
            )
            try:
                with psycopg.connect(connection_info) as writer:
                    result.extend(Database._guard_battle_rows(writer, [item]))
            except (psycopg.Error, ValueError) as error:  # pragma: no cover
                guard_errors.append(error)

        writer = threading.Thread(target=guard)
        writer.start()
        time.sleep(0.1)
        assert writer.is_alive(), guard_errors
        release.set()
        writer.join(10)
        thread.join(10)
        assert not writer.is_alive() and not thread.is_alive()
        assert finalized and finalized[0]["status"] == "finalized", repr(finalized)
        assert result == []
        with psycopg.connect(connection_info) as connection:
            assert connection.execute(
                "SELECT count(*) FROM legend_battles"
            ).fetchone()[0] == 0
            assert connection.execute(
                "SELECT count(*) FROM battle_evidence"
            ).fetchone()[0] == 0


def test_rolling_log_rejects_noncanonical_day_without_resolution_source(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        item = SimpleNamespace(
            battle=SimpleNamespace(ranked_day_start=DAY0 - timedelta(days=1)),
        )
        with psycopg.connect(connection_info) as connection:
            before = connection.execute("SELECT count(*) FROM legend_battles").fetchone()[0]
            assert Database._guard_battle_rows(connection, [item]) == []
            assert connection.execute("SELECT count(*) FROM legend_battles").fetchone()[0] == before


def test_measurement_snapshot_and_projection_labels(database_url: str) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        database = ApiDatabase(connection_info)
        try:
            with database.pool.connection() as connection:
                player_id = _player(connection)
                _full_season(connection, player_id)
                _seed_army(connection)
                connection.commit()
                _materialize_all(connection)
                connection.execute("CREATE SEQUENCE issue82_storage_probe_seq")
                connection.execute(
                    "CREATE TABLE issue82_storage_probe_partition "
                    "(id bigint) PARTITION BY RANGE (id)"
                )
                connection.execute(
                    "CREATE TABLE issue82_storage_probe_partition_1 "
                    "PARTITION OF issue82_storage_probe_partition "
                    "FOR VALUES FROM (0) TO (100)"
                )
                connection.commit()
                snapshot = measure_season_storage(connection, SEASON)
                assert snapshot["tables"]["player_season_summaries"]["rows"] == 1
                sequence = snapshot["tables"]["issue82_storage_probe_seq"]
                assert sequence["relation_kind"] == "S"
                assert sequence["row_count_semantics"] == "not_applicable_for_sequence"
                assert sequence["allocated_bytes"] > 0
                parent = snapshot["tables"]["issue82_storage_probe_partition"]
                assert parent["relation_kind"] == "p"
                assert parent["row_count_semantics"] == "not_counted_for_partition_parent"
                assert parent["allocated_bytes"] == 0
                assert "p" in snapshot["relation_kinds"]["included"]
                assert snapshot["measured_relation_total_bytes"] == sum(
                    entry["allocated_bytes"]
                    for entry in snapshot["tables"].values()
                )
                assert "i" in snapshot["relation_kinds"]["excluded"]
                assert snapshot["tables"]["api_player_daily_logs"]["rows"] == 28
                assert snapshot["season_counts"]["api_player_daily_logs"] == 28
                assert snapshot["summaries"]["player_season"]["rows"] == 1
                assert snapshot["summaries"]["player_season"]["total_bytes"] > 0
                assert any("WAL" in gap for gap in snapshot["unmeasured"])
                per_player = (
                    snapshot["summaries"]["player_season"]["total_bytes"]
                    / snapshot["summaries"]["player_season"]["rows"]
                )
                projection = project_six_months(
                    player_season_bytes=per_player,
                    army_season_bytes=float(
                        snapshot["summaries"]["army_season"]["total_bytes"]
                    ),
                    live_detail_bytes_per_day=1_000_000.0,
                    daily_bookkeeping_bytes_per_day=500_000.0,
                    players=12500,
                    usable_bytes=1_017_969_311_744,
                )
                assert projection["players"] == 12500
                assert projection["projected_total_bytes"] > 0
                assert any("Step 9" in label for label in projection["labels"])
                assert isinstance(projection["fits_budget"], bool)
                with pytest.raises(ValueError, match="projection population"):
                    project_six_months(
                        player_season_bytes=1.0, army_season_bytes=1.0,
                        live_detail_bytes_per_day=1.0,
                        daily_bookkeeping_bytes_per_day=1.0, players=0,
                    )
        finally:
            database.close()
