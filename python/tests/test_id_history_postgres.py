"""Issue 126: ingestion, retained history, and catalogue changes across cleanup."""
from __future__ import annotations

import json
from datetime import timedelta

import psycopg
import pytest
from domain_test_support import (
    domain_database,
    enable_direct_army_fixture,
    store_observation,
)
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from test_army_analytics_publication_postgres import (
    DAY_START,
    SEASON_ID,
    _event,
    _processor,
    _publish_day,
    _row,
)
from test_army_history import fact
from test_army_season_summaries_postgres import SEASON as MEASURE_SEASON
from test_army_season_summaries_postgres import _seed
from test_private_api import NOW, NOW_SECONDS, TS_CURRENT, signed_headers
from test_reconciliation_postgres import _processor as _real_processor
from test_reconciliation_postgres import _store_baseline_pair
from test_season_detail_retirement_postgres import (
    _ensure_canonical_anchor,
    _full_season,
    _player,
    _seed_army,
)

from clashlens import (
    api_analytics,
    api_players,
    boundary_publication,
    catalog,
    reconciliation_db,
)
from clashlens.api import create_app
from clashlens.api_db import ApiDatabase
from clashlens.army_analytics import (
    CATEGORIES,
    ArmyAnalyticsSelection,
    build_army_result,
)
from clashlens.army_history import HISTORY_READ_CATEGORIES
from clashlens.army_season_summaries import materialize_army_season
from clashlens.season_retirement import finalize_season_detail, retire_season_detail
from clashlens.season_summaries import materialize_completed_seasons

SEASON_START = DAY_START - timedelta(days=22)
SEASON_END = SEASON_START + timedelta(days=28)
UNKNOWN_CODE = "h900p900e14_900u5x900-1x901s1x900i2x900-1x901"


