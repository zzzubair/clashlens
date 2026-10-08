"""The per-Reset record against the 05:30 board target, written through the
worker's database role as the alert check writes it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import urlsplit

import psycopg
import test_alerts
from domain_test_support import as_api_role, domain_database
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from clashlens import alerts, reset_acceptance
from clashlens.collector_db import CollectorDatabase

runtime = test_alerts.runtime
RESET = datetime(2026, 10, 8, 5, tzinfo=UTC)


def _as_worker(connection_info: str) -> str:
    options = conninfo_to_dict(connection_info).get("options", "")
    return make_conninfo(
        connection_info, options=f"{options} -c role=clashlens_python_worker".strip()
    )


def _seed_reset(connection: psycopg.Connection, reset: datetime) -> list[int]:
    """Three members: two collected and one failed after saving its profile;
    two readings still processing, one of them the failed member's."""
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
        (players[2], "failed", timedelta(minutes=8), 9003),
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
                 ('process_observation', 'reading-9002', '{}', 9002, 'pending'),
                 ('process_observation', 'reading-9003', '{}', 9003, 'pending')
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


def test_the_reset_record_keeps_each_stage_time_and_the_board_counts(
    database_url: str,
) -> None:
    with domain_database(database_url, include_coordinator=True) as connection_info:
        with psycopg.connect(connection_info, autocommit=True) as owner:
            owner.execute("SET session_replication_role = replica")
            players = _seed_reset(owner, RESET)

        def refresh(read_minutes: float | None = None) -> dict:
            read = None if read_minutes is None else RESET + timedelta(minutes=read_minutes)
            with psycopg.connect(_as_worker(connection_info)) as connection:
                record = reset_acceptance.refresh(
                    connection,
                    readable_boundary=None if read is None else RESET,
                    readable_at=read,
                )
            assert record is not None
            return record

        def finish(observation: int, minutes: int) -> None:
            with psycopg.connect(connection_info, autocommit=True) as owner:
                owner.execute("SET session_replication_role = replica")
                owner.execute(
                    "UPDATE python_processing_jobs SET status = 'complete',"
                    " completed_at = %s WHERE observation_id = %s",
                    (RESET + timedelta(minutes=minutes), observation),
                )

        # 05:12: collection over, two readings not yet processed, no board.
        record = refresh()
        assert (record["captured_count"], record["collected_count"]) == (3, 2)
        assert record["not_collected_count"] == 1
        assert record["membership_captured_at"] == RESET + timedelta(seconds=1)
        assert record["collection_finished_at"] == RESET + timedelta(minutes=9)
        assert record["proof_processed_at"] is None
        assert record["inputs_frozen_at"] is record["readable_at"] is None

        # The failed member's saved profile still holds the proof back.
        finish(9002, 15)
        assert refresh()["proof_processed_at"] is None
        # So does a Season-boundary league history a member saved.
        with psycopg.connect(connection_info, autocommit=True) as owner:
            owner.execute("SET session_replication_role = replica")
            owner.execute(
                "UPDATE collector_work SET league_history_observation_id = 9004"
                " WHERE player_id = %s",
                (players[0],),
            )
            owner.execute(
                "INSERT INTO python_processing_jobs (work_type, deduplication_key,"
                " input_json, observation_id, status) VALUES ('process_observation',"
                " 'reading-9004', '{}', 9004, 'pending')"
            )
        finish(9003, 16)
        assert refresh()["proof_processed_at"] is None
        finish(9004, 17)
        with psycopg.connect(connection_info, autocommit=True) as owner:
            owner.execute("SET session_replication_role = replica")
            _publish(owner, RESET, players)
        # The board's inputs froze, but the website does not show it yet.
        record = refresh()
        assert record["board_inputs"] is not None
        assert record["readable_at"] is record["settlement"] is None
        # A boundary check finishes before the website shows the board.
        with psycopg.connect(connection_info, autocommit=True) as owner:
            owner.execute("SET session_replication_role = replica")
            owner.execute("UPDATE reset_boundary_settlements SET state = 'unresolved'")
        # The website first showed the board at 05:29; the check saved it later.
        record = refresh(29)
        assert record["proof_processed_at"] == RESET + timedelta(minutes=17)
        assert record["inputs_frozen_at"] == RESET + timedelta(minutes=20)
        assert record["published_at"] == RESET + timedelta(minutes=24)
        assert record["readable_at"] == RESET + timedelta(minutes=29)
        assert record["board_inputs"] == {"Complete": 1, "Partial": 1, "Unavailable": 1}
        assert record["settlement"] == {"unresolved": 2}
        # Each value is kept as first seen.
        with psycopg.connect(connection_info, autocommit=True) as owner:
            owner.execute("SET session_replication_role = replica")
            owner.execute("UPDATE reset_boundary_settlements SET state = 'provisional'")
        record = refresh(40)
        assert record["readable_at"] == RESET + timedelta(minutes=29)
        assert record["settlement"] == {"unresolved": 2}


