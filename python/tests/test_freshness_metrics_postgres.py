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

from clashlens import api_leaderboard, api_players
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
                seed_check(connection, tag, "profile", older)
                # Checks skip the battle log on purpose, so its age never counts.
                seed_check(connection, tag, "battle_log", now - timedelta(days=1))
            for tag, active in (
                ("#NONE", True),
                ("#HALF", True),
                ("#FAIL", True),
                ("#OFF", False),
                ("#NOTFOUND", True),
                ("#NEVERFOUND", True),
            ):
                connection.execute(
                    "INSERT INTO players (normalized_tag, active) VALUES (%s, %s)",
                    (tag, active),
                )
            seed_check(connection, "#HALF", "battle_log", now)
            seed_check(connection, "#FAIL", "profile", None)
            for endpoint in ("profile", "battle_log"):
                seed_check(connection, "#OFF", endpoint, now - timedelta(days=10))
                seed_check(
                    connection,
                    "#NOTFOUND",
                    endpoint,
                    now - timedelta(days=1),
                    not_found=now if endpoint == "profile" else None,
                )
            seed_check(connection, "#NEVERFOUND", "profile", None, not_found=now)
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
            assert metrics[prefix + "active_players"] == 25
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


@pytest.mark.parametrize(
    ("endpoint", "success_seconds", "not_found_seconds", "visible"),
    [
        ("profile", None, 0, False),
        ("profile", -120, 0, False),
        ("profile", 0, 0, True),
        ("profile", 0, -120, True),
        ("battle_log", -120, 0, True),
    ],
)
def test_collector_check_age_not_found_membership(
    database_url,
    endpoint,
    success_seconds,
    not_found_seconds,
    visible,
):
    now = datetime.now(UTC)
    with domain_database(database_url) as info:
        with psycopg.connect(info) as connection:
            connection.execute(
                "INSERT INTO players (normalized_tag, active) VALUES ('#P', true)"
            )
            for checked_endpoint in ("profile", "battle_log"):
                at = now - timedelta(seconds=180)
                not_found = None
                if checked_endpoint == endpoint:
                    at = (
                        None
                        if success_seconds is None
                        else now + timedelta(seconds=success_seconds)
                    )
                    not_found = now + timedelta(seconds=not_found_seconds)
                seed_check(connection, "#P", checked_endpoint, at, not_found=not_found)
        database = CollectorDatabase(info)
        try:
            measured = database.health_metrics()
            assert measured["active_players"] == 1
            assert measured["check_age_sample_players"] == int(visible)
            assert measured["check_age_missing_players"] == 0
            for name in ("p50", "p95", "max"):
                if visible:
                    elapsed = (
                        measured["metrics_sample_timestamp_seconds"] - now.timestamp()
                    )
                    profile_age = 180 if endpoint == "battle_log" else -success_seconds
                    assert measured[f"check_age_{name}_seconds"] == pytest.approx(
                        profile_age + elapsed, abs=0.001
                    )
                else:
                    assert f"check_age_{name}_seconds" not in measured
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