def test_raw_battle_reconciles_to_both_player_pages_and_analytics(database_url, archive_server, monkeypatch):
    with domain_database(database_url) as ci:
        database, processor = _real_processor(ci, archive_server)
        api = ApiDatabase(ci)
        try:
            # Capture both members together; reset membership is immutable.
            with psycopg.connect(ci) as connection:
                members = [(tag, _player(connection, tag)) for tag in ("#2PP", "#8PP")]
                for boundary in (DAY_START, DAY_START + timedelta(days=1)):
                    sweep = connection.execute("""INSERT INTO collector_reset_sweeps
                        (boundary_at, member_ids, membership_captured_at)
                        VALUES (%s, %s, clock_timestamp()) RETURNING id""",
                        (boundary, [player for _, player in members])).fetchone()[0]
                    for tag, player in members:
                        connection.execute("""INSERT INTO collector_work
                            (kind, lane, scope, player_id, normalized_tag, sweep_id, due_at, coalescing_key)
                            VALUES ('reset_baseline', 'reset', 'player', %s, %s, %s, %s, %s)""",
                            (player, tag, sweep, boundary, f"trace-{sweep}-{player}"))
            for tag in ("#2PP", "#8PP"):
                pair = _store_baseline_pair(ci, archive_server, key=f"start-{tag}", boundary=DAY_START,
                                           trophies=6000, empty_battle_log=True, normalized_tag=tag)
                for job in pair[2:]:
                    assert processor.process_job(job, owner=f"baseline-{job}").outcome == "processed"
            _ingest(ci, archive_server, processor, "trace-1", True, code="u5x58")
            # The defender cannot gain a daily event from only its opponent's log.
            for tag in ("#2PP", "#8PP"):
                job = reconciliation_db.enqueue_reconciliation(database, player_tag=tag, day_start=DAY_START,
                    now=DAY_START + timedelta(hours=2), request_key=f"single-{tag}")
                assert processor.process_job(job, owner=f"single-{tag}").outcome == "processed"
            missing = api_players.get_player_page(api, "#8PP", now=DAY_START + timedelta(hours=3), freshness_seconds=300)
            assert missing["daily_logs"][0]["battles"] == []
            _ingest(ci, archive_server, processor, "trace-2", False, code="u5x58")
            for tag, trophies in (("#2PP", 6040), ("#8PP", 5960)):
                pair = _store_baseline_pair(ci, archive_server, key=f"end-{tag}", boundary=DAY_START + timedelta(days=1),
                    trophies=trophies, empty_battle_log=True, normalized_tag=tag)
                for job in pair[2:]:
                    assert processor.process_job(job, owner=f"baseline-{job}").outcome == "processed"
                job = reconciliation_db.enqueue_reconciliation(database, player_tag=tag, day_start=DAY_START,
                    now=DAY_START + timedelta(days=1), request_key=f"complete-{tag}")
                assert processor.process_job(job, owner=f"complete-{tag}").outcome == "processed"
            pages = [api_players.get_player_page(api, tag, now=DAY_START + timedelta(days=1, hours=1), freshness_seconds=300)
                     for tag in ("#2PP", "#8PP")]
            logs = [next(day for day in page["daily_logs"] if day["battles"]) for page in pages]
            battles = [log["battles"][0] for log in logs]
            assert all(len(log["battles"]) == 1 for log in logs)
            assert battles[0]["battle_id"] == battles[1]["battle_id"]
            assert {battle["lens"] for battle in battles} == {"offense", "defense"}
            assert [battle["trophy_change"] for battle in battles] == [40, -40]
            assert logs[0]["state"] == "Complete"
            assert logs[1]["state"] == "Partial"
            assert logs[1]["partial_reasons"] == ["automatic_defense_basis_unavailable"]
            # Use the existing completed-day fixture publication seam. It reads
            # the real reconciled logs and decodes above, without changing them.
            enable_direct_army_fixture(database, monkeypatch)
            with database.pool.connection() as connection:
                boundary_publication._enqueue_army_analytics(database, connection, ranked_day_start=DAY_START)
                jobs = connection.execute("SELECT id FROM python_processing_jobs WHERE work_type='build_army_analytics' AND status='pending' AND input_json ? 'ranked_day_start' ORDER BY id").fetchall()
            assert jobs
            for (job,) in jobs:
                result = processor.process_job(job, owner="trace-analytics")
                assert result.outcome == "processed", result
            with database.pool.connection() as connection:
                for lens in ("offense", "defense"):
                    materialize_army_season(connection, SEASON_ID, lens)
                connection.commit()
            for lens in ("offense", "defense"):
                result = api_analytics.get_army_season_summary(api, SEASON_ID, lens, "troops", "usage-rate")
                assert result["total_attacks"] == (1 if lens == "offense" else 0)
                if lens == "offense":
                    assert result["rows"][0]["usage_count"] == 1
                    assert result["rows"][0]["quantity"] == 5
                else:
                    assert result["rows"] == []
        finally:
            database.close()
            api.close()


def _ingest(ci, archive_server, processor, key, attack, *, stars=3, destruction=100, code=UNKNOWN_CODE, opponent=None):
    tag, other = ("#2PP", "#8PP") if attack else ("#8PP", "#2PP")
    _, job = store_observation(
        ci, archive_server, occurrence_key=key, endpoint="battle_log",
        normalized_tag=tag, observed_at=DAY_START + timedelta(hours=2, minutes=int(key.split("-")[-1])),
        body=json.dumps({"items": [_row(attack, opponent or other, code, DAY_START + timedelta(hours=1), stars, destruction)]}).encode(),
    )
    result = processor.process_job(job, owner=key)
    assert result is not None and result.outcome == "processed"
    return job


def _publish_reports(database, processor):
    with database.pool.connection() as connection:
        reports = connection.execute(
            """SELECT b.id, p.perspective, e.stars, e.destruction_percentage,
                      e.battle_timestamp, e.attacker_gain, e.defender_loss, owner.normalized_tag
               FROM legend_battles b JOIN battle_perspectives p ON p.battle_id=b.id
               JOIN battle_evidence e ON e.id=p.evidence_id
               JOIN players owner ON owner.id=e.reporting_player_id ORDER BY p.perspective"""
        ).fetchall()
    events = {}
    for battle_id, side, stars, destruction, timestamp, attack_delta, defense_delta, tag in reports:
        attack = side == "attacker"
        events.setdefault(tag, []).append(
            _event(battle_id, "offense" if attack else "defense", timestamp, stars, destruction,
                   attack_delta if attack else -defense_delta)
        )
    for tag, battles in events.items():
        _publish_day(database, tag, battles)
    with database.pool.connection() as connection:
        jobs = connection.execute("SELECT id FROM python_processing_jobs WHERE work_type='build_army_analytics' AND status='pending' ORDER BY id").fetchall()
    for (job,) in jobs:
        result = processor.process_job(job, owner="history-facts")
        assert result is not None and result.outcome == "processed"


