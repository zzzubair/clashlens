"""Alerts player tracking behavior."""

import json
from datetime import UTC, datetime

import pytest
from alerts_support import (
    ROOT,
)

from clashlens import alerts


def test_failed_or_pending_checks_keep_the_confirmed_profile_time(
    runtime, database_url, archive_server, tmp_path, monkeypatch, capsys
):
    import hashlib
    from datetime import timedelta

    import psycopg
    from domain_test_support import (
        PROFILE_FIXTURE,
        _processor,
        as_api_role,
        decide_leagues,
        domain_database,
        store_observation,
    )
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from clashlens import api_leaderboard, api_players
    from clashlens.api_db import ApiDatabase
    from clashlens.collector_db import CollectorDatabase, ResponseHandoff
    from clashlens.response_fields import content_fingerprint

    accepted_at = datetime(2026, 8, 6, 6, tzinfo=UTC)
    confirmed_at = accepted_at + timedelta(hours=2)
    body = PROFILE_FIXTURE.read_bytes()
    with domain_database(database_url) as connection_info:
        _, job = store_observation(
            connection_info, archive_server, occurrence_key="confirmed-initial",
            endpoint="profile", body=body, observed_at=accepted_at,
            normalized_tag="#2PP",
            parser_version="supercell-profile-parser-v3",
        )
        options = conninfo_to_dict(connection_info)["options"]
        database, processor = _processor(
            make_conninfo(connection_info, options=options + " -c role=clashlens_python_worker"),
            archive_server,
        )
        collector = CollectorDatabase(make_conninfo(
            connection_info, options=options + " -c role=clashlens_collector"
        ))
        api = ApiDatabase(as_api_role(connection_info))
        url_file = tmp_path / "database-url"
        url_file.write_text(as_api_role(connection_info))
        monkeypatch.setenv("CLASHLENS_DATABASE_URL_FILE", str(url_file))

        def check(payload, at, occurrence, status=200):
            digest = hashlib.sha256(payload).hexdigest()
            with psycopg.connect(connection_info) as connection:
                player_id = connection.execute(
                    "SELECT id FROM players WHERE normalized_tag = '#2PP'"
                ).fetchone()[0]
            return collector.record_response(ResponseHandoff(
                occurrence_key=occurrence, scope="player", identity_key="#2PP",
                endpoint="profile", player_id=player_id, normalized_tag="#2PP",
                request_started_at=at - timedelta(seconds=1), response_completed_at=at,
                http_status=status, response_hash=digest,
                content_fingerprint=content_fingerprint(
                    "profile", payload, http_status=status, response_hash=digest
                ),
                byte_size=len(payload), spool_key=f"sha256/{digest[:2]}/{digest}",
                collector_version="confirmation-test", key_label="regular-a",
                evidence_headers={"content-type": "application/json"},
            ))

        def assert_time(now, expected, trophies):
            with api.pool.connection() as page_connection:
                page = api_players.read_player_page(page_connection, "#2PP", now=now, freshness_seconds=900)
            board = api_leaderboard.get_live_leaderboard(api, limit=1, now=now)
            [entry] = board["entries"]
            assert page["observed_at"] == entry["observed_at"] == expected.isoformat()
            assert page["trophies"] == entry["trophies"] == trophies
            assert page["age_seconds"] == entry["age_seconds"] == int((now - expected).total_seconds())
            assert board["source_observations"]["stale_count"] == 0
            capsys.readouterr()
            alerts.leaderboard_freshness_probe(now)
            runtime.leaderboard = capsys.readouterr().out.strip()
            runtime.now = now.timestamp()
            assert runtime.run() == 0
            assert not runtime.posts

        try:
            capsys.readouterr()
            alerts.leaderboard_freshness_probe(accepted_at)
            runtime.leaderboard = capsys.readouterr().out.strip()
            assert runtime.leaderboard == "0 0 0"
            runtime.now = accepted_at.timestamp()
            assert runtime.run() == 0
            assert not runtime.posts
            assert processor.process_job(job, owner="confirmed-initial") is not None
            decide_leagues(database, accepted_at)
            trophies = json.loads(body)["trophies"]
            check(body, accepted_at, "confirmed-initial")
            assert check(body, confirmed_at, "confirmed-unchanged").changed is False
            check(b'{"reason":"inMaintenance"}', confirmed_at + timedelta(minutes=5),
                  "confirmed-failed", status=503)
            assert_time(confirmed_at + timedelta(minutes=6), confirmed_at, trophies)
            older = json.loads(body)
            older["trophies"] += 50
            older_body = json.dumps(older).encode()
            older_at = confirmed_at - timedelta(hours=1)
            _, older_job = store_observation(
                connection_info, archive_server, occurrence_key="confirmed-older",
                endpoint="profile", body=older_body, observed_at=older_at,
                normalized_tag="#2PP",
                parser_version="supercell-profile-parser-v3",
            )
            check(older_body, older_at, "confirmed-older")
            assert processor.process_job(older_job, owner="confirmed-older") is not None
            assert_time(confirmed_at + timedelta(minutes=6), confirmed_at, trophies)
            changed = json.loads(body)
            changed["trophies"] += 30
            changed_body = json.dumps(changed).encode()
            changed_at = confirmed_at + timedelta(minutes=7)
            _, changed_job = store_observation(
                connection_info, archive_server, occurrence_key="confirmed-changed",
                endpoint="profile", body=changed_body, observed_at=changed_at,
                normalized_tag="#2PP",
                parser_version="supercell-profile-parser-v3",
            )
            check(changed_body, changed_at, "confirmed-changed")
            assert_time(changed_at + timedelta(minutes=1), confirmed_at, trophies)
            assert processor.process_job(changed_job, owner="confirmed-changed") is not None
            assert_time(changed_at + timedelta(minutes=1), changed_at, trophies + 30)
            check(changed_body, changed_at + timedelta(minutes=2), "confirmed-new-unchanged")
            assert_time(changed_at + timedelta(minutes=3), changed_at + timedelta(minutes=2), trophies + 30)
        finally:
            api.close()
            collector.close()
            database.close()