def test_finished_player_checked_every_8_minutes_stays_fresh(database_url):
    from itertools import pairwise

    from clashlens.battle_log_schedule import FINISHED_RECHECK
    from clashlens.collector_db import REVISIT_INTERVAL, ResponseHandoff

    start = datetime.fromtimestamp(NOW, UTC)
    with domain_database(database_url) as info:
        owner = ApiDatabase(info)
        collector = CollectorDatabase(info)
        board_reader = ApiDatabase(as_api_role(info))
        try:
            for tag in ("#FINISHED", "#PLAYING"):
                seed_profile(owner, tag, 6000, observed_at=start)
            with psycopg.connect(info) as connection:
                # The worker stores this after processing the seeded profile.
                connection.execute(
                    "UPDATE players SET current_profile_fingerprint = %s",
                    ("b" * 64,),
                )
                ids = dict(connection.execute("SELECT normalized_tag, id FROM players"))

            def check(tag: str, at: datetime) -> None:
                # The collector saves an unchanged profile, as on every check.
                collector.record_response(
                    ResponseHandoff(
                        occurrence_key=f"{tag}-{at.isoformat()}",
                        scope="player",
                        identity_key=tag,
                        endpoint="profile",
                        player_id=ids[tag],
                        normalized_tag=tag,
                        request_started_at=at - timedelta(seconds=1),
                        response_completed_at=at,
                        http_status=200,
                        response_hash="a" * 64,
                        content_fingerprint="b" * 64,
                        byte_size=4,
                        spool_key="sha256/aa/" + "a" * 64,
                        collector_version="test",
                        key_label="regular-a",
                        evidence_headers={},
                    )
                )

            def stale_count(at: datetime) -> int:
                # The Live Leaderboard's count, which the stale alert also reads.
                board = api_leaderboard.get_live_leaderboard(
                    board_reader, limit=2, now=at
                )
                assert board["total_entries"] == 2
                return board["source_observations"]["stale_count"]

            last = start + FINISHED_RECHECK * 7
            missed = last + timedelta(minutes=10, seconds=1)
            checks = sorted(
                [(start + FINISHED_RECHECK * n, "#FINISHED") for n in range(8)]
                + [
                    (start + REVISIT_INTERVAL * n, "#PLAYING")
                    for n in range(int((missed - start) / REVISIT_INTERVAL) + 1)
                ]
            )
            for (at, tag), (following, _) in pairwise(checks):
                check(tag, at)
                # Just before the next check of either player, neither is stale.
                if following <= last + FINISHED_RECHECK:
                    assert stale_count(following - timedelta(seconds=1)) == 0
            assert stale_count(last + FINISHED_RECHECK) == 0
            # Without its next check the finished player goes stale at 10 minutes.
            assert stale_count(missed) == 1
        finally:
            board_reader.close()
            collector.close()
            owner.close()


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
            # Only the profile check counts; a later battle log changes nothing.
            for endpoint, expected_age in (("profile", 20), ("battle_log", 20)):
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
            # A profile 404 hides the player until a success; a server error
            # in between must not restore them to the freshness population.
            for seconds, status in enumerate((404, 500, 200), start=1):
                database.record_response(
                    replace(
                        original,
                        occurrence_key=f"profile-{status}",
                        request_started_at=now + timedelta(seconds=seconds - 1),
                        response_completed_at=now + timedelta(seconds=seconds),
                        http_status=status,
                    )
                )
                measured = database.health_metrics()
                assert measured["check_age_sample_players"] == int(status == 200)
                assert measured["check_age_missing_players"] == 0
                if status == 200:
                    elapsed = (
                        measured["metrics_sample_timestamp_seconds"] - now.timestamp()
                    )
                    assert measured["check_age_max_seconds"] == pytest.approx(
                        max(0.0, elapsed - seconds), abs=0.001
                    )
                else:
                    assert all(
                        f"check_age_{name}_seconds" not in measured
                        for name in ("p50", "p95", "max")
                    )
        finally:
            database.close()