def test_an_older_reset_record_keeps_updating_until_its_readings_are_processed(
    database_url: str,
) -> None:
    # A reading can wait days, even beyond a week, for the archive while newer
    # Resets go by.
    with domain_database(database_url, include_coordinator=True) as connection_info:
        with psycopg.connect(connection_info, autocommit=True) as owner:
            owner.execute("SET session_replication_role = replica")
            _seed_reset(owner, RESET)

        def refresh_and_read() -> dict:
            with psycopg.connect(_as_worker(connection_info)) as connection:
                reset_acceptance.refresh(connection, readable_boundary=None, readable_at=None)
            with psycopg.connect(connection_info) as owner:
                return owner.execute(
                    "SELECT proof_processed_at FROM reset_acceptance_records"
                    " WHERE boundary_at = %s",
                    (RESET,),
                ).fetchone()

        assert refresh_and_read() == (None,)
        # Newer Resets go by before the reading is processed.
        with psycopg.connect(connection_info, autocommit=True) as owner:
            owner.execute("SET session_replication_role = replica")
            for days in (1, 10):
                owner.execute(
                    "INSERT INTO collector_reset_sweeps (boundary_at, member_ids)"
                    " VALUES (%s, '{}')",
                    (RESET + timedelta(days=days),),
                )
        assert refresh_and_read() == (None,)
        with psycopg.connect(connection_info, autocommit=True) as owner:
            owner.execute("SET session_replication_role = replica")
            owner.execute(
                "UPDATE python_processing_jobs SET status = 'complete', completed_at = %s"
                " WHERE status = 'pending'",
                (RESET + timedelta(days=10, hours=3),),
            )
        assert refresh_and_read() == (RESET + timedelta(days=10, hours=3),)


def test_a_reset_the_check_never_saw_gets_its_record_when_it_returns(
    database_url: str,
) -> None:
    # The alert check was down across three Resets.
    with domain_database(database_url, include_coordinator=True) as connection_info:
        with psycopg.connect(connection_info, autocommit=True) as owner:
            owner.execute("SET session_replication_role = replica")
            _seed_reset(owner, RESET)
            for days in (1, 2):
                owner.execute(
                    "INSERT INTO collector_reset_sweeps (boundary_at, member_ids)"
                    " VALUES (%s, '{}')",
                    (RESET + timedelta(days=days),),
                )
        with psycopg.connect(_as_worker(connection_info)) as connection:
            reset_acceptance.refresh(connection, readable_boundary=None, readable_at=None)
        with psycopg.connect(connection_info) as owner:
            recorded = owner.execute(
                "SELECT boundary_at, captured_count, collection_finished_at"
                " FROM reset_acceptance_records ORDER BY boundary_at"
            ).fetchall()
        assert recorded[0] == (RESET, 3, RESET + timedelta(minutes=9))
        assert [row[0] for row in recorded] == [RESET + timedelta(days=d) for d in (0, 1, 2)]


def test_a_processed_time_whose_jobs_were_cleaned_up_stays_unknown(
    database_url: str,
) -> None:
    # Processing finished, but the finished-job cleanup removed one job before
    # the check saw it: the time is unknown, never the collection time.
    with domain_database(database_url, include_coordinator=True) as connection_info:
        with psycopg.connect(connection_info, autocommit=True) as owner:
            owner.execute("SET session_replication_role = replica")
            _seed_reset(owner, RESET)
            owner.execute(
                "UPDATE python_processing_jobs SET status = 'complete', completed_at = %s",
                (RESET + timedelta(minutes=26),),
            )
            owner.execute("DELETE FROM python_processing_jobs WHERE observation_id = 9003")
        for _check in range(2):
            with psycopg.connect(_as_worker(connection_info)) as connection:
                record = reset_acceptance.refresh(
                    connection, readable_boundary=None, readable_at=None
                )
            assert record is not None
            assert record["collection_finished_at"] == RESET + timedelta(minutes=9)
            assert record["proof_processed_at"] is None


