"""The per-Reset record against the 05:30 board target, written through the
worker's database role as the alert check writes it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

import psycopg
import test_alerts
from domain_test_support import as_api_role, domain_database
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from clashlens import alerts, reset_acceptance

runtime = test_alerts.runtime
RESET = datetime(2026, 10, 8, 5, tzinfo=UTC)


def _as_worker(connection_info: str) -> str:
    options = conninfo_to_dict(connection_info).get("options", "")
    return make_conninfo(
        connection_info, options=f"{options} -c role=clashlens_python_worker".strip()
    )


def _seed_reset(connection: psycopg.Connection, reset: datetime) -> list[int]:
    """Three members: two collected, one failed, one reading still processing."""
    players = [
        row[0]
        for row in connection.execute(
            "INSERT INTO players (normalized_tag) VALUES ('#2PP'), ('#8QQ'), ('#9RR')"
            " RETURNING id"
        ).fetchall()
    ]
    sweep_id = connection.execute(
        """
        INSERT INTO collector_reset_sweeps (boundary_at, member_ids, membership_captured_at)
        VALUES (%s, %s, %s) RETURNING id
        """,
        (reset, players, reset + timedelta(seconds=1)),
    ).fetchone()[0]
    for player, status, finished, observation in (
        (players[0], "complete", timedelta(minutes=5), 9001),
        (players[1], "complete", timedelta(minutes=9), 9002),
        (players[2], "failed", timedelta(minutes=8), None),
    ):
        connection.execute(
            """
            INSERT INTO collector_work (
                kind, lane, scope, player_id, normalized_tag, due_at, sweep_id,
                coalescing_key, status, completed_at, updated_at,
                profile_observation_id
            )
            SELECT 'reset_baseline', 'reset', 'player', id, normalized_tag, %s, %s,
                   'reset-test:' || id, %s,
                   CASE WHEN %s = 'complete' THEN %s::timestamptz END, %s, %s
            FROM players WHERE id = %s
            """,
            (reset, sweep_id, status, status, reset + finished, reset + finished,
             observation, player),
        )
    connection.execute(
        """
        INSERT INTO python_processing_jobs (
            work_type, deduplication_key, input_json, observation_id, status
        ) VALUES ('process_observation', 'reading-9001', '{}', 9001, 'complete'),
                 ('process_observation', 'reading-9002', '{}', 9002, 'pending')
        """
    )
    for player, state in ((players[0], "Complete"), (players[1], "Partial")):
        _day(connection, player, reset, state, version=1)
    for player, state in ((players[0], "provisional"), (players[1], "unresolved")):
        connection.execute(
            "INSERT INTO reset_boundary_settlements (player_id, boundary_at, state)"
            " VALUES (%s, %s, %s)",
            (player, reset, state),
        )
    return players


def _day(connection, player: int, reset: datetime, state: str, *, version: int) -> None:
    connection.execute(
        """
        INSERT INTO ranked_day_versions (
            player_id, ranked_day_start, ranked_day_end, official_season_id,
            season_day_number, season_anchor_rule_version,
            reconciliation_rule_version, input_hash, result_hash, version,
            state, confidence
        ) VALUES (%s, %s, %s, '2026-10', 3, 'test', 'test', repeat('a', 64),
                  repeat(%s, 64), %s, %s, 'confirmed')
        """,
        (player, reset - timedelta(days=1), reset, str(version), version, state),
    )


def _publish(connection, reset: datetime, players: list[int]) -> None:
    """The first frozen board: inputs frozen at 05:20, published at 05:24."""
    snapshot_id = connection.execute(
        """
        INSERT INTO leaderboard_snapshots (
            snapshot_kind, boundary_at, version, ordering_rule_version,
            freshness_rule_version, state, measured_coverage,
            stale_entry_count, published_at
        ) VALUES ('frozen', %s, 1, 'test', 'test', 'published', 1, 0, %s)
        RETURNING id
        """,
        (reset, reset + timedelta(minutes=24)),
    ).fetchone()[0]
    generation_id = connection.execute(
        """
        INSERT INTO boundary_publication_generations (
            boundary_at, generation, ordering_rule_version, freshness_rule_version,
            expected_population_count, expected_population_hash, target_at,
            snapshot_id, snapshot_state
        ) VALUES (%s, 1, 'test', 'test', 3, repeat('c', 64), %s, %s, 'published')
        RETURNING id
        """,
        (reset, reset + timedelta(minutes=5), snapshot_id),
    ).fetchone()[0]
    manifest_id = connection.execute(
        """
        INSERT INTO boundary_publication_manifests (
            generation_id, artifact_kind, rule_versions, digest, frozen_at
        ) VALUES (%s, 'snapshot', '{}', repeat('d', 64), %s) RETURNING id
        """,
        (generation_id, reset + timedelta(minutes=20)),
    ).fetchone()[0]
    connection.execute(
        "UPDATE boundary_publication_generations SET snapshot_manifest_id = %s"
        " WHERE id = %s",
        (manifest_id, generation_id),
    )
    for ordinal, (player, state) in enumerate(
        zip(players, ("Complete", "Partial", "Unavailable"), strict=True), start=1
    ):
        connection.execute(
            """
            INSERT INTO boundary_publication_manifest_rows (
                manifest_id, ordinal, player_id, ranked_day_version_id,
                classification, input_identity
            ) VALUES (%s, %s, %s, (SELECT max(id) FROM ranked_day_versions
                                   WHERE player_id = %s), %s, '{}')
            """,
            (manifest_id, ordinal, player, player, state),
        )


def test_the_reset_record_keeps_each_stage_time_and_the_day_results(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        with psycopg.connect(connection_info, autocommit=True) as owner:
            owner.execute("SET session_replication_role = replica")
            players = _seed_reset(owner, RESET)

        def refresh(minutes: float, readable: datetime | None = None) -> dict:
            with psycopg.connect(_as_worker(connection_info)) as connection:
                record = reset_acceptance.refresh(
                    connection,
                    readable_boundary=readable,
                    now=RESET + timedelta(minutes=minutes),
                )
            assert record is not None
            return record

        # 05:12: collection over, one reading not yet processed, no board.
        record = refresh(12)
        assert (record["captured_count"], record["collected_count"]) == (3, 2)
        assert record["not_collected_count"] == 1
        assert record["membership_captured_at"] == RESET + timedelta(seconds=1)
        assert record["collection_finished_at"] == RESET + timedelta(minutes=9)
        assert record["proof_processed_at"] is None
        assert record["inputs_frozen_at"] is record["readable_at"] is None

        with psycopg.connect(connection_info, autocommit=True) as owner:
            owner.execute("SET session_replication_role = replica")
            owner.execute(
                "UPDATE python_processing_jobs SET status = 'complete',"
                " completed_at = %s WHERE observation_id = 9002",
                (RESET + timedelta(minutes=15),),
            )
            _publish(owner, RESET, players)
        # 05:31: the alert check first reads the board back.
        record = refresh(31, readable=RESET)
        assert record["proof_processed_at"] == RESET + timedelta(minutes=15)
        assert record["inputs_frozen_at"] == RESET + timedelta(minutes=20)
        assert record["published_at"] == RESET + timedelta(minutes=24)
        assert record["readable_at"] == RESET + timedelta(minutes=31)
        assert record["board_inputs"] == {"Complete": 1, "Partial": 1, "Unavailable": 1}
        assert record["at_0600"] is None
        # Times are kept as first seen.
        assert refresh(40, readable=RESET)["readable_at"] == RESET + timedelta(minutes=31)

        record = refresh(61)
        assert record["at_0600"]["day_results"] == {
            "Complete": 1,
            "Partial": 1,
            "Missing": 1,
        }
        assert record["at_0600"]["settlement"] == {"provisional": 1, "unresolved": 1}
        assert record["at_next_reset"] is None

        # A later repair is counted just before the next Reset, not at 06:00.
        with psycopg.connect(connection_info, autocommit=True) as owner:
            owner.execute("SET session_replication_role = replica")
            _day(owner, players[1], RESET, "Complete", version=2)
        record = refresh(23 * 60 + 51)
        assert record["at_0600"]["day_results"]["Partial"] == 1
        assert record["at_next_reset"]["day_results"] == {"Complete": 2, "Missing": 1}


def test_the_alert_probe_prints_the_latest_resets_progress(
    database_url: str, tmp_path, monkeypatch, capsys
) -> None:
    reset = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    reset -= timedelta(hours=(reset.hour - 5) % 24)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        url_file = tmp_path / "database-url"
        url_file.write_text(_as_worker(connection_info))
        monkeypatch.setenv("CLASHLENS_DATABASE_URL_FILE", str(url_file))
        alerts.reset_probe("0")
        assert capsys.readouterr().out.split() == ["0"] * 5
        with psycopg.connect(connection_info, autocommit=True) as owner:
            owner.execute("SET session_replication_role = replica")
            _seed_reset(owner, reset)
        alerts.reset_probe(str(int(reset.timestamp())))
        boundary, captured, ended, frozen, readable = map(
            int, capsys.readouterr().out.split()
        )
        assert (boundary, captured, ended, frozen) == (int(reset.timestamp()), 3, 3, 0)
        assert readable >= boundary


def test_the_board_check_reads_the_board_as_the_website_does(
    runtime, database_url: str, tmp_path, monkeypatch, capsys
) -> None:
    from fastapi.testclient import TestClient

    from clashlens.api import create_app
    from clashlens.api_db import ApiDatabase

    with domain_database(database_url, include_coordinator=True) as connection_info:
        url_file = tmp_path / "database-url"
        url_file.write_text(as_api_role(connection_info))
        monkeypatch.setenv("CLASHLENS_DATABASE_URL_FILE", str(url_file))
        database = ApiDatabase(as_api_role(connection_info), max_size=2)
        app = create_app(
            database, keys={("typescript-website", "current"): b"a" * 32}, clock=lambda: runtime.now
        )
        client = TestClient(app, raise_server_exceptions=False)

        def request(url, *, headers=None, **_kwargs):
            parsed = urlsplit(url)
            response = client.get(f"{parsed.path}?{parsed.query}", headers=headers)
            if response.status_code == 404:
                raise alerts.urllib.error.HTTPError(url, 404, "Not Found", None, None)
            assert response.status_code == 200
            return response.content

        monkeypatch.setattr(alerts, "request", request)
        try:
            # No frozen board yet.
            alerts.publication_probe()
            assert capsys.readouterr().out.split()[1] == "0"
            with psycopg.connect(connection_info, autocommit=True) as owner:
                owner.execute("SET session_replication_role = replica")
                players = _seed_reset(owner, RESET)
                _publish(owner, RESET, players)
            alerts.publication_probe()
            assert int(capsys.readouterr().out.split()[1]) == int(RESET.timestamp())
        finally:
            client.close()
            database.close()