@pytest.mark.parametrize("later_status", [None, 503])
@pytest.mark.parametrize("upgrade", [False, True])
def test_not_found_player_leaves_the_leaderboard_and_alert_until_found_again(
    runtime, database_url, archive_server, tmp_path, monkeypatch, capsys,
    later_status, upgrade,
):
    import hashlib
    from datetime import timedelta

    import psycopg
    from domain_test_support import (
        PROFILE_FIXTURE,
        _processor,
        as_api_role,
        decide_leagues,
        domain_database,
        store_observation,
    )
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from clashlens import api_leaderboard
    from clashlens.api_db import ApiDatabase
    from clashlens.collector_db import (
        CollectorDatabase,
        ResponseHandoff,
        TransportFailure,
    )
    from clashlens.response_fields import content_fingerprint

    accepted_at = datetime(2026, 8, 6, 6, tzinfo=UTC)
    body = PROFILE_FIXTURE.read_bytes()
    not_found = b'{"reason":"notFound","message":"Not found"}'
    with domain_database(database_url) as connection_info:
        _, job = store_observation(
            connection_info, archive_server, occurrence_key="nf-initial",
            endpoint="profile", body=body, observed_at=accepted_at,
            normalized_tag="#2PP", parser_version="supercell-profile-parser-v3",
        )
        options = conninfo_to_dict(connection_info)["options"]
        database, processor = _processor(
            make_conninfo(connection_info, options=options + " -c role=clashlens_python_worker"),
            archive_server,
        )
        collector = CollectorDatabase(make_conninfo(
            connection_info, options=options + " -c role=clashlens_collector"
        ))
        api = ApiDatabase(as_api_role(connection_info))
        url_file = tmp_path / "database-url"
        url_file.write_text(as_api_role(connection_info))
        monkeypatch.setenv("CLASHLENS_DATABASE_URL_FILE", str(url_file))
        with psycopg.connect(connection_info) as connection:
            player_id = connection.execute(
                "SELECT id FROM players WHERE normalized_tag = '#2PP'"
            ).fetchone()[0]

        def check(payload, at, occurrence, status=200):
            digest = hashlib.sha256(payload).hexdigest()
            return collector.record_response(ResponseHandoff(
                occurrence_key=occurrence, scope="player", identity_key="#2PP",
                endpoint="profile", player_id=player_id, normalized_tag="#2PP",
                request_started_at=at - timedelta(seconds=1), response_completed_at=at,
                http_status=status, response_hash=digest,
                content_fingerprint=content_fingerprint(
                    "profile", payload, http_status=status, response_hash=digest
                ),
                byte_size=len(payload), spool_key=f"sha256/{digest[:2]}/{digest}",
                collector_version="not-found-test", key_label="regular-a",
                evidence_headers={"content-type": "application/json"},
            ))

        def alert_at(now):
            board = api_leaderboard.get_live_leaderboard(api, limit=1, now=now)
            capsys.readouterr()
            alerts.leaderboard_freshness_probe(now)
            runtime.leaderboard = capsys.readouterr().out.strip()
            runtime.now = now.timestamp()
            assert runtime.run() == 0
            return board, runtime.leaderboard

        try:
            assert processor.process_job(job, owner="nf-initial") is not None
            decide_leagues(database, accepted_at)
            check(body, accepted_at, "nf-initial")
            # A server error and a timeout leave the player listed and stale.
            check(b'{"reason":"inMaintenance"}', accepted_at + timedelta(minutes=5),
                  "nf-failed", status=503)
            collector.record_transport_failure(TransportFailure(
                occurrence_key="nf-timeout", scope="player", identity_key="#2PP",
                endpoint="profile", player_id=player_id, normalized_tag="#2PP",
                request_started_at=accepted_at + timedelta(minutes=10),
                failed_at=accepted_at + timedelta(minutes=10, seconds=30),
                failure_category="timeout", retry_state="next_pass",
                key_label="regular-a",
            ))
            board, counts = alert_at(accepted_at + timedelta(hours=3))
            assert [entry["tag"] for entry in board["entries"]] == ["#2PP"]
            assert counts == "1 1 10800"
            assert len(runtime.posts) == 1
            # Not found, then the same answer again: hidden, and the alert clears.
            check(not_found, accepted_at + timedelta(hours=3), "nf-404", status=404)
            assert check(
                not_found, accepted_at + timedelta(hours=3, minutes=5), "nf-404-again",
                status=404,
            ).changed is False
            board, counts = alert_at(accepted_at + timedelta(hours=3, minutes=6))
            assert board["entries"] == [] and board["total_entries"] == 0
            assert board["tracked_population"] == 1
            assert counts == "0 0 0"
            assert len(runtime.posts) == 2
            assert "recovered" in runtime.posts[-1]["content"]
            if later_status is not None:
                check(b'{"reason":"inMaintenance"}',
                      accepted_at + timedelta(hours=3, minutes=7),
                      "nf-still-failed", status=later_status)
            collector.record_transport_failure(TransportFailure(
                occurrence_key="nf-still-timeout", scope="player", identity_key="#2PP",
                endpoint="profile", player_id=player_id, normalized_tag="#2PP",
                request_started_at=accepted_at + timedelta(hours=3, minutes=8),
                failed_at=accepted_at + timedelta(hours=3, minutes=8, seconds=30),
                failure_category="timeout", retry_state="next_pass",
                key_label="regular-a",
            ))
            if upgrade:
                with psycopg.connect(connection_info) as connection:
                    connection.execute(
                        "ALTER TABLE collector_response_state DROP COLUMN last_not_found_at"
                    )
                    connection.commit()
                    connection.execute(
                        (ROOT / "deploy/migrations/0043_api_profile_not_found_read.sql").read_text()
                    )
            board, counts = alert_at(accepted_at + timedelta(hours=3, minutes=9))
            assert board["entries"] == [] and board["total_entries"] == 0
            assert board["tracked_population"] == 1
            assert counts == "0 0 0"
            assert len(runtime.posts) == 2
            # The next successful check brings the player straight back, fresh.
            found_at = accepted_at + timedelta(hours=3, minutes=10)
            check(body, found_at, "nf-found")
            board, counts = alert_at(found_at + timedelta(minutes=1))
            [entry] = board["entries"]
            assert entry["tag"] == "#2PP"
            assert entry["observed_at"] == found_at.isoformat()
            assert counts == "0 1 60"
            assert len(runtime.posts) == 2
        finally:
            api.close()
            collector.close()
            database.close()