@pytest.mark.parametrize("first_attack", [True, False])
def test_reports_in_either_order_repeated_polls_restart_and_correction(
    database_url, archive_server, monkeypatch, first_attack,
):
    with domain_database(database_url) as ci:
        database, processor = _processor(ci, archive_server, monkeypatch)
        api = ApiDatabase(ci)
        try:
            first_job = _ingest(ci, archive_server, processor, "first-1", first_attack)
            _ingest(ci, archive_server, processor, "repeat-2", first_attack)
            with database.pool.connection() as connection:
                assert connection.execute("SELECT count(*) FROM legend_battles").fetchone()[0] == 1
                assert connection.execute("SELECT count(*) FROM battle_perspectives").fetchone()[0] == 1
            database.close()
            database, processor = _processor(ci, archive_server, monkeypatch)
            assert processor.process_job(first_job, owner="restart") is None
            _ingest(ci, archive_server, processor, "opposite-3", not first_attack, stars=2, destruction=80)
            # Newer correction affects its own side, preserving the disagreement.
            _ingest(ci, archive_server, processor, "correction-4", first_attack, stars=1, destruction=50)
            _publish_reports(database, processor)
            with database.pool.connection() as connection:
                assert connection.execute("SELECT count(*) FROM legend_battles").fetchone()[0] == 1
                assert connection.execute("SELECT count(*) FROM battle_perspectives").fetchone()[0] == 2
                for lens in ("offense", "defense"):
                    materialize_army_season(connection, SEASON_ID, lens)
                connection.commit()
            for lens in ("offense", "defense"):
                result = api_analytics.get_army_season_summary(api, SEASON_ID, lens, "heroes", "usage-rate")
                assert result["total_attacks"] == 1
                assert result["perspective_disagreement_count"] == 1
                # Current facts retain each corrected outcome; history retains usage.
                with database.pool.connection() as connection:
                    outcome = connection.execute("SELECT stars FROM army_analytics_battle_facts WHERE lens=%s AND is_current", (lens,)).fetchone()[0]
                assert outcome == (1 if (lens == "offense") == first_attack else 2)
                assert result["rows"][0]["usage_count"] == 1
        finally:
            database.close()
            api.close()