def test_a_replaced_reset_response_still_holds_back_the_processed_time(
    database_url: str,
) -> None:
    # A retried Reset item saves a newer battle log; the one it replaced was
    # saved too and must be processed before the readings count as processed.
    with domain_database(database_url, include_coordinator=True) as connection_info:
        with psycopg.connect(connection_info, autocommit=True) as owner:
            owner.execute("SET session_replication_role = replica")
            players = _seed_reset(owner, RESET)
            owner.execute(
                "INSERT INTO python_processing_jobs (work_type, deduplication_key,"
                " input_json, observation_id, status) VALUES"
                " ('process_observation', 'reading-9010', '{}', 9010, 'pending'),"
                " ('process_observation', 'reading-9011', '{}', 9011, 'complete')"
            )
            work_id = owner.execute(
                "SELECT id FROM collector_work WHERE player_id = %s", (players[1],)
            ).fetchone()[0]
            for observation in (9010, 9011):
                handoff = SimpleNamespace(
                    endpoint="battle_log", http_status=200, collector_work_id=work_id
                )
                with owner.transaction():
                    CollectorDatabase._record_intent_endpoint(owner, handoff, observation)
            owner.execute(
                "UPDATE python_processing_jobs SET status = 'complete', completed_at = %s"
                " WHERE observation_id IN (9002, 9003)",
                (RESET + timedelta(minutes=15),),
            )

        def refresh() -> dict:
            with psycopg.connect(_as_worker(connection_info)) as connection:
                record = reset_acceptance.refresh(
                    connection, readable_boundary=None, readable_at=None
                )
            assert record is not None
            return record

        assert refresh()["proof_processed_at"] is None
        with psycopg.connect(connection_info, autocommit=True) as owner:
            owner.execute("SET session_replication_role = replica")
            owner.execute(
                "UPDATE python_processing_jobs SET status = 'complete', completed_at = %s"
                " WHERE observation_id = 9010",
                (RESET + timedelta(minutes=20),),
            )
        assert refresh()["proof_processed_at"] == RESET + timedelta(minutes=20)


def test_the_alert_probe_prints_the_latest_resets_progress(
    database_url: str, tmp_path, monkeypatch, capsys
) -> None:
    reset = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    reset -= timedelta(hours=(reset.hour - 5) % 24)
    with domain_database(database_url, include_coordinator=True) as connection_info:
        url_file = tmp_path / "database-url"
        url_file.write_text(_as_worker(connection_info))
        monkeypatch.setenv("CLASHLENS_DATABASE_URL_FILE", str(url_file))
        alerts.reset_probe("0", "0")
        assert capsys.readouterr().out.split() == ["0"] * 5
        with psycopg.connect(connection_info, autocommit=True) as owner:
            owner.execute("SET session_replication_role = replica")
            _seed_reset(owner, reset)
        shown = int(reset.timestamp())
        alerts.reset_probe(str(shown), str(shown + 1500))
        boundary, captured, ended, frozen, readable = map(
            int, capsys.readouterr().out.split()
        )
        assert (boundary, captured, ended, frozen) == (shown, 3, 3, 0)
        assert readable == shown + 1500


def test_the_board_check_resolves_the_board_the_website_showed(
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
            # The website showed no board yet.
            alerts.publication_probe([])
            assert capsys.readouterr().out.split()[1] == "0"
            with psycopg.connect(connection_info, autocommit=True) as owner:
                owner.execute("SET session_replication_role = replica")
                players = _seed_reset(owner, RESET)
                _publish(owner, RESET, players)
            # The Season and day the website's page sent its visitor on to.
            board = alerts.signed_read("http://127.0.0.1:8000", "/v1/leaderboards/frozen?limit=1")
            daily = [board["official_season_id"], str(board["season_day_number"])]
            alerts.publication_probe(daily)
            assert int(capsys.readouterr().out.split()[1]) == int(RESET.timestamp())
        finally:
            client.close()
            database.close()