def test_update_status_reports_delayed_collection_and_processing(database_url):
    from dataclasses import replace

    from clashlens.collector_db import ResponseHandoff

    now = datetime.fromtimestamp(NOW, UTC)
    with domain_database(database_url) as info:
        with psycopg.connect(info) as connection:
            player_id = connection.execute(
                "INSERT INTO players (normalized_tag, active) VALUES ('#2PP', true) RETURNING id"
            ).fetchone()[0]
        collector = CollectorDatabase(info)
        database = ApiDatabase(as_api_role(info))
        original = ResponseHandoff(
            occurrence_key="old-profile",
            scope="player",
            identity_key="#2PP",
            endpoint="profile",
            player_id=player_id,
            normalized_tag="#2PP",
            request_started_at=now - timedelta(minutes=21),
            response_completed_at=now - timedelta(minutes=20),
            http_status=200,
            response_hash="a" * 64,
            content_fingerprint="b" * 64,
            byte_size=4,
            spool_key="sha256/aa/" + "a" * 64,
            collector_version="test",
            key_label="regular-a",
            evidence_headers={},
        )

        client = TestClient(
            create_app(
                database=database,
                keys={("typescript-website", "current"): KEY},
                clock=lambda: NOW,
            )
        )

        def status():
            response = client.get("/v1/status", headers=_signed_headers("/v1/status"))
            assert response.status_code == 200
            body = response.json()
            return body["collection_delayed"], body["processing_delayed"]

        def record(handoff):
            collector.record_response(handoff)
            # Saved when the answer arrived, as in production.
            with psycopg.connect(info) as connection:
                connection.execute(
                    "UPDATE python_processing_jobs SET created_at = %s"
                    " WHERE deduplication_key = %s",
                    (
                        handoff.response_completed_at,
                        "process-response:" + handoff.occurrence_key,
                    ),
                )

        def set_old_job(status, due_at):
            with psycopg.connect(info) as connection:
                connection.execute(
                    "UPDATE python_processing_jobs SET status = %s, due_at = %s"
                    " WHERE created_at < %s",
                    (status, due_at, now - timedelta(minutes=10)),
                )

        try:
            with client:
                # Nothing collected yet is not a delay.
                assert status() == (False, False)
                # One answer 20 minutes ago, still waiting to be processed.
                record(original)
                assert status() == (True, True)
                # A fresh answer clears collection; the old saved one still waits.
                record(
                    replace(
                        original,
                        occurrence_key="new-profile",
                        request_started_at=now - timedelta(minutes=2),
                        response_completed_at=now - timedelta(minutes=1),
                        response_hash="c" * 64,
                        content_fingerprint="d" * 64,
                        spool_key="sha256/cc/" + "c" * 64,
                    )
                )
                assert status() == (False, True)
                # Waiting for storage, or retried a minute ago, still counts from
                # when the data was saved 20 minutes ago.
                set_old_job("waiting_dependency", now + timedelta(minutes=5))
                assert status() == (False, True)
                set_old_job("waiting_retry", now - timedelta(minutes=1))
                assert status() == (False, True)
                body = client.get(
                    "/v1/status", headers=_signed_headers("/v1/status")
                ).json()
                assert (
                    body["oldest_waiting_saved_at"]
                    == (now - timedelta(minutes=20)).isoformat()
                )
                set_old_job("complete", now)
                assert status() == (False, False)
        finally:
            collector.close()


def test_player_page_reports_battle_history_publication_separately(database_url):
    now = datetime.fromtimestamp(NOW, UTC)
    with domain_database(database_url) as info:
        owner = ApiDatabase(info)
        database = ApiDatabase(as_api_role(info))
        try:
            seed_profile(owner, "#2PP", 6000, observed_at=now - timedelta(minutes=1))
            seed_profile(owner, "#9QQ", 6000, observed_at=now - timedelta(minutes=1))
            published_at = now - timedelta(hours=2)
            with psycopg.connect(info) as connection:
                connection.execute(
                    "UPDATE api_player_daily_logs SET published_at = %s"
                    " WHERE player_id = (SELECT id FROM players WHERE normalized_tag = '#2PP')",
                    (published_at,),
                )
                connection.execute(
                    "DELETE FROM api_player_daily_logs"
                    " WHERE player_id = (SELECT id FROM players WHERE normalized_tag = '#9QQ')"
                )
                # A successful battle log request alone does not move the time.
                seed_check(connection, "#2PP", "battle_log", now - timedelta(minutes=1))

            def battle_history_updated_at(tag):
                page = api_players.get_player_page(
                    database, tag, now=now, freshness_seconds=900
                )
                return page["battle_history_updated_at"]

            assert battle_history_updated_at("#2PP") == published_at.isoformat()
            # Nothing published stays unknown, not "just now".
            assert battle_history_updated_at("#9QQ") is None
        finally:
            database.close()
            owner.close()