def test_unknown_history_survives_retirement_retry_and_later_naming(
    database_url, archive_server, monkeypatch, tmp_path,
):
    with domain_database(database_url) as ci:
        database, processor = _processor(ci, archive_server, monkeypatch)
        api = ApiDatabase(ci)
        try:
            _ingest(ci, archive_server, processor, "attacker-1", True)
            _ingest(ci, archive_server, processor, "defender-2", False)
            _ingest(ci, archive_server, processor, "other-3", True, opponent="#9PP", code="u1x58", stars=1, destruction=50)
            _publish_reports(database, processor)
            with database.pool.connection() as connection:
                _ensure_canonical_anchor(connection, SEASON_ID, SEASON_START)
                # Bind fixture publications to their exact ranked-day version,
                # as the production publication writer does.
                connection.execute("""UPDATE api_player_daily_logs l
                    SET ranked_day_version_id = r.id FROM ranked_day_versions r
                    WHERE r.player_id=l.player_id AND r.ranked_day_start=l.ranked_day_start""")
                players = materialize_completed_seasons(connection, season_id=SEASON_ID, max_players=100, now=SEASON_END + timedelta(days=1))
                assert players["materialized"] == 2
                # Rollback models an interrupted summary transaction.
                for lens in ("offense", "defense"):
                    materialize_army_season(connection, SEASON_ID, lens)
                connection.rollback()
                assert connection.execute("SELECT count(*) FROM army_season_summaries").fetchone()[0] == 0
                _ensure_canonical_anchor(connection, SEASON_ID, SEASON_START)
                connection.execute("""UPDATE api_player_daily_logs l SET ranked_day_version_id=r.id
                    FROM ranked_day_versions r WHERE r.player_id=l.player_id AND r.ranked_day_start=l.ranked_day_start""")
                materialize_completed_seasons(connection, season_id=SEASON_ID, max_players=100, now=SEASON_END + timedelta(days=1))
                for lens in ("offense", "defense"):
                    materialize_army_season(connection, SEASON_ID, lens)
                connection.commit()
            before_players = {tag: api_players.get_player_season_summary(api, tag, SEASON_ID) for tag in ("#2PP", "#8PP")}
            before = {category: api_analytics.get_army_season_summary(api, SEASON_ID, "offense", category, "usage-rate") for category in HISTORY_READ_CATEGORIES}
            assert [row["unit_id"] for row in before["troops"]["rows"]] == ["troop:58"]
            with database.pool.connection() as connection:
                finalized = finalize_season_detail(connection, SEASON_ID, SEASON_END + timedelta(days=1), apply=True)
                assert finalized["status"] == "finalized", json.dumps(finalized, default=str)
                connection.commit()
                retire_season_detail(connection, SEASON_ID, max_rows=1, apply=True)
                connection.rollback()
                for _ in range(30):
                    report = retire_season_detail(connection, SEASON_ID, max_rows=1, apply=True)
                    connection.commit()
                    if report["status"] == "retired":
                        break
                assert report["status"] == "retired", report
                assert connection.execute("SELECT count(*) FROM army_analytics_battle_facts").fetchone()[0] == 0
                assert connection.execute("SELECT count(*) FROM legend_battles").fetchone()[0] == 0
                assert retire_season_detail(connection, SEASON_ID, apply=True)["status"] == "retired"
            archive_server[3].objects.clear()
            reads_before = archive_server[3].get_count
            for namespace in ("troop", "spell", "hero", "pet", "equipment"):
                monkeypatch.setitem(catalog._CATALOG_ENTRIES, f"{namespace}:900", {"name": f"Named {namespace}", "category": namespace, "is_siege": False})
            monkeypatch.setitem(catalog._CATALOG_ENTRIES, "troop:901", {"name": "Named siege", "category": "troop", "is_siege": True})
            app = create_app(database=api, keys={("typescript-website", "current"): TS_CURRENT}, clock=lambda: NOW_SECONDS, now=lambda: NOW)
            with TestClient(app) as client:
                for category in HISTORY_READ_CATEGORIES:
                    target = f"/v1/analytics/armies/seasons/{SEASON_ID}?lens=offense&category={category}&sort=usage-rate"
                    response = client.get(target, headers=signed_headers(target))
                    assert response.status_code == 200
                    result = response.json()
                    assert result["unknown_affected_attacks"] == 1  # At collection.
                    assert result["total_attacks"] == 2
                    assert result["versions"]["analytics"] == "army-unit-usage-v1"
                    assert result["rows"]
                    for row in result["rows"]:
                        assert row["usage_count"] == 1
                        assert row["usage_denominator"] == 1
                        assert row["battle_trophies"] == (6040 if row["unit_id"] == "troop:58" else 6000)
                        assert row["quantity"] == (5 if row["unit_id"] == "troop:900" else 1)
                        assert "star_counts" not in row
                        assert "average_destruction" not in row
                        assert "Unknown" not in row["label"]
            api = ApiDatabase(ci)
            assert {tag: api_players.get_player_season_summary(api, tag, SEASON_ID) for tag in before_players} == before_players
            assert archive_server[3].get_count == reads_before
            # Export only the retired representation for the fixture restore
            # and browser rehearsal. There are no battle details or raw bodies.
            exported = {"tables": {}, "json_columns": {}, "army_api": {}}
            with database.pool.connection() as connection:
                for table in ("players", "army_season_summaries", "player_season_summaries", "season_detail_retirements"):
                    cursor = connection.execute(f"SELECT * FROM {table}")
                    columns = [column.name for column in cursor.description]
                    exported["json_columns"][table] = [column.name for column in cursor.description if column.type_code == 3802]
                    exported["tables"][table] = [dict(zip(columns, row)) for row in cursor.fetchall()]
            for lens in ("offense", "defense"):
                for category in sorted(HISTORY_READ_CATEGORIES):
                    exported["army_api"][f"{lens}/{category}"] = api_analytics.get_army_season_summary(api, SEASON_ID, lens, category, "usage-rate")
            exported["player_api"] = before_players
            (tmp_path / "retained-history.json").write_text(json.dumps(exported, default=str, sort_keys=True))
        finally:
            database.close()
            api.close()


