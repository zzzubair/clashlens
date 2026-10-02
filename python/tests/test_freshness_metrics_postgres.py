from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from domain_test_support import as_api_role, domain_database
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from test_api_db_public_ops import seed_profile
from test_api_security import KEY, NOW, _signed_headers
from test_collector import _Client, _collector, _Spool

from clashlens import api_leaderboard
from clashlens.api import create_app
from clashlens.api_db import ApiDatabase
from clashlens.collector_db import CollectorDatabase


def seed_check(connection, tag, endpoint, at, *, not_found=None):
    connection.execute(
        """
        INSERT INTO collector_response_state (
            scope, identity_key, endpoint, player_id, normalized_tag,
            last_response_hash, last_content_fingerprint,
            last_occurrence_key, last_applied_occurrence_key,
            last_seen_at, last_success_at, last_not_found_at
        ) SELECT 'player', normalized_tag, %s, id, normalized_tag,
                 repeat('a', 64), repeat('a', 64), normalized_tag || %s, 'seed',
                 COALESCE(%s, clock_timestamp()), %s, %s
          FROM players WHERE normalized_tag = %s
        """,
        (endpoint, endpoint, at, at, not_found, tag),
    )


def test_collector_exports_population_check_ages_with_missing_history(database_url):
    now = datetime.now(UTC)
    with domain_database(database_url) as info:
        with psycopg.connect(info) as connection:
            # 20 complete players: one future clock, then ages 10, 20, ... 190.
            for index in range(20):
                tag = f"#P{index}"
                connection.execute(
                    "INSERT INTO players (normalized_tag, active) VALUES (%s, true)",
                    (tag,),
                )
                older = (
                    now + timedelta(seconds=60)
                    if index == 0
                    else now - timedelta(seconds=index * 10)
                )
                # Alternate the older endpoint so neither can mask the other.
                for endpoint in ("profile", "battle_log"):
                    at = (
                        older
                        if endpoint == ("profile" if index % 2 else "battle_log")
                        else now + timedelta(seconds=120)
                    )
                    seed_check(connection, tag, endpoint, at)
            for tag, active in (
                ("#NONE", True),
                ("#HALF", True),
                ("#FAIL", True),
                ("#OFF", False),
            ):
                connection.execute(
                    "INSERT INTO players (normalized_tag, active) VALUES (%s, %s)",
                    (tag, active),
                )
            seed_check(connection, "#HALF", "profile", now)
            seed_check(connection, "#FAIL", "profile", None)
            for endpoint in ("profile", "battle_log"):
                seed_check(connection, "#OFF", endpoint, now - timedelta(days=10))
        options = conninfo_to_dict(info)["options"]
        database = CollectorDatabase(
            make_conninfo(info, options=options + " -c role=clashlens_collector")
        )
        try:
            spool = _Spool()
            collector = _collector(spool, database, _Client(spool))
            status, _, body = asyncio.run(collector.health_response("/metrics"))
            assert status == 200
            metrics = {
                line.split()[0]: float(line.split()[1])
                for line in body.decode().splitlines()
            }
            prefix = "clashlens_collector_"
            assert metrics[prefix + "active_players"] == 23
            assert metrics[prefix + "check_age_sample_players"] == 20
            assert metrics[prefix + "check_age_missing_players"] == 3
            elapsed = (
                metrics[prefix + "metrics_sample_timestamp_seconds"] - now.timestamp()
            )
            for name, age in (("p50", 90), ("p95", 180), ("max", 190)):
                assert metrics[prefix + f"check_age_{name}_seconds"] == pytest.approx(
                    age + elapsed, abs=0.001
                )
        finally:
            database.close()


def test_empty_collector_omits_unknown_percentiles(database_url):
    with domain_database(database_url) as info:
        database = CollectorDatabase(info)
        try:
            for populated in (False, True):
                if populated:
                    with psycopg.connect(info) as connection:
                        connection.execute(
                            "INSERT INTO players (normalized_tag, active) VALUES ('#P', true)"
                        )
                metrics = database.health_metrics()
                assert metrics["check_age_sample_players"] == 0
                assert metrics["check_age_missing_players"] == int(populated)
                assert all(
                    f"check_age_{name}_seconds" not in metrics
                    for name in ("p50", "p95", "max")
                )
            with psycopg.connect(info) as connection:
                for endpoint in ("profile", "battle_log"):
                    seed_check(
                        connection,
                        "#P",
                        endpoint,
                        datetime.now(UTC) + timedelta(days=1),
                    )
            metrics = database.health_metrics()
            assert metrics["check_age_sample_players"] == 1
            assert metrics["check_age_missing_players"] == 0
            assert all(
                metrics[f"check_age_{name}_seconds"] == 0
                for name in ("p50", "p95", "max")
            )
        finally:
            database.close()