def test_all_players_keep_28_daily_trophy_entries_and_totals(database_url):
    with domain_database(database_url) as ci:
        api = ApiDatabase(ci)
        try:
            with psycopg.connect(ci) as connection:
                _ensure_canonical_anchor(connection, SEASON_ID, SEASON_START)
                for tag in ("#2PP", "#8PP", "#9PP"):
                    _full_season(connection, _player(connection, tag), season=SEASON_ID, day0=SEASON_START)
                _seed_army(connection, season=SEASON_ID, day0=SEASON_START)
                materialize_completed_seasons(connection, season_id=SEASON_ID, max_players=100, now=SEASON_END + timedelta(days=1))
                for lens in ("offense", "defense"):
                    materialize_army_season(connection, SEASON_ID, lens)
            before = {tag: api_players.get_player_season_summary(api, tag, SEASON_ID) for tag in ("#2PP", "#8PP", "#9PP")}
            assert all(len(result["daily_entries"]) == 28 for result in before.values())
            with psycopg.connect(ci) as connection:
                assert finalize_season_detail(connection, SEASON_ID, SEASON_END + timedelta(days=1), apply=True)["status"] == "finalized"
                connection.commit()
                for _ in range(10):
                    result = retire_season_detail(connection, SEASON_ID, max_rows=10, apply=True)
                    connection.commit()
                    if result["status"] == "retired":
                        break
                assert result["status"] == "retired"
            assert {tag: api_players.get_player_season_summary(api, tag, SEASON_ID) for tag in before} == before
        finally:
            api.close()


def test_measure_retained_season_bytes_against_label_based_rows(database_url):
    """A repeatable 224-attack season cost with quantities and trophies."""
    specs = []
    for index in range(224):
        code = UNKNOWN_CODE if index % 8 == 0 else f"h0p9e14_32u{1 + index % 5}x58s1x2i{1 + index % 4}x0"
        decoded = fact(code, 3 if index % 2 else 2, 100 if index % 2 else 80, trophies=6000 + (index % 28) * 10)
        specs.append({**decoded, "trophies": decoded["battle_time_trophies"], "destruction": decoded["destruction_percentage"], "unresolved": decoded["unresolved_components"]})
    with domain_database(database_url) as ci, psycopg.connect(ci) as connection:
        _seed(connection, offense=specs)
        for lens in ("offense", "defense"):
            materialize_army_season(connection, MEASURE_SEASON, lens)
        connection.execute("CREATE TEMP TABLE history_v1 (LIKE army_season_summaries INCLUDING ALL)")
        connection.execute("ALTER TABLE history_v1 DROP COLUMN unit_usage")
        cursor = connection.execute("SELECT * FROM army_season_summaries WHERE category='troops'")
        columns = [column.name for column in cursor.description]
        for values in cursor.fetchall():
            template = dict(zip(columns, values))
            for category in sorted(CATEGORIES):
                record = dict(template)
                record.pop("unit_usage")
                record["category"] = category
                record["projection_version"] = "army-season-summary-v1"
                selection = ArmyAnalyticsSelection(record["lens"], MEASURE_SEASON, 1, 28, "all", category, "usage-rate")
                result = build_army_result([fact(UNKNOWN_CODE if i % 8 == 0 else f"h0p9e14_32u{1 + i % 5}x58s1x2i{1 + i % 4}x0", 3 if i % 2 else 2, 100 if i % 2 else 80) for i in range(224)] if record["lens"] == "offense" else [], selection)
                record["result_rows"] = Jsonb(result["rows"])
                record["army_states"] = Jsonb(record["army_states"])
                connection.execute(f"INSERT INTO history_v1 ({', '.join(record)}) VALUES ({', '.join(['%s'] * len(record))})", list(record.values()))
        measurements = {}
        for label, table in (("previous", "history_v1"), ("id_based", "army_season_summaries")):
            count, size = connection.execute(f"SELECT count(*), sum(pg_column_size(s)) FROM {table} s").fetchone()
            allocated = connection.execute("SELECT pg_total_relation_size(%s)", (table,)).fetchone()[0]
            measurements[label] = {"rows": count, "retained_row_bytes": size, "allocated_bytes": allocated}
        measurements["row_growth_bytes"] = measurements["id_based"]["retained_row_bytes"] - measurements["previous"]["retained_row_bytes"]
        print("HISTORY_STORAGE " + json.dumps(measurements, sort_keys=True))
        connection.commit()
        api = ApiDatabase(ci)
        try:
            result = api_analytics.get_army_season_summary(api, MEASURE_SEASON, "offense", "troops", "usage-rate")
            assert result["total_attacks"] == 224
            assert sum(row["usage_count"] for row in result["rows"]) == 196
            assert all(row["usage_denominator"] == 8 for row in result["rows"])
        finally:
            api.close()


def test_api_rename_changes_only_the_displayed_name(database_url, monkeypatch):
    with domain_database(database_url) as ci:
        api = ApiDatabase(ci)
        try:
            with psycopg.connect(ci) as connection:
                _seed(connection, offense=[{"stars": 3, "home_troops": [["troop:58", 5]]}])
                materialize_army_season(connection, MEASURE_SEASON, "offense")
            before = api_analytics.get_army_season_summary(api, MEASURE_SEASON, "offense", "troops", "usage-rate")
            monkeypatch.setitem(catalog._CATALOG_ENTRIES, "troop:58", {**catalog._CATALOG_ENTRIES["troop:58"], "name": "Renamed troop"})
            after = api_analytics.get_army_season_summary(api, MEASURE_SEASON, "offense", "troops", "usage-rate")
            before["rows"][0]["label"] = "Renamed troop"
            assert after == before
        finally:
            api.close()


def test_legacy_unknown_summary_reports_partial_id_evidence(database_url):
    with domain_database(database_url) as ci:
        api = ApiDatabase(ci)
        try:
            with psycopg.connect(ci) as connection:
                _seed(connection, offense=[{"stars": 3, "army_state": "partial", "home_troops": [["troop:58", 1]],
                    "unresolved": [{"numeric_id": 900, "quantity": 1, "section": "u", "origin": "home"}]}])
                materialize_army_season(connection, MEASURE_SEASON, "offense")
                connection.execute("UPDATE army_season_summaries SET unit_usage=NULL, projection_version='army-season-summary-v1'")
            result = api_analytics.get_army_season_summary(api, MEASURE_SEASON, "offense", "troops", "usage-rate")
            assert result is None
        finally:
            api.close()


def test_quantity_trophy_pages_survive_retirement_and_do_not_repeat_groups(database_url):
    # A category can exceed one API page without exceeding the retained JSON
    # limit. Every page stays below the API response limit.
    with domain_database(database_url) as ci:
        api = ApiDatabase(ci)
        try:
            with psycopg.connect(ci) as connection:
                _seed(connection, offense=[{"stars": 3, "home_troops": [["troop:58", 5]], "trophies": 5000 + i} for i in range(450)])
                materialize_army_season(connection, MEASURE_SEASON, "offense")
                connection.execute("DELETE FROM army_analytics_battle_facts")
            first = api_analytics.get_army_season_summary(api, MEASURE_SEASON, "offense", "troops", "usage-rate")
            second = api_analytics.get_army_season_summary(api, MEASURE_SEASON, "offense", "troops", "usage-rate", offset=first["pagination"]["next_offset"])
            last = api_analytics.get_army_season_summary(api, MEASURE_SEASON, "offense", "troops", "usage-rate", offset=second["pagination"]["next_offset"])
            assert [len(page["rows"]) for page in (first, second, last)] == [200, 200, 50]
            assert last["pagination"]["next_offset"] is None
            rows = first["rows"] + second["rows"] + last["rows"]
            assert {row["battle_trophies"] for row in rows} == set(range(5000, 5450))
            assert all(row["usage_count"] == row["usage_denominator"] == 1 for row in rows)
            assert all(len(json.dumps(page).encode()) < 1_048_576 for page in (first, second, last))
        finally:
            api.close()