def test_operator_freshness_matches_live_membership_and_600_second_boundary(
    database_url,
):
    now = datetime.fromtimestamp(NOW, UTC)
    with domain_database(database_url) as info:
        owner = ApiDatabase(info)
        try:
            ages = [0, 100, 200, 300, 600, 600.5, 900]
            for index, age in enumerate(ages):
                seed_profile(
                    owner, f"#P{index}", 6000, observed_at=now - timedelta(seconds=age)
                )
            for tag in ("#INACTIVE", "#REJECTED", "#NOTFOUND"):
                seed_profile(owner, tag, 6000, observed_at=now - timedelta(days=1))
            with psycopg.connect(info) as connection:
                connection.execute(
                    "UPDATE players SET active = false WHERE normalized_tag = '#INACTIVE'"
                )
                connection.execute(
                    "UPDATE player_profile_versions SET source_contract_state = 'unsupported' WHERE normalized_tag = '#REJECTED'"
                )
                seed_check(
                    connection,
                    "#NOTFOUND",
                    "profile",
                    now - timedelta(days=1),
                    not_found=now,
                )
                # An unchanged confirmation refreshes the oldest profile.
                connection.execute(
                    "UPDATE players SET current_profile_confirmed_at = %s WHERE normalized_tag = '#P6'",
                    (now + timedelta(seconds=10),),
                )
                connection.execute(
                    "INSERT INTO players (normalized_tag, active) VALUES ('#NOPROFILE', true)"
                )
            database = ApiDatabase(as_api_role(info))
            try:
                board = api_leaderboard.get_live_leaderboard(database, limit=1, now=now)
                with TestClient(
                    create_app(
                        database=database,
                        keys={("typescript-website", "current"): KEY},
                        clock=lambda: NOW,
                    )
                ) as client:
                    response = client.get(
                        "/operatorz", headers=_signed_headers("/operatorz")
                    )
                assert response.status_code == 200
                measured = response.json()["live_leaderboard"]
                assert measured == {
                    "sample_timestamp_seconds": float(NOW),
                    "entries": board["total_entries"],
                    "age_missing_entries": 0,
                    "age_p50_seconds": 200,
                    "age_p95_seconds": 600.5,
                    "age_max_seconds": 600.5,
                    "older_than_10_minutes": board["source_observations"][
                        "stale_count"
                    ],
                }
                assert measured["entries"] == 7
                assert measured["older_than_10_minutes"] == 1
            finally:
                database.close()
        finally:
            owner.close()


def test_empty_leaderboard_reports_no_samples(database_url):
    with domain_database(database_url) as info:
        database = ApiDatabase(as_api_role(info))
        try:
            metrics = api_leaderboard.live_freshness_metrics(
                database, now=datetime.now(UTC)
            )
            assert metrics["entries"] == metrics["older_than_10_minutes"] == 0
            assert metrics["age_missing_entries"] == 0
            assert all(
                metrics[f"age_{name}_seconds"] is None for name in ("p50", "p95", "max")
            )
        finally:
            database.close()


def test_unchanged_success_advances_check_age_but_failure_does_not(database_url):
    from dataclasses import replace

    from clashlens.collector_db import ResponseHandoff

    with domain_database(database_url) as info:
        with psycopg.connect(info) as connection:
            player_id = connection.execute(
                "INSERT INTO players (normalized_tag, active) VALUES ('#2PP', true) RETURNING id"
            ).fetchone()[0]
        database = CollectorDatabase(info)
        now = datetime.now(UTC)
        original = ResponseHandoff(
            occurrence_key="initial-profile",
            scope="player",
            identity_key="#2PP",
            endpoint="profile",
            player_id=player_id,
            normalized_tag="#2PP",
            request_started_at=now - timedelta(seconds=121),
            response_completed_at=now - timedelta(seconds=120),
            http_status=200,
            response_hash="a" * 64,
            content_fingerprint="b" * 64,
            byte_size=4,
            spool_key="sha256/aa/" + "a" * 64,
            collector_version="test",
            key_label="regular-a",
            evidence_headers={},
        )
        try:
            for endpoint in ("profile", "battle_log"):
                database.record_response(
                    replace(
                        original,
                        endpoint=endpoint,
                        occurrence_key=f"initial-{endpoint}",
                    )
                )
            for endpoint, expected_age in (("profile", 120), ("battle_log", 20)):
                result = database.record_response(
                    replace(
                        original,
                        endpoint=endpoint,
                        occurrence_key=f"unchanged-{endpoint}",
                        request_started_at=now - timedelta(seconds=21),
                        response_completed_at=now - timedelta(seconds=20),
                    )
                )
                assert result.changed is False
                measured = database.health_metrics()
                elapsed = measured["metrics_sample_timestamp_seconds"] - now.timestamp()
                assert measured["check_age_max_seconds"] == pytest.approx(
                    expected_age + elapsed, abs=0.001
                )
            for endpoint in ("profile", "battle_log"):
                database.record_response(
                    replace(
                        original,
                        endpoint=endpoint,
                        occurrence_key=f"failed-{endpoint}",
                        request_started_at=now - timedelta(seconds=1),
                        response_completed_at=now,
                        http_status=403,
                        response_hash="c" * 64,
                        content_fingerprint="d" * 64,
                        spool_key="sha256/cc/" + "c" * 64,
                    )
                )
            measured = database.health_metrics()
            elapsed = measured["metrics_sample_timestamp_seconds"] - now.timestamp()
            assert measured["check_age_max_seconds"] == pytest.approx(
                20 + elapsed, abs=0.001
            )
        finally:
            database.close()
